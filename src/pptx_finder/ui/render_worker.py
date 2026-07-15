"""后台渲染线程：串行调用 PowerPoint COM 渲染（COM 单线程套间）。

优先级模型：预览（仅最新一个）> 手动预热 > 预取（相邻/命中页，填磁盘缓存）。
- 预览随时抢占：新预览到来即作废所有待处理的预取（它们是旧页的邻居）。
- 预取低优先、不发信号、复用已打开的同一文件（仅多导出几页），让用户翻过去时缓存命中=瞬间。
- 预热仅保留为显式能力；主窗口启动时不再自动拉起 PowerPoint。
"""
from __future__ import annotations

import collections
import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from .. import renderer


log = logging.getLogger(__name__)


class RenderWorker(QThread):
    rendered = Signal(int, str)  # request_id, png_path（失败为空串）
    # 80ms absorbs rapid result-selection churn while still getting the next page
    # ready before a normal human page-turn. Concurrency remains one COM export.
    _PREFETCH_IDLE_GRACE_SEC = 0.08
    _PRIORITY_PREVIEW = 0
    _PRIORITY_PREFETCH = 220
    # PowerPoint is effectively single-instance on normal Windows installs.
    # Keeping our hidden automation session around lets a later Explorer double-
    # click reuse its temporary snapshot/DPI state.  A short grace lets the UI
    # enqueue adjacent-page prefetches, then the owned session is released.
    _SESSION_IDLE_RELEASE_SEC = 0.35

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cv = threading.Condition()
        self._preview = None  # (req_id, path, page, key, long_edge, priority)，仅留最新
        self._prefetch: collections.deque = collections.deque()  # (path, page, key, long_edge, priority)
        self._prefetch_pending_keys: set[tuple[str, int, str | None, int, int]] = set()
        self._prefetch_active_keys: set[tuple[str, int, str | None, int, int]] = set()
        self._warm = False
        self._stopping = False
        self._release_requested = 0
        self._release_completed = 0
        self._release_count = 0
        self._preview_requested = 0
        self._preview_completed = 0
        self._preview_failed = 0
        self._prefetch_requested = 0
        self._prefetch_completed = 0
        self._prefetch_failed = 0
        self._prefetch_deduped = 0
        self._prefetch_cleared = 0
        self._preview_cleared = 0
        self._warm_requested = 0
        self._warm_completed = 0
        self._max_prefetch_queue = 0
        self._session_maybe_open = False
        self._session_last_activity = 0.0
        self._idle_session_releases = 0
        self._shutdown_failures = 0

    def _safe_renderer_shutdown(self) -> bool:
        """Keep the worker alive when PowerPoint transiently rejects cleanup.

        A dead render thread is much worse than one failed cleanup attempt: every
        later preview request remains queued forever and the UI keeps showing
        ``正在渲染``.  The worker can retry an idle/release cleanup on its next
        pass; explicit external-open handoff still waits for a successful pass.
        """
        try:
            renderer.shutdown()
            return True
        except Exception:  # noqa: BLE001 COM/RPC cleanup may be transient
            with self._cv:
                self._shutdown_failures += 1
            log.warning("preview renderer shutdown failed; will retry", exc_info=True)
            return False

    def request(
        self,
        req_id: int,
        path: str,
        page_no: int,
        cache_key: str | None = None,
        long_edge: int = 1600,
        priority: int = _PRIORITY_PREVIEW,
    ) -> None:
        with self._cv:
            self._preview_requested += 1
            self._preview = (req_id, path, page_no, cache_key, int(long_edge), int(priority))
            self._warm = False  # 用户已在等真实预览；预热此时只会抢占等待路径
            self._prefetch_cleared += len(self._prefetch)
            self._prefetch.clear()  # 新预览 → 旧页的预取全作废
            self._prefetch_pending_keys.clear()
            self._cv.notify()

    def prefetch(
        self,
        path: str,
        page_no: int,
        cache_key: str | None = None,
        long_edge: int = 960,
        priority: int = _PRIORITY_PREFETCH,
    ) -> None:
        """后台预渲染某页填缓存（低优先、不发信号）；新预览到来会清空待预取。"""
        key = (path, page_no, cache_key, int(long_edge), int(priority))
        with self._cv:
            if key in self._prefetch_pending_keys or key in self._prefetch_active_keys:
                self._prefetch_deduped += 1
                return
            self._prefetch_requested += 1
            self._prefetch.append((path, page_no, cache_key, int(long_edge), int(priority)))
            self._prefetch_pending_keys.add(key)
            self._max_prefetch_queue = max(self._max_prefetch_queue, len(self._prefetch))
            self._cv.notify()

    def prewarm(self) -> None:
        """后台静默预热 PowerPoint COM（启动后调），让用户首次预览不卡冷启动(~1.5s)。"""
        with self._cv:
            if self._preview is not None:
                return
            self._warm_requested += 1
            self._warm = True
            self._cv.notify()

    def clear(self) -> None:
        """Discard queued render work that is no longer relevant to the current search."""
        with self._cv:
            if self._preview is not None:
                self._preview_cleared += 1
            self._preview = None
            self._warm = False
            self._prefetch_cleared += len(self._prefetch)
            self._prefetch.clear()
            self._prefetch_pending_keys.clear()
            self._cv.notify()

    def release_session(self, timeout_sec: float = 20.0) -> bool:
        """Close the preview presentation/COM client on this worker thread.

        External PowerPoint opening must call this first.  COM state is
        thread-local, so closing it from the caller/background thread would not
        release the renderer's apartment and could expose that hidden session as
        the user's normal PowerPoint window.  Never hard-abort here: the renderer
        Python child and the POWERPNT.EXE COM server are separate processes, so
        killing only the former can strand a hash-named snapshot presentation.
        A timed-out handoff is reported to the caller and the worker is left to
        finish and clean up cooperatively.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._cv:
            if self._stopping:
                return False
            if not self.isRunning():
                return True
            if self._preview is not None:
                self._preview_cleared += 1
            self._preview = None
            self._warm = False
            self._prefetch_cleared += len(self._prefetch)
            self._prefetch.clear()
            self._prefetch_pending_keys.clear()
            self._release_requested += 1
            target = self._release_requested
            self._cv.notify_all()
            while self._release_completed < target and not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            return self._release_completed >= target

    def abort_inflight(self) -> bool:
        abort = getattr(renderer, "abort_inflight", None)
        if not callable(abort):
            return False
        try:
            return bool(abort())
        except Exception:  # noqa: BLE001 emergency cleanup must not crash exit
            return False

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()

    def run(self) -> None:
        try:
            while True:
                with self._cv:
                    while True:
                        if self._stopping:
                            return
                        if self._release_requested > self._release_completed:
                            kind, data = "release", self._release_requested
                            break
                        if self._preview is not None:
                            kind, data = "preview", self._preview
                            self._preview = None
                            break
                        if self._warm:
                            self._warm = False
                            kind, data = "warm", None
                            break
                        if self._prefetch:
                            self._cv.wait(self._PREFETCH_IDLE_GRACE_SEC)
                            # A real preview/warm/release that arrived during the
                            # grace period always outranks speculative prefetch.
                            if (
                                self._stopping
                                or self._release_requested > self._release_completed
                                or self._preview is not None
                                or self._warm
                            ):
                                continue
                            if not self._prefetch:
                                continue
                            kind, data = "prefetch", self._prefetch.popleft()
                            self._prefetch_pending_keys.discard(data)
                            self._prefetch_active_keys.add(data)
                            break
                        if self._session_maybe_open:
                            remaining = (
                                self._SESSION_IDLE_RELEASE_SEC
                                - (time.monotonic() - self._session_last_activity)
                            )
                            if remaining <= 0:
                                kind, data = "idle_release", None
                                self._session_maybe_open = False
                                break
                            self._cv.wait(remaining)
                            continue
                        self._cv.wait()
                # —— 锁外执行实际渲染 ——
                if kind == "idle_release":
                    released = self._safe_renderer_shutdown()
                    with self._cv:
                        if released:
                            self._idle_session_releases += 1
                        else:
                            # Keep a retry deadline instead of pretending the
                            # hidden session was released successfully.
                            self._session_maybe_open = True
                            self._session_last_activity = time.monotonic()
                elif kind == "release":
                    released = self._safe_renderer_shutdown()
                    with self._cv:
                        if released:
                            # Drop anything that raced with the handoff.  UI file
                            # operations also suppress new preview requests, but
                            # this second clear makes the boundary self-contained.
                            if self._preview is not None:
                                self._preview_cleared += 1
                            self._preview = None
                            self._warm = False
                            self._prefetch_cleared += len(self._prefetch)
                            self._prefetch.clear()
                            self._prefetch_pending_keys.clear()
                            self._session_maybe_open = False
                            self._release_completed = max(self._release_completed, int(data))
                            self._release_count += 1
                            self._cv.notify_all()
                    if not released:
                        # The request remains pending and will be retried. Avoid
                        # a hot loop while PowerPoint is rejecting RPC calls.
                        time.sleep(0.05)
                elif kind == "warm":
                    try:
                        renderer.prewarm()
                    except Exception:  # noqa: BLE001
                        pass
                    with self._cv:
                        self._warm_completed += 1
                        self._session_maybe_open = True
                        self._session_last_activity = time.monotonic()
                elif kind == "preview":
                    req_id, path, page_no, key, long_edge, priority = data
                    try:
                        png = renderer.render_page(
                            path,
                            page_no,
                            cache_key=key,
                            long_edge=long_edge,
                            hi_priority=True,
                            priority=priority,
                            use_snapshot=True,
                        )
                    except Exception:  # noqa: BLE001
                        png = None
                    with self._cv:
                        self._preview_completed += 1
                        if not png:
                            self._preview_failed += 1
                        self._session_maybe_open = True
                        self._session_last_activity = time.monotonic()
                    self.rendered.emit(req_id, str(png) if png else "")
                else:  # prefetch：只复用已打开的安全快照，不发信号、被预览随时抢占
                    path, page_no, key, long_edge, priority = data
                    ok = False
                    try:
                        ok = bool(renderer.render_page(
                            path,
                            page_no,
                            cache_key=key,
                            long_edge=long_edge,
                            hi_priority=False,
                            priority=priority,
                            use_snapshot=True,
                            existing_session_only=True,
                        ))
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        with self._cv:
                            self._prefetch_completed += 1
                            if not ok:
                                self._prefetch_failed += 1
                            self._prefetch_active_keys.discard(data)
                            self._session_maybe_open = True
                            self._session_last_activity = time.monotonic()
        finally:
            self._safe_renderer_shutdown()

    def diagnostic_lines(self) -> list[str]:
        with self._cv:
            return [
                "render_worker: "
                f"preview_pending={self._preview is not None} "
                f"prefetch_pending={len(self._prefetch)} "
                f"prefetch_active={len(self._prefetch_active_keys)} "
                f"warm_pending={self._warm} "
                f"release={self._release_completed}/{self._release_requested} "
                f"stopping={self._stopping}",
                "render_worker_stats: "
                f"preview={self._preview_completed}/{self._preview_requested} "
                f"preview_failed={self._preview_failed} preview_cleared={self._preview_cleared} "
                f"prefetch={self._prefetch_completed}/{self._prefetch_requested} "
                f"prefetch_failed={self._prefetch_failed} "
                f"deduped={self._prefetch_deduped} cleared={self._prefetch_cleared} "
                f"max_prefetch_queue={self._max_prefetch_queue} "
                f"warm={self._warm_completed}/{self._warm_requested} "
                f"session_releases={self._release_count} "
                f"shutdown_failures={self._shutdown_failures} "
                f"idle_session_releases={self._idle_session_releases}",
            ]
