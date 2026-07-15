"""跨线程版本事件桥：watcher 子线程留版 → Qt 队列信号 → UI 主线程槽。

VersionManager 不依赖 Qt（保持 versioning 子系统纯净）；它只回调 emit_snapshot，
本桥把回调转成 Qt 信号——signal 在主线程创建，子线程 emit 自动走 QueuedConnection，
线程安全地把事件投递到 UI 主线程，无需手写锁或轮询。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class VersionBridge(QObject):
    snapshotted = Signal(str, str)  # (path, version_id)
    content_changed = Signal(str)   # Word/PDF 保存，仅触发搜索索引
    runtime_error = Signal(str)     # optional service failure, queued to GUI
    feature_state = Signal(str, bool)  # backend rollback, queued to GUI

    def emit_snapshot(self, path: str, version_id: str) -> None:
        """供 VersionManager.on_snapshot 回调（可能在 watcher 子线程被调用）。"""
        self.snapshotted.emit(path, version_id)

    def emit_content_changed(self, path: str) -> None:
        """把 watcher 线程里的 Word/PDF 保存事件投递到 UI 主线程。"""
        self.content_changed.emit(path)

    def emit_runtime_error(self, message: str) -> None:
        self.runtime_error.emit(str(message))

    def emit_feature_state(self, key: str, enabled: bool) -> None:
        self.feature_state.emit(str(key), bool(enabled))
