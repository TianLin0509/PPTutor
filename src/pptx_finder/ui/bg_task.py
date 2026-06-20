"""通用后台一次性任务：把可能阻塞 UI 主线程的重活（版本解压重组 / 启 PowerPoint COM）
丢到后台线程跑，完成经信号回主线程刷新。绝不在主线程同步等。

配合 main_window._run_bg 使用：主线程只 start() 即返回，UI 全程响应。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class BackgroundTask(QThread):
    done = Signal(object)  # fn 的返回值（异常时为 None），经队列连接切回主线程

    def __init__(self, fn: Callable, label: str = "", parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._label = label

    def run(self) -> None:
        result = None
        try:
            result = self._fn()
        except Exception:  # noqa: BLE001 后台任务失败不杀线程，结果回 None
            log.warning("background task failed: %s", self._label, exc_info=True)
        self.done.emit(result)
