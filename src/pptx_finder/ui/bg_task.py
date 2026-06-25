"""通用后台一次性任务：把可能阻塞 UI 主线程的重活（版本解压重组 / 启 PowerPoint COM）
丢到后台线程跑，完成经信号回主线程刷新。绝不在主线程同步等。

配合 main_window._run_bg 使用：主线程只 start() 即返回，UI 全程响应。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)
_diag_lock = threading.Lock()
_active: dict[int, tuple[str, float]] = {}
_samples: deque[float] = deque(maxlen=128)
_total = 0
_failed = 0
_max_ms = 0.0
try:
    _MAX_CONCURRENT = max(1, int(os.environ.get("PPTUTOR_BG_TASKS", "4")))
except ValueError:
    _MAX_CONCURRENT = 4
_gate = threading.BoundedSemaphore(_MAX_CONCURRENT)
_waiting = 0


def diagnostic_lines() -> list[str]:
    with _diag_lock:
        samples = sorted(_samples)
        p95 = samples[int(len(samples) * 0.95) - 1] if samples else 0.0
        active_labels = [label or "task" for label, _start in _active.values()]
        return [
            "background_tasks: "
            f"active={len(_active)} waiting={_waiting} limit={_MAX_CONCURRENT} "
            f"total={_total} failed={_failed} max_ms={_max_ms:.1f} p95_ms={p95:.1f}",
            "background_active: " + (", ".join(active_labels[:8]) if active_labels else "-"),
        ]


class BackgroundTask(QThread):
    done = Signal(object)  # fn 的返回值（异常时为 None），经队列连接切回主线程

    def __init__(self, fn: Callable, label: str = "", parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    def run(self) -> None:
        global _failed, _max_ms, _total, _waiting
        result = None
        ident = id(self)
        with _diag_lock:
            _waiting += 1
        _gate.acquire()
        start = time.perf_counter()
        with _diag_lock:
            _waiting -= 1
            _active[ident] = (self._label, start)
            _total += 1
        try:
            result = self._fn()
        except Exception:  # noqa: BLE001 后台任务失败不杀线程，结果回 None
            with _diag_lock:
                _failed += 1
            log.warning("background task failed: %s", self._label, exc_info=True)
        finally:
            elapsed = (time.perf_counter() - start) * 1000.0
            with _diag_lock:
                _active.pop(ident, None)
                _samples.append(elapsed)
                _max_ms = max(_max_ms, elapsed)
            _gate.release()
        self.done.emit(result)
