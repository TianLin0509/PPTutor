"""Parent-process client for the isolated renderer service."""
from __future__ import annotations

import atexit
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


_FALSE = {"0", "false", "no", "off"}
_TRUE = {"1", "true", "yes", "on"}


class RendererRequestAborted(RuntimeError):
    """The GUI explicitly cancelled an in-flight renderer request."""


def should_use_ipc() -> bool:
    """Return whether GUI-side renderer calls should go through a child process."""
    if os.environ.get("PPTUTOR_RENDERER_CHILD") == "1":
        return False
    flag = os.environ.get("PPTUTOR_RENDERER_IPC")
    if flag is not None:
        return flag.strip().lower() not in _FALSE
    # Keep source/test runs simple; packaged user builds get crash isolation by default.
    return bool(getattr(sys, "frozen", False))


class RendererProcessClient:
    def __init__(self, *, connect_timeout: float = 10.0, request_timeout: float = 20.0):
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self._lock = threading.RLock()
        # ``abort`` must remain callable while ``request`` owns ``_lock`` and is
        # blocked in socket.readline().  Keep transport ownership on a separate,
        # very short-lived lock so the UI can cut the child process loose.
        self._transport_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._file = None
        # Before the authenticated handshake the child cannot have received a
        # render request, so terminating a failed startup is safe.  Afterwards
        # it may own a separate POWERPNT.EXE COM server and must be allowed to
        # unwind through render_service.main()'s finally cleanup.
        self._child_ready = False
        self._detached_cleanup = 0
        self._detached_alive = 0
        # Keep exact Popen objects until their handshake-complete children have
        # really exited.  This makes the safety gate self-healing via poll() even
        # if Windows refuses to create the optional daemon reaper thread.
        self._detached_procs: list[Any] = []
        self._request_active = False
        self._abort_generation = 0
        self._aborts = 0
        self._seq = 0
        self._total = 0
        self._restarts = 0
        self._timeouts = 0
        self._crashes = 0
        self._last_ms = 0.0
        self._samples: deque[float] = deque(maxlen=128)
        self._last_error = ""

    def _command(self, port: int, token: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--renderer-worker", str(port), token]
        return [sys.executable, "-m", "pptx_finder", "--renderer-worker", str(port), token]

    def _start_locked(self) -> None:
        with self._transport_lock:
            proc = self._proc
            sock = self._sock
            detached_alive = self._prune_detached_locked()
        if detached_alive:
            self._last_error = "previous renderer is still cleaning up PowerPoint"
            raise RendererRequestAborted(self._last_error)
        if proc is not None and proc.poll() is None and sock is not None:
            return
        self._hard_stop_locked()
        # ``_hard_stop_locked`` may itself detach a handshake-complete child.
        # Starting another child now can create a second hidden POWERPNT.EXE while
        # the first still owns its COM session and snapshot deck.  Sacrifice one
        # preview request instead of multiplying Office pollution.
        with self._transport_lock:
            detached_alive = self._prune_detached_locked()
        if detached_alive:
            self._last_error = "previous renderer is still cleaning up PowerPoint"
            raise RendererRequestAborted(self._last_error)

        token = secrets.token_hex(16)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(self.connect_timeout)
        port = int(listener.getsockname()[1])

        env = dict(os.environ)
        env["PPTUTOR_RENDERER_CHILD"] = "1"
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            proc = subprocess.Popen(
                self._command(port, token),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(Path.home()),
                creationflags=creationflags,
            )
            with self._transport_lock:
                self._proc = proc
            sock, _addr = listener.accept()
            sock.settimeout(self.request_timeout)
            f = sock.makefile("rwb", buffering=0)
            hello = json.loads(f.readline().decode("utf-8"))
            if hello.get("token") != token:
                raise RuntimeError("renderer worker handshake token mismatch")
            with self._transport_lock:
                self._sock = sock
                self._file = f
                self._child_ready = True
            self._restarts += 1
            self._last_error = ""
        except Exception:
            self._hard_stop_locked()
            raise
        finally:
            listener.close()

    def _hard_stop_locked(self) -> None:
        with self._transport_lock:
            f = self._file
            self._file = None
            sock = self._sock
            self._sock = None
            proc = self._proc
            self._proc = None
            child_ready = self._child_ready
            self._child_ready = False
        if child_ready:
            self._disconnect_ready_child(f, sock, proc)
        else:
            self._stop_transport(f, sock, proc)

    def _graceful_stop_locked(self) -> None:
        """Detach transport and let a shutdown-acknowledging child exit itself.

        The child sends its reply after closing the owned Presentation and
        PowerPoint application, then still has a very small Python ``finally``
        cleanup window.  Terminating it immediately in that window can strand a
        hash-named snapshot deck in POWERPNT.EXE.
        """
        with self._transport_lock:
            f = self._file
            self._file = None
            sock = self._sock
            self._sock = None
            proc = self._proc
            self._proc = None
            self._child_ready = False
        self._stop_transport(f, sock, proc, graceful_wait_sec=2.0)

    def _disconnect_ready_child(self, f, sock: socket.socket | None, proc) -> None:
        """Wake a ready renderer without terminating its COM-cleanup process.

        POWERPNT.EXE is not a child of the Python renderer in a way that
        ``Popen.terminate`` cleans up.  Closing the authenticated transport wakes
        the parent request immediately; once an in-flight COM call returns, the
        renderer sees the broken socket and executes its ``finally: shutdown``.
        A daemon reaper only releases the process handle and never kills it.
        """
        self._stop_transport(f, sock, None)
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                return
        except Exception:  # noqa: BLE001 process may already be gone
            return
        with self._transport_lock:
            self._detached_cleanup += 1
            self._detached_procs.append(proc)
            self._detached_alive = len(self._detached_procs)

        def _reap() -> None:
            try:
                proc.wait()
            except Exception:  # noqa: BLE001 detached cleanup is best-effort
                pass
            finally:
                with self._transport_lock:
                    self._detached_procs = [
                        item for item in self._detached_procs if item is not proc
                    ]
                    self._detached_alive = len(self._detached_procs)

        try:
            reaper = threading.Thread(
                target=_reap,
                name="pptdoctor-render-cleanup",
                daemon=True,
            )
            reaper.start()
        except Exception as exc:  # noqa: BLE001 request-time poll remains the fallback
            self._last_error = (
                "renderer cleanup thread unavailable; polling child process: "
                f"{type(exc).__name__}: {exc}"
            )

    def _prune_detached_locked(self) -> int:
        """Refresh the detached-child gate; caller owns ``_transport_lock``."""
        alive = []
        for proc in self._detached_procs:
            try:
                if proc.poll() is None:
                    alive.append(proc)
            except Exception:  # noqa: BLE001 uncertainty must fail closed
                alive.append(proc)
        self._detached_procs = alive
        self._detached_alive = len(alive)
        return self._detached_alive

    @staticmethod
    def _stop_transport(
        f,
        sock: socket.socket | None,
        proc,
        *,
        graceful_wait_sec: float = 0.0,
    ) -> None:
        # ``shutdown`` is what wakes a different thread blocked in ``readline``;
        # closing the file object first can itself wait on that reader on Windows.
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass
        try:
            if sock is not None:
                sock.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if f is not None:
                f.close()
        except Exception:  # noqa: BLE001
            pass
        if proc is None:
            return
        if proc.poll() is None:
            if graceful_wait_sec > 0:
                try:
                    proc.wait(timeout=float(graceful_wait_sec))
                    return
                except Exception:  # noqa: BLE001 child is genuinely stuck
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass

    def _current_abort_generation(self) -> int:
        with self._transport_lock:
            return self._abort_generation

    def _request_locked(
        self,
        payload: dict[str, Any],
        *,
        abort_generation: int | None = None,
    ) -> dict[str, Any]:
        generation = (
            self._current_abort_generation()
            if abort_generation is None else int(abort_generation)
        )
        self._start_locked()
        if self._current_abort_generation() != generation:
            self._hard_stop_locked()
            raise RendererRequestAborted("renderer request aborted before send")
        with self._transport_lock:
            channel = self._file
        assert channel is not None
        self._seq += 1
        req_id = self._seq
        payload = dict(payload)
        payload["id"] = req_id
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        start = time.perf_counter()
        try:
            channel.write(raw)
            line = channel.readline()
            if not line:
                raise RuntimeError("renderer worker exited")
            resp = json.loads(line.decode("utf-8"))
            if resp.get("id") != req_id:
                raise RuntimeError("renderer worker response id mismatch")
            elapsed = (time.perf_counter() - start) * 1000.0
            self._last_ms = elapsed
            self._samples.append(elapsed)
            self._total += 1
            if not resp.get("ok"):
                self._last_error = str(resp.get("error") or "renderer error")
            return resp
        except socket.timeout:
            if self._current_abort_generation() != generation:
                self._last_error = "aborted"
                self._hard_stop_locked()
                raise RendererRequestAborted("renderer request aborted")
            self._timeouts += 1
            self._last_error = f"timeout after {self.request_timeout:.0f}s"
            self._hard_stop_locked()
            raise
        except Exception as exc:
            if self._current_abort_generation() != generation:
                self._last_error = "aborted"
                self._hard_stop_locked()
                raise RendererRequestAborted("renderer request aborted") from exc
            self._crashes += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._hard_stop_locked()
            raise

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            with self._transport_lock:
                generation = self._abort_generation
                self._request_active = True
            try:
                return self._request_locked(payload, abort_generation=generation)
            except RendererRequestAborted:
                raise
            except (socket.timeout, TimeoutError):
                # A full COM timeout already means the page is unhealthy/too slow. Repeating the
                # same call would turn a 20s failure into 40s of spinner with no new information.
                raise
            except Exception:
                # One restart attempt covers renderer crashes during a request.
                if self._current_abort_generation() != generation:
                    raise RendererRequestAborted("renderer request aborted")
                return self._request_locked(
                    payload,
                    abort_generation=self._current_abort_generation(),
                )
            finally:
                with self._transport_lock:
                    self._request_active = False

    def abort(self) -> bool:
        """Immediately break an in-flight/idle renderer child without ``_lock``.

        This is the emergency path used for external-open handoff and app exit.
        It never touches PowerPoint directly; only the isolated child process and
        its private socket are stopped.
        """
        with self._transport_lock:
            active = bool(
                self._request_active
                or self._proc is not None
                or self._sock is not None
                or self._file is not None
            )
            self._abort_generation += 1
            if active:
                self._aborts += 1
            f = self._file
            self._file = None
            sock = self._sock
            self._sock = None
            proc = self._proc
            self._proc = None
            child_ready = self._child_ready
            self._child_ready = False
        if child_ready:
            self._disconnect_ready_child(f, sock, proc)
        else:
            self._stop_transport(f, sock, proc)
        return active

    def render_page(
        self,
        path: str,
        page_no: int,
        *,
        cache_key: str | None,
        long_edge: int,
        hi_priority: bool,
        priority: int | None,
        use_snapshot: bool = False,
        existing_session_only: bool = False,
        allow_borrowed_session: bool = False,
        one_shot: bool = False,
    ) -> Path | None:
        try:
            resp = self.request({
                "op": "render_once" if one_shot else "render",
                "path": path,
                "page_no": int(page_no),
                "cache_key": cache_key,
                "long_edge": int(long_edge),
                "hi_priority": bool(hi_priority),
                "priority": priority,
                "use_snapshot": bool(use_snapshot),
                "existing_session_only": bool(existing_session_only),
                "allow_borrowed_session": bool(allow_borrowed_session),
            })
        except Exception:
            return None
        png = str(resp.get("path") or "")
        return Path(png) if png else None

    def close_current_presentation(self) -> None:
        with self._lock:
            try:
                self._request_locked({"op": "close_current"})
            except Exception:
                pass

    def release_session(self) -> bool:
        """Release PowerPoint/COM while keeping the renderer child ready and idle."""
        with self._lock:
            proc = self._proc
            try:
                if proc is None or proc.poll() is not None:
                    return True
            except Exception:
                return False
            try:
                resp = self._request_locked({"op": "release_session"})
                return bool(resp.get("ok") and resp.get("released"))
            except Exception:
                return False

    def prewarm(self) -> None:
        with self._lock:
            try:
                self._request_locked({"op": "prewarm"})
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            graceful = False
            try:
                if self._proc is not None and self._proc.poll() is None:
                    resp = self._request_locked({"op": "shutdown"})
                    graceful = bool(resp.get("ok") and resp.get("shutdown"))
            except Exception:
                pass
            finally:
                if graceful:
                    self._graceful_stop_locked()
                else:
                    self._hard_stop_locked()

    def diagnostic_lines(self) -> list[str]:
        acquired = self._lock.acquire(blocking=False)
        try:
            # Samples are written while ``_lock`` is held. If a request owns it,
            # skip percentile calculation instead of iterating a mutating deque.
            samples = sorted(self._samples) if acquired else []
            p95 = samples[int(len(samples) * 0.95) - 1] if samples else 0.0
            proc = self._proc
            try:
                alive = proc is not None and proc.poll() is None
            except Exception:  # noqa: BLE001 diagnostic reads must never block/fail UI
                alive = False
            return [
                "renderer_ipc: "
                f"enabled={should_use_ipc()} alive={alive} busy={not acquired} total={self._total} "
                f"restarts={self._restarts} crashes={self._crashes} timeouts={self._timeouts} "
                f"aborts={self._aborts} detached_cleanup={self._detached_cleanup} "
                f"detached_alive={self._detached_alive} "
                f"last_ms={self._last_ms:.1f} p95_ms={p95:.1f}",
                f"renderer_ipc_last_error: {self._last_error or '-'}",
            ]
        finally:
            if acquired:
                self._lock.release()


_client = RendererProcessClient()
atexit.register(_client.shutdown)


def render_page(
    path: str,
    page_no: int,
    *,
    cache_key: str | None,
    long_edge: int,
    hi_priority: bool,
    priority: int | None,
    use_snapshot: bool = False,
    existing_session_only: bool = False,
    allow_borrowed_session: bool = False,
    one_shot: bool = False,
) -> Path | None:
    return _client.render_page(
        path,
        page_no,
        cache_key=cache_key,
        long_edge=long_edge,
        hi_priority=hi_priority,
        priority=priority,
        use_snapshot=use_snapshot,
        existing_session_only=existing_session_only,
        allow_borrowed_session=allow_borrowed_session,
        one_shot=one_shot,
    )


def render_page_once(
    path: str,
    page_no: int,
    *,
    cache_key: str | None,
    long_edge: int,
    hi_priority: bool,
    priority: int | None,
    use_snapshot: bool = False,
    allow_borrowed_session: bool = False,
) -> Path | None:
    return render_page(
        path,
        page_no,
        cache_key=cache_key,
        long_edge=long_edge,
        hi_priority=hi_priority,
        priority=priority,
        use_snapshot=use_snapshot,
        allow_borrowed_session=allow_borrowed_session,
        one_shot=True,
    )


def close_current_presentation() -> None:
    _client.close_current_presentation()


def release_session() -> bool:
    return _client.release_session()


def prewarm() -> None:
    _client.prewarm()


def shutdown() -> None:
    _client.shutdown()


def abort_inflight() -> bool:
    return _client.abort()


def diagnostic_lines() -> list[str]:
    return _client.diagnostic_lines()
