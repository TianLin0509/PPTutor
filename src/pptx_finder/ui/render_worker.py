"""后台渲染线程：串行调用 PowerPoint COM 渲染（COM 单线程套间）。

只渲染最新请求：用户快速切换结果时，丢弃积压的过期请求，避免白渲染。
"""
from __future__ import annotations

import queue

from PySide6.QtCore import QThread, Signal

from .. import renderer

_STOP = object()
_WARM = object()  # 预热哨兵：后台静默启动 PowerPoint COM


class RenderWorker(QThread):
    rendered = Signal(int, str)  # request_id, png_path（失败为空串）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()

    def request(self, req_id: int, path: str, page_no: int, cache_key: str | None = None) -> None:
        self._q.put((req_id, path, page_no, cache_key))

    def stop(self) -> None:
        self._q.put(_STOP)

    def prewarm(self) -> None:
        """请求后台静默预热 PowerPoint COM（启动后调），让用户首次预览不卡在冷启动(~1.5s)。"""
        self._q.put(_WARM)

    def run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                if item is _WARM:
                    try:
                        renderer._get_app()  # 后台静默拉起 PowerPoint，首次预览免冷启动
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                # 抽干队列，只保留最后一个渲染请求（warm 跳过，stop 立即退）
                while True:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is _STOP:
                        return
                    if nxt is _WARM:
                        continue
                    item = nxt
                req_id, path, page_no, key = item
                # hi_priority：预览抢占共享 COM 锁，不被一屏缩略图渲染拖在后面排队
                png = renderer.render_page(path, page_no, cache_key=key, hi_priority=True)
                self.rendered.emit(req_id, str(png) if png else "")
        finally:
            renderer.shutdown()
