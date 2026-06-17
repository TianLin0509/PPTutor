"""后台渲染线程：串行调用 PowerPoint COM 渲染（COM 单线程套间）。

只渲染最新请求：用户快速切换结果时，丢弃积压的过期请求，避免白渲染。
"""
from __future__ import annotations

import queue

from PySide6.QtCore import QThread, Signal

from .. import renderer

_STOP = object()


class RenderWorker(QThread):
    rendered = Signal(int, str)  # request_id, png_path（失败为空串）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()

    def request(self, req_id: int, path: str, page_no: int, cache_key: str | None = None) -> None:
        self._q.put((req_id, path, page_no, cache_key))

    def stop(self) -> None:
        self._q.put(_STOP)

    def run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                # 抽干队列，只保留最后一个请求
                while True:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is _STOP:
                        return
                    item = nxt
                req_id, path, page_no, key = item
                png = renderer.render_page(path, page_no, cache_key=key)
                self.rendered.emit(req_id, str(png) if png else "")
        finally:
            renderer.shutdown()
