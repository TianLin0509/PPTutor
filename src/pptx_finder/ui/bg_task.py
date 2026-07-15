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

from PySide6.QtCore import QThread, QTimer, Signal

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
# Keep one lane available for operations where a person is visibly waiting.
# Without this, four slow diagnostics/history jobs can leave "正在打开" queued
# indefinitely even though the main UI itself remains responsive.
_REGULAR_CONCURRENT = max(1, _MAX_CONCURRENT - 1) if _MAX_CONCURRENT > 1 else 1
_INTERACTIVE_LABELS = {
    "open", "restore", "export",
    "version-restore", "version-export", "version-recover",
    "version-restore-prepare",
    "ppt-slim-create",
    "ppt-slim-open",
    "copy-page-text",
    "autostart-toggle", "version-retention-update",
}
_gate_cv = threading.Condition()
_active_slots = 0
_waiting = 0


def _acquire_slot(label: str, cancelled: threading.Event) -> bool:
    global _active_slots, _waiting
    interactive = label in _INTERACTIVE_LABELS
    limit = _MAX_CONCURRENT if interactive else _REGULAR_CONCURRENT
    with _diag_lock:
        _waiting += 1
    try:
        with _gate_cv:
            while _active_slots >= limit:
                if cancelled.is_set():
                    return False
                _gate_cv.wait(0.05)
            if cancelled.is_set():
                return False
            _active_slots += 1
            return True
    finally:
        with _diag_lock:
            _waiting -= 1


def _release_slot() -> None:
    global _active_slots
    with _gate_cv:
        _active_slots = max(0, _active_slots - 1)
        _gate_cv.notify_all()


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
        self._cancelled = threading.Event()
        # BackgroundTask is deliberately one-shot.  Merely removing a finished
        # task from a Python tracking list does not destroy its parented QThread;
        # on Windows each retained QThread keeps a cluster of kernel semaphore
        # handles alive.  Long-lived windows (notably the film-report entry)
        # could therefore accumulate thousands of handles over a workday.
        self._retirement_connected = False

    def start(self, priority=QThread.InheritPriority) -> None:
        # Callers register their ``done`` / ``finished`` UI cleanup slots before
        # start().  Register retirement here, deliberately *last*: PySide/Qt 6.11
        # can crash if a nested event loop handles DeferredDelete before a later
        # Python finished-slot.  Last connection preserves every callback, then
        # releases the one-shot QThread and its native handles safely.
        if not self._retirement_connected:
            self.finished.connect(self._retire_after_signal_drain)
            self._retirement_connected = True
        super().start(priority)

    def _retire_after_signal_drain(self) -> None:
        # PySide 6.11 can access-violate in QtWidgets when a just-finished Python
        # QThread is deleted while nested dialog/test event loops still contain
        # its queued signal deliveries.  A short grace is invisible to users,
        # bounds retained handles, and lets every done/finished callback drain.
        # Detach first so closing a parent window during the grace cannot destroy
        # the task ahead of the timer and leave a callback aimed at an invalid
        # C++ wrapper.
        self.setParent(None)
        QTimer.singleShot(1000, self.deleteLater)

    @property
    def label(self) -> str:
        return self._label

    def stop(self) -> None:
        """Cancel a task that is still waiting for a background slot."""
        self._cancelled.set()
        with _gate_cv:
            _gate_cv.notify_all()

    def run(self) -> None:
        global _failed, _max_ms, _total, _waiting
        result = None
        ident = id(self)
        if not _acquire_slot(self._label, self._cancelled):
            self.done.emit(None)
            return
        if self._cancelled.is_set():
            _release_slot()
            self.done.emit(None)
            return
        start = time.perf_counter()
        with _diag_lock:
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
            _release_slot()
        self.done.emit(result)
