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
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._file = None
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
        if self._proc is not None and self._proc.poll() is None and self._sock is not None:
            return
        self._hard_stop_locked()

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
            self._proc = subprocess.Popen(
                self._command(port, token),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(Path.home()),
                creationflags=creationflags,
            )
            sock, _addr = listener.accept()
            sock.settimeout(self.request_timeout)
            f = sock.makefile("rwb", buffering=0)
            hello = json.loads(f.readline().decode("utf-8"))
            if hello.get("token") != token:
                raise RuntimeError("renderer worker handshake token mismatch")
            self._sock = sock
            self._file = f
            self._restarts += 1
            self._last_error = ""
        except Exception:
            self._hard_stop_locked()
            raise
        finally:
            listener.close()

    def _hard_stop_locked(self) -> None:
        f = self._file
        self._file = None
        try:
            if f is not None:
                f.close()
        except Exception:  # noqa: BLE001
            pass
        sock = self._sock
        self._sock = None
        try:
            if sock is not None:
                sock.close()
        except Exception:  # noqa: BLE001
            pass
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass

    def _request_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._start_locked()
        assert self._file is not None
        self._seq += 1
        req_id = self._seq
        payload = dict(payload)
        payload["id"] = req_id
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        start = time.perf_counter()
        try:
            self._file.write(raw)
            line = self._file.readline()
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
            self._timeouts += 1
            self._last_error = f"timeout after {self.request_timeout:.0f}s"
            self._hard_stop_locked()
            raise
        except Exception as exc:
            self._crashes += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._hard_stop_locked()
            raise

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                return self._request_locked(payload)
            except (socket.timeout, TimeoutError):
                # A full COM timeout already means the page is unhealthy/too slow. Repeating the
                # same call would turn a 20s failure into 40s of spinner with no new information.
                raise
            except Exception:
                # One restart attempt covers renderer crashes during a request.
                return self._request_locked(payload)

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
    ) -> Path | None:
        try:
            resp = self.request({
                "op": "render",
                "path": path,
                "page_no": int(page_no),
                "cache_key": cache_key,
                "long_edge": int(long_edge),
                "hi_priority": bool(hi_priority),
                "priority": priority,
                "use_snapshot": bool(use_snapshot),
                "existing_session_only": bool(existing_session_only),
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

    def prewarm(self) -> None:
        with self._lock:
            try:
                self._request_locked({"op": "prewarm"})
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            try:
                if self._proc is not None and self._proc.poll() is None:
                    self._request_locked({"op": "shutdown"})
            except Exception:
                pass
            finally:
                self._hard_stop_locked()

    def diagnostic_lines(self) -> list[str]:
        with self._lock:
            samples = sorted(self._samples)
            p95 = samples[int(len(samples) * 0.95) - 1] if samples else 0.0
            alive = self._proc is not None and self._proc.poll() is None
            return [
                "renderer_ipc: "
                f"enabled={should_use_ipc()} alive={alive} total={self._total} "
                f"restarts={self._restarts} crashes={self._crashes} timeouts={self._timeouts} "
                f"last_ms={self._last_ms:.1f} p95_ms={p95:.1f}",
                f"renderer_ipc_last_error: {self._last_error or '-'}",
            ]


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
    )


def close_current_presentation() -> None:
    _client.close_current_presentation()


def prewarm() -> None:
    _client.prewarm()


def shutdown() -> None:
    _client.shutdown()


def diagnostic_lines() -> list[str]:
    return _client.diagnostic_lines()
