"""后台渲染线程：串行调用 PowerPoint COM 渲染（COM 单线程套间）。

优先级模型：预览（仅最新一个）> 预热 > 预取（相邻/命中页，填磁盘缓存）。
- 预览随时抢占：新预览到来即作废所有待处理的预取（它们是旧页的邻居）。
- 预取低优先、不发信号、复用已打开的同一文件（仅多导出几页），让用户翻过去时缓存命中=瞬间。
- 预热：后台静默拉起 PowerPoint，首次预览免冷启动。
"""
from __future__ import annotations

import collections
import threading

from PySide6.QtCore import QThread, Signal

from .. import renderer


class RenderWorker(QThread):
    rendered = Signal(int, str)  # request_id, png_path（失败为空串）
    _PREFETCH_IDLE_GRACE_SEC = 0.06

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cv = threading.Condition()
        self._preview = None  # (req_id, path, page, key)，仅留最新
        self._prefetch: collections.deque = collections.deque()  # (path, page, key)
        self._prefetch_pending_keys: set[tuple[str, int, str | None]] = set()
        self._prefetch_active_keys: set[tuple[str, int, str | None]] = set()
        self._warm = False
        self._stopping = False

    def request(self, req_id: int, path: str, page_no: int, cache_key: str | None = None) -> None:
        with self._cv:
            self._preview = (req_id, path, page_no, cache_key)
            self._warm = False  # 用户已在等真实预览；预热此时只会抢占等待路径
            self._prefetch.clear()  # 新预览 → 旧页的预取全作废
            self._prefetch_pending_keys.clear()
            self._cv.notify()

    def prefetch(self, path: str, page_no: int, cache_key: str | None = None) -> None:
        """后台预渲染某页填缓存（低优先、不发信号）；新预览到来会清空待预取。"""
        key = (path, page_no, cache_key)
        with self._cv:
            if key in self._prefetch_pending_keys or key in self._prefetch_active_keys:
                return
            self._prefetch.append((path, page_no, cache_key))
            self._prefetch_pending_keys.add(key)
            self._cv.notify()

    def prewarm(self) -> None:
        """后台静默预热 PowerPoint COM（启动后调），让用户首次预览不卡冷启动(~1.5s)。"""
        with self._cv:
            if self._preview is not None:
                return
            self._warm = True
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify()

    def run(self) -> None:
        try:
            while True:
                with self._cv:
                    while not (self._stopping or self._warm or self._preview or self._prefetch):
                        self._cv.wait()
                    if self._stopping:
                        return
                    if self._preview is not None:
                        kind, data = "preview", self._preview
                        self._preview = None
                    elif self._warm:
                        self._warm = False
                        kind, data = "warm", None
                    else:
                        self._cv.wait(self._PREFETCH_IDLE_GRACE_SEC)
                        if self._stopping:
                            return
                        if self._preview is not None or self._warm:
                            continue
                        if not self._prefetch:
                            continue
                        kind, data = "prefetch", self._prefetch.popleft()
                        self._prefetch_pending_keys.discard(data)
                        self._prefetch_active_keys.add(data)
                # —— 锁外执行实际渲染 ——
                if kind == "warm":
                    try:
                        renderer._get_app()  # 后台静默拉起 PowerPoint
                    except Exception:  # noqa: BLE001
                        pass
                elif kind == "preview":
                    req_id, path, page_no, key = data
                    try:
                        png = renderer.render_page(path, page_no, cache_key=key, hi_priority=True)
                    except Exception:  # noqa: BLE001
                        png = None
                    self.rendered.emit(req_id, str(png) if png else "")
                else:  # prefetch：低优先填缓存，不发信号、被预览随时抢占
                    path, page_no, key = data
                    try:
                        renderer.render_page(path, page_no, cache_key=key, hi_priority=False)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        with self._cv:
                            self._prefetch_active_keys.discard(data)
        finally:
            renderer.shutdown()
