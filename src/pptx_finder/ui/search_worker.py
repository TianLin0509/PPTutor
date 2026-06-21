"""Background search worker.

The SQLite/FTS query can be fast most of the time, but it is still unbounded
from the UI thread's perspective. This worker keeps only the latest pending
query and lets the main window ignore stale completions by request id.
"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from .. import db, search as search_mod

log = logging.getLogger(__name__)


class SearchWorker(QThread):
    searched = Signal(int, str, object, float, object)

    def __init__(self, db_path: str | None = None, parent=None, conn=None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._conn_injected = conn
        self._cv = threading.Condition()
        self._pending: tuple[int, str, str] | None = None
        self._stop = False

    def request(self, req_id: int, query: str, mode_key: str) -> None:
        with self._cv:
            self._pending = (req_id, query, mode_key)
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    @staticmethod
    def _apply_mode(results: list, mode_key: str) -> list:
        if mode_key == "filename":
            return [r for r in results if r.name_hit]
        if mode_key == "content":
            return [r for r in results if r.hits]
        return results

    def run(self) -> None:
        own_conn = None
        conn = self._conn_injected
        if conn is None:
            if not self._db_path:
                self.searched.emit(0, "", [], 0.0, "missing db path")
                return
            own_conn = db.connect(self._db_path)
            conn = own_conn
        try:
            while True:
                with self._cv:
                    while self._pending is None and not self._stop:
                        self._cv.wait()
                    if self._stop:
                        return
                    req_id, query, mode_key = self._pending
                    self._pending = None
                started = time.perf_counter()
                error = None
                results = []
                try:
                    results = self._apply_mode(search_mod.search(conn, query), mode_key)
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    log.warning("search failed: %s", query, exc_info=True)
                elapsed_ms = (time.perf_counter() - started) * 1000
                self.searched.emit(req_id, query, results, elapsed_ms, error)
        finally:
            if own_conn is not None:
                own_conn.close()
