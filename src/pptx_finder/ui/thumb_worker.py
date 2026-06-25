"""缩略图渲染线程：FIFO 渲染所有请求（不像预览只渲最新），小尺寸 + 磁盘缓存。

与预览的 RenderWorker 经 renderer._lock 串行（不并发 COM）；各自独立 PowerPoint 实例。
切换搜索时主窗调 clear() 丢弃旧请求，避免为已离开视图的结果白渲。
"""
from __future__ import annotations

import itertools
import queue
import threading

from PySide6.QtCore import QThread, Signal

from .. import renderer

_STOP = object()


class ThumbWorker(QThread):
    thumb_rendered = Signal(str, int, str)  # path, page_no, png_path（失败空串）
    _PRIORITY_DEFAULT = 100

    def __init__(self, parent=None, long_edge: int = 480):
        super().__init__(parent)
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._long_edge = long_edge
        self._queued: set[tuple[str, int]] = set()
        self._queued_priority: dict[tuple[str, int], int] = {}
        self._active: set[tuple[str, int]] = set()
        self._stopping = False
        self._lock = threading.Lock()

    def request(self, path: str, page_no: int, priority: int = _PRIORITY_DEFAULT) -> None:
        key = (path, page_no)
        priority = int(priority)
        with self._lock:
            if self._stopping:
                return
            if key in self._active:
                return
            old_priority = self._queued_priority.get(key)
            if old_priority is not None and old_priority <= priority:
                return
            self._queued.add(key)
            self._queued_priority[key] = priority
        self._q.put((priority, next(self._seq), key))

    def clear(self) -> None:
        """丢弃尚未处理的请求（切换搜索时调用）。"""
        try:
            while True:
                _priority, _seq, item = self._q.get_nowait()
                if item is _STOP:  # 保留停止信号
                    self._q.put((10**9, next(self._seq), _STOP))
                    break
                with self._lock:
                    self._queued.discard(item)
                    self._queued_priority.pop(item, None)
        except queue.Empty:
            pass

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
        try:
            while True:
                _priority, _seq, item = self._q.get_nowait()
                if item is not _STOP:
                    with self._lock:
                        self._queued.discard(item)
                        self._queued_priority.pop(item, None)
        except queue.Empty:
            pass
        self._q.put((10**9, next(self._seq), _STOP))

    def run(self) -> None:
        try:
            while True:
                priority, _seq, item = self._q.get()
                if item is _STOP:
                    break
                path, page_no = item
                with self._lock:
                    if self._queued_priority.get((path, page_no)) != priority:
                        continue
                    self._queued.discard((path, page_no))
                    self._queued_priority.pop((path, page_no), None)
                    self._active.add((path, page_no))
                try:
                    try:
                        png = renderer.find_cached_render(path, page_no, min_long_edge=self._long_edge)
                        if png is None:
                            png = renderer.render_page(
                                path,
                                page_no,
                                long_edge=self._long_edge,
                                hi_priority=False,
                                priority=priority,
                            )
                    except Exception:  # noqa: BLE001
                        png = None
                    self.thumb_rendered.emit(path, page_no, str(png) if png else "")
                finally:
                    with self._lock:
                        self._active.discard((path, page_no))
        finally:
            renderer.shutdown()
