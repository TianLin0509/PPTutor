"""后台索引线程：在 QThread 里跑 update_index，进度经信号回主线程。"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from .. import db, indexer

log = logging.getLogger(__name__)


class IndexWorker(QThread):
    progress = Signal(int, int, str)  # done, total, current_path
    finished_index = Signal(dict)     # summary
    _PROGRESS_EMIT_MS = 80

    def __init__(self, db_path: str, roots: list[str], workers: int | None = None, parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self._roots = roots
        self._workers = workers
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        conn = db.connect(self._db_path)
        db.init_db(conn)
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
                summary = indexer.update_index(
                    conn, self._roots,
                    progress_cb=emit_progress,
                    workers=self._workers,
                    stop_event=self._stop,
                )
            except Exception as e:  # noqa: BLE001 索引线程兜底，不让异常杀进程
                self.finished_index.emit({"error": str(e)})
                return
            self.finished_index.emit(summary)
            try:
                from .. import cluster
                cluster.compute_groups(conn)  # 版本归组（后台，失败不影响搜索就绪）
            except Exception as e:  # noqa: BLE001
                log.warning("compute_groups failed: %s", e)
            try:
                maintenance = db.maintain(conn)
                if maintenance.get("error"):
                    log.warning("db maintenance incomplete: %s", maintenance["error"])
            except Exception as e:  # noqa: BLE001
                log.warning("db maintenance failed: %s", e)
        finally:
            conn.close()
