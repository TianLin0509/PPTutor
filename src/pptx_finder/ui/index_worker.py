"""后台索引线程：在 QThread 里跑 update_index，进度经信号回主线程。"""
from __future__ import annotations

import logging
import os
import threading
import time

from PySide6.QtCore import QThread, Signal

from .. import db, indexer

log = logging.getLogger(__name__)


def _set_windows_background_mode(enabled: bool) -> bool:
    """Lower both CPU and I/O priority for the weekly automatic full scan."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        # THREAD_MODE_BACKGROUND_BEGIN / THREAD_MODE_BACKGROUND_END.
        priority = 0x00010000 if enabled else 0x00020000
        handle = ctypes.windll.kernel32.GetCurrentThread()
        return bool(ctypes.windll.kernel32.SetThreadPriority(handle, priority))
    except Exception:  # noqa: BLE001 priority is an optimization, never a blocker
        return False


class IndexWorker(QThread):
    progress = Signal(int, int, str)  # done, total, current_path
    finished_index = Signal(dict)     # summary
    _PROGRESS_EMIT_MS = 80
    _MAINTENANCE_MIN_UPDATES = 50

    def __init__(
        self,
        db_path: str,
        roots: list[str],
        workers: int | None = None,
        parent=None,
        *,
        background_priority: bool = False,
        supported_exts: tuple[str, ...] | None = None,
        compute_groups: bool = True,
        interaction_pause_sec: float = 0.8,
        feature_signature: str = "",
    ):
        super().__init__(parent)
        self._db_path = db_path
        self._roots = roots
        self._workers = workers
        self._background_priority = bool(background_priority)
        self._supported_exts = supported_exts
        self._compute_groups = bool(compute_groups)
        self._interaction_pause_sec = max(0.0, float(interaction_pause_sec))
        self._feature_signature = str(feature_signature or "")
        self._activity_lock = threading.Lock()
        self._pause_until = 0.0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def note_user_activity(self) -> None:
        """GUI 主线程只更新时间戳；索引线程据此暂停继续投递磁盘/CPU 工作。"""
        with self._activity_lock:
            self._pause_until = max(
                self._pause_until,
                time.monotonic() + self._interaction_pause_sec,
            )

    def _yield_to_foreground(self) -> None:
        while not self._stop.is_set():
            with self._activity_lock:
                remaining = self._pause_until - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(0.05, remaining))

    def run(self) -> None:
        background_mode = (
            _set_windows_background_mode(True)
            if self._background_priority else False
        )
        conn = None
        try:
            conn = db.connect(self._db_path)
            db.init_db(conn)
        except Exception as exc:  # noqa: BLE001 terminal state must reach the UI
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if background_mode:
                _set_windows_background_mode(False)
            self.finished_index.emit({"error": f"{type(exc).__name__}: {exc}"})
            return
        last_emit_at = 0.0
        last_phase: str | None = None

        def emit_progress(done: int, total: int, current: str) -> None:
            nonlocal last_emit_at, last_phase
            phase = "scan" if total < 0 else "index"
            now = time.monotonic()
            final = done >= total > 0
            should_emit = (
                final
                or phase != last_phase
                or not last_emit_at
                or (now - last_emit_at) * 1000 >= self._PROGRESS_EMIT_MS
            )
            if not should_emit:
                return
            last_emit_at = now
            last_phase = phase
            self.progress.emit(done, total, current)

        try:
            try:
                index_kwargs = {
                    "progress_cb": emit_progress,
                    "workers": self._workers,
                    "stop_event": self._stop,
                }
                if self._supported_exts is not None:
                    index_kwargs["supported_exts"] = self._supported_exts
                if not self._compute_groups:
                    index_kwargs["compute_content_hash"] = False
                if self._background_priority:
                    # 应用内所有建库都使用隔离的低 CPU/I/O 优先级进程；前台交互发生
                    # 时停止继续扩充队列，已经在跑的最多只有两个 worker。
                    index_kwargs["isolated_worker"] = True
                    index_kwargs["throttle_cb"] = self._yield_to_foreground
                    index_kwargs["max_pending_factor"] = 1
                summary = indexer.update_index(conn, self._roots, **index_kwargs)
                if self._feature_signature:
                    summary["feature_signature"] = self._feature_signature
            except Exception as e:  # noqa: BLE001 索引线程兜底，不让异常杀进程
                self.finished_index.emit({"error": str(e)})
                return
            if int(summary.get("cancelled", 0) or 0):
                # A user-requested stop must stay cheap. Grouping/FTS optimize/
                # VACUUM can take minutes on a large database and would make
                # quitting look hung after the actual scan already stopped.
                self.finished_index.emit(summary)
                return
            content_changed = bool(
                int(summary.get("indexed", 0) or 0)
                or int(summary.get("deleted", 0) or 0)
            )
            update_count = (
                int(summary.get("indexed", 0) or 0)
                + int(summary.get("skipped_ppt", 0) or 0)
            )
            should_maintain = bool(
                int(summary.get("deleted", 0) or 0)
                or update_count >= self._MAINTENANCE_MIN_UPDATES
            )
            if content_changed and self._compute_groups:
                try:
                    from .. import cluster
                    cluster.compute_groups(conn)  # 版本归组（后台，失败不影响搜索就绪）
                except Exception as e:  # noqa: BLE001
                    log.warning("compute_groups failed: %s", e)
            if should_maintain:
                try:
                    maintenance = db.maintain(conn)
                    if maintenance.get("error"):
                        log.warning("db maintenance incomplete: %s", maintenance["error"])
                except Exception as e:  # noqa: BLE001
                    log.warning("db maintenance failed: %s", e)
            # 完成信号必须排在所有写事务/维护之后。旧顺序会让 GUI 收到“完成”后
            # 立刻写 meta，与仍在归组/VACUUM 的连接争锁，最坏冻结 busy_timeout 8 秒。
            self.finished_index.emit(summary)
        finally:
            conn.close()
            if background_mode:
                _set_windows_background_mode(False)
