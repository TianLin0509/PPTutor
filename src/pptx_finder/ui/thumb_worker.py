"""缩略图渲染线程：FIFO 渲染所有请求（不像预览只渲最新），小尺寸 + 磁盘缓存。

与预览的 RenderWorker 经 renderer._lock 串行（不并发 COM）；各自独立 PowerPoint 实例。
切换搜索时主窗调 clear() 丢弃旧请求，避免为已离开视图的结果白渲。
"""
from __future__ import annotations

import queue

from PySide6.QtCore import QThread, Signal

from .. import renderer

_STOP = object()


class ThumbWorker(QThread):
    thumb_rendered = Signal(str, int, str)  # path, page_no, png_path（失败空串）

    def __init__(self, parent=None, long_edge: int = 480):
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()
        self._long_edge = long_edge

    def request(self, path: str, page_no: int) -> None:
        self._q.put((path, page_no))

    def clear(self) -> None:
        """丢弃尚未处理的请求（切换搜索时调用）。"""
        try:
            while True:
                item = self._q.get_nowait()
                if item is _STOP:  # 保留停止信号
                    self._q.put(_STOP)
                    break
        except queue.Empty:
            pass

    def stop(self) -> None:
        self._q.put(_STOP)

    def run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                path, page_no = item
                png = renderer.render_page(path, page_no, long_edge=self._long_edge)
                self.thumb_rendered.emit(path, page_no, str(png) if png else "")
        finally:
            renderer.shutdown()
