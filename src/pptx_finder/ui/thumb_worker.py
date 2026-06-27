"""缩略图加载线程：FIFO 处理所有请求（不像预览只保留最新）。

左侧结果缩略图不启动 PowerPoint COM：只使用已有渲染缓存、PPTX 内置
thumbnail、Windows Shell 缩略图缓存。切换搜索时主窗调 clear() 丢弃旧请求，
避免为已离开视图的结果白加载。
"""
from __future__ import annotations

import itertools
import queue
import threading

from PySide6.QtCore import QThread, Signal

from .. import renderer, thumbnailer

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
        self._requested = 0
        self._completed = 0
        self._failed = 0
        self._cache_hits = 0
        self._deduped = 0
        self._upgraded = 0
        self._cleared = 0
        self._max_queue = 0

    def request(self, path: str, page_no: int, priority: int = _PRIORITY_DEFAULT) -> None:
        key = (path, page_no)
        priority = int(priority)
        with self._lock:
            if self._stopping:
                return
            if key in self._active:
                self._deduped += 1
                return
            old_priority = self._queued_priority.get(key)
            if old_priority is not None and old_priority <= priority:
                self._deduped += 1
                return
            if old_priority is not None and priority < old_priority:
                self._upgraded += 1
            self._requested += 1
            self._queued.add(key)
            self._queued_priority[key] = priority
            self._max_queue = max(self._max_queue, len(self._queued))
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
                    if item in self._queued:
                        self._cleared += 1
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
                        if item in self._queued:
                            self._cleared += 1
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
                        self._deduped += 1
                        continue
                    self._queued.discard((path, page_no))
                    self._queued_priority.pop((path, page_no), None)
                    self._active.add((path, page_no))
                ok = False
                cache_hit = False
                try:
                    try:
                        png = thumbnailer.find_non_com_thumbnail(
                            path,
                            page_no,
                            long_edge=self._long_edge,
                        )
                        if png is not None:
                            cache_hit = True
                    except Exception:  # noqa: BLE001
                        png = None
                    ok = bool(png)
                    self.thumb_rendered.emit(path, page_no, str(png) if png else "")
                finally:
                    with self._lock:
                        self._completed += 1
                        if not ok:
                            self._failed += 1
                        if cache_hit:
                            self._cache_hits += 1
                        self._active.discard((path, page_no))
        finally:
            renderer.shutdown()

    def diagnostic_lines(self) -> list[str]:
        with self._lock:
            return [
                "thumb_worker: "
                f"queued={len(self._queued)} active={len(self._active)} "
                f"stopping={self._stopping}",
                "thumb_worker_stats: "
                f"completed={self._completed}/{self._requested} "
                f"failed={self._failed} cache_hits={self._cache_hits} "
                f"deduped={self._deduped} upgraded={self._upgraded} "
                f"cleared={self._cleared} max_queue={self._max_queue}",
            ]
