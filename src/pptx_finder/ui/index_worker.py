"""后台索引线程：在 QThread 里跑 update_index，进度经信号回主线程。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from .. import db, indexer


class IndexWorker(QThread):
    progress = Signal(int, int, str)  # done, total, current_path
    finished_index = Signal(dict)     # summary

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
        try:
            summary = indexer.update_index(
                conn, self._roots,
                progress_cb=lambda d, t, c: self.progress.emit(d, t, c),
                workers=self._workers,
                stop_event=self._stop,
            )
            try:
                from .. import cluster
                cluster.compute_groups(conn)  # 版本归组（后台，失败不影响搜索）
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001 索引线程兜底，不让异常杀进程
            summary = {"error": str(e)}
        finally:
            conn.close()
        self.finished_index.emit(summary)
