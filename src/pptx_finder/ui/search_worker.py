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
    _SLOW_DIAG_MS = 1000.0
    _DIAG_SAMPLE_LIMIT = 64
    _READ_BUSY_TIMEOUT_MS = 400

    def __init__(self, db_path: str | None = None, parent=None, conn=None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._conn_injected = conn
        self._cv = threading.Condition()
        self._pending: tuple[int, str, str, tuple[str, ...] | None, bool, bool] | None = None
        self._stop = False
        self._active_conn = None
        self._cancel_active = False
        self._diag_lock = threading.Lock()
        self._diag = {
            "total": 0,
            "slow": 0,
            "interrupted": 0,
            "last_query_chars": 0,
            "last_elapsed_ms": 0.0,
            "last_error": None,
            "active_query_chars": 0,
            "active_started": 0.0,
            "pending_query_chars": 0,
            "samples": [],
        }

    def request(self, req_id: int, query: str, mode_key: str,
                exts: tuple[str, ...] | None = None,
                case_sensitive: bool = False,
                group_similar: bool = True) -> None:
        with self._cv:
            self._pending = (
                req_id,
                query,
                mode_key,
                exts,
                bool(case_sensitive),
                bool(group_similar),
            )
            self._cancel_active = False
            with self._diag_lock:
                self._diag["pending_query_chars"] = len(query)
            self._interrupt_active_locked()
            self._cv.notify()

    def cancel(self) -> None:
        with self._cv:
            self._pending = None
            self._cancel_active = True
            with self._diag_lock:
                self._diag["pending_query_chars"] = 0
            self._interrupt_active_locked()
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            with self._diag_lock:
                self._diag["pending_query_chars"] = 0
            self._interrupt_active_locked()
            self._cv.notify()

    def _interrupt_active_locked(self) -> None:
        conn = self._active_conn
        if conn is None:
            return
        try:
            conn.interrupt()
        except Exception:  # noqa: BLE001 中断是加速路径，失败不应影响后续请求入队
            log.debug("failed to interrupt active search connection", exc_info=True)

    @staticmethod
    def _apply_mode(results: list, mode_key: str) -> list:
        if mode_key == "filename":
            return [r for r in results if r.name_hit]
        if mode_key == "content":
            return [r for r in results if r.hits]
        return results

    def diagnostics(self) -> dict:
        with self._diag_lock:
            d = dict(self._diag)
            samples = list(d.pop("samples", []))
        if d.get("active_started"):
            d["active_elapsed_ms"] = max(0.0, (time.perf_counter() - float(d["active_started"])) * 1000)
        else:
            d["active_elapsed_ms"] = 0.0
        d["sample_count"] = len(samples)
        if samples:
            ordered = sorted(samples)
            idx = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999999) - 1))
            d["p95_elapsed_ms"] = ordered[idx]
            d["max_elapsed_ms"] = max(samples)
        else:
            d["p95_elapsed_ms"] = 0.0
            d["max_elapsed_ms"] = 0.0
        return d

    def diagnostic_lines(self) -> list[str]:
        d = self.diagnostics()
        lines = [
            f"search: total={d['total']} slow={d['slow']} interrupted={d['interrupted']}",
        ]
        if d.get("active_query_chars"):
            lines.append(
                f"search_active: {d['active_elapsed_ms']:.0f} ms · query_chars={d['active_query_chars']}")
        if d.get("pending_query_chars"):
            lines.append(f"search_pending: query_chars={d['pending_query_chars']}")
        if d["sample_count"]:
            lines.append(
                f"search_latency: samples={d['sample_count']} p95={d['p95_elapsed_ms']:.0f} ms max={d['max_elapsed_ms']:.0f} ms")
        if d["last_query_chars"]:
            tail = f" error={d['last_error']}" if d["last_error"] else ""
            lines.append(
                f"search_last: {d['last_elapsed_ms']:.0f} ms · query_chars={d['last_query_chars']}{tail}")
        return lines

    def _record_diagnostics(self, query: str, elapsed_ms: float, error: object) -> None:
        err_text = str(error or "")
        if query and err_text:
            err_text = err_text.replace(query, "[query]")
        interrupted = "interrupted" in err_text.lower()
        with self._diag_lock:
            self._diag["total"] += 1
            if elapsed_ms >= self._SLOW_DIAG_MS:
                self._diag["slow"] += 1
            if interrupted:
                self._diag["interrupted"] += 1
            self._diag["last_query_chars"] = len(query)
            self._diag["last_elapsed_ms"] = elapsed_ms
            self._diag["last_error"] = err_text or None
            self._diag["samples"].append(float(elapsed_ms))
            if len(self._diag["samples"]) > self._DIAG_SAMPLE_LIMIT:
                self._diag["samples"] = self._diag["samples"][-self._DIAG_SAMPLE_LIMIT:]

    def run(self) -> None:
        own_conn = None
        conn = self._conn_injected
        try:
            while True:
                with self._cv:
                    while self._pending is None and not self._stop:
                        self._cv.wait()
                    if self._stop:
                        return
                    req_id, query, mode_key, exts, case_sensitive, group_similar = self._pending
                    self._pending = None
                    self._cancel_active = False
                    self._active_conn = conn  # 在同一把锁内提前置位，消除「取消落在赋值之前」的窗口
                    with self._diag_lock:
                        self._diag["pending_query_chars"] = 0
                        self._diag["active_query_chars"] = len(query)
                        self._diag["active_started"] = time.perf_counter()
                started = time.perf_counter()
                error = None
                results = []
                try:
                    if conn is None:
                        if not self._db_path:
                            raise RuntimeError("missing db path")
                        # Connect lazily for the request. A transient startup
                        # lock/path failure must be reported to the UI, not kill
                        # this QThread and leave every later query on “searching”.
                        # Interactive search is read-only and must fail fast
                        # behind a rare schema/VACUUM lock. The normal writer
                        # connection waits up to eight seconds and also issues
                        # journal-mode PRAGMAs, which used to leave the UI on
                        # “searching” for ~7.4 seconds in the reproduced case.
                        own_conn = db.connect_readonly(
                            self._db_path,
                            busy_timeout_ms=self._READ_BUSY_TIMEOUT_MS,
                        )
                        conn = own_conn
                        with self._cv:
                            if self._stop or self._cancel_active or self._pending is not None:
                                raise RuntimeError("interrupted")
                            self._active_conn = conn
                    search_kwargs = {"exts": exts}
                    # Keep the default call shape backward-compatible with test/fake
                    # search functions and older integrations; only opt in explicitly.
                    if case_sensitive:
                        search_kwargs["case_sensitive"] = True
                    if not group_similar:
                        search_kwargs["group_similar"] = False
                    results = self._apply_mode(
                        search_mod.search(conn, query, **search_kwargs),
                        mode_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    if "interrupted" in str(exc).lower():
                        log.debug(
                            "search interrupted: req_id=%s query_chars=%s",
                            req_id, len(query))
                    else:
                        log.warning(
                            "search failed: req_id=%s query_chars=%s error_type=%s",
                            req_id, len(query), type(exc).__name__, exc_info=True)
                finally:
                    with self._cv:
                        if self._active_conn is conn:
                            self._active_conn = None
                        suppress_stale_interrupted = (
                            error is not None
                            and "interrupted" in str(error).lower()
                            and (self._pending is not None or self._stop or self._cancel_active)
                        )
                        suppress_stale_success = self._pending is not None
                        suppress_cancelled = self._cancel_active
                        if suppress_cancelled:
                            self._cancel_active = False
                    with self._diag_lock:
                        self._diag["active_query_chars"] = 0
                        self._diag["active_started"] = 0.0
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._record_diagnostics(query, elapsed_ms, error)
                if suppress_cancelled or suppress_stale_interrupted or suppress_stale_success:
                    continue
                self.searched.emit(req_id, query, results, elapsed_ms, error)
        finally:
            if own_conn is not None:
                own_conn.close()
