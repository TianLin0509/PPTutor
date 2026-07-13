from __future__ import annotations

import logging
import threading
import time

from pptx_finder import db
from pptx_finder.ui import search_worker as search_worker_mod
from test_ui import _index

from pptx_finder.ui.search_worker import SearchWorker


class InterruptibleConn:
    def __init__(self):
        self.interrupted = threading.Event()

    def interrupt(self):
        self.interrupted.set()


def test_search_worker_returns_latest_query_results(qtbot, tmp_path):
    conn = _index(tmp_path)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(7, "昇腾", "all")

        req_id, query, results, elapsed_ms, error = blocker.args
        assert req_id == 7
        assert query == "昇腾"
        assert error is None
        assert elapsed_ms >= 0
        assert len(results) == 1
        assert results[0].hits
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_filters_modes(qtbot, tmp_path):
    conn = _index(tmp_path)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(8, "昇腾", "filename")

        _, _, results, _, error = blocker.args
        assert error is None
        assert results == []
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_propagates_case_sensitive_flag(monkeypatch, qtbot, tmp_path):
    conn = db.connect(tmp_path / "case-worker.db")
    db.init_db(conn)
    seen: list[bool] = []

    def fake_search(_conn, _query, exts=None, case_sensitive=False):
        seen.append(bool(case_sensitive))
        return []

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000):
            worker.request(9, "AI SP", "all", case_sensitive=True)
        assert seen == [True]
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_interrupts_stale_running_query(monkeypatch, qtbot, tmp_path):
    conn = InterruptibleConn()
    slow_started = threading.Event()
    seen = []

    def fake_search(conn_arg, query, exts=None):
        if query == "slow":
            slow_started.set()
            assert conn_arg.interrupted.wait(2), "slow search should be interrupted"
            raise RuntimeError("interrupted")
        return []

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.searched.connect(lambda *args: seen.append(args))
    worker.start()
    try:
        worker.request(1, "slow", "all")
        assert slow_started.wait(1), "slow search should start"

        started = time.perf_counter()
        worker.request(2, "fast", "all")
        qtbot.waitUntil(lambda: any(args[0] == 2 for args in seen), timeout=1500)

        assert time.perf_counter() - started < 1.2
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_does_not_emit_interrupted_stale_query(monkeypatch, qtbot, tmp_path):
    conn = InterruptibleConn()
    slow_started = threading.Event()
    seen = []

    def fake_search(conn_arg, query, exts=None):
        if query == "slow":
            slow_started.set()
            assert conn_arg.interrupted.wait(2), "slow search should be interrupted"
            raise RuntimeError("interrupted")
        return []

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.searched.connect(lambda *args: seen.append(args))
    worker.start()
    try:
        worker.request(1, "slow", "all")
        assert slow_started.wait(1), "slow search should start"

        worker.request(2, "fast", "all")
        qtbot.waitUntil(lambda: any(args[0] == 2 for args in seen), timeout=1500)
        qtbot.wait(80)

        assert [args[0] for args in seen] == [2]
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_does_not_emit_stale_success_when_newer_query_pending(monkeypatch, qtbot, tmp_path):
    conn = InterruptibleConn()
    slow_started = threading.Event()
    seen = []

    def fake_search(conn_arg, query, exts=None):
        if query == "slow":
            slow_started.set()
            assert conn_arg.interrupted.wait(2), "slow search should be interrupted"
            return ["stale"]
        return ["fresh"]

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.searched.connect(lambda *args: seen.append(args))
    worker.start()
    try:
        worker.request(1, "slow", "all")
        assert slow_started.wait(1), "slow search should start"

        worker.request(2, "fast", "all")
        qtbot.waitUntil(lambda: any(args[0] == 2 for args in seen), timeout=1500)
        qtbot.wait(80)

        assert [args[0] for args in seen] == [2]
        assert seen[0][2] == ["fresh"]
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_cancel_interrupts_running_query_without_emitting(monkeypatch, qtbot, tmp_path):
    conn = InterruptibleConn()
    slow_started = threading.Event()
    seen = []

    def fake_search(conn_arg, query, exts=None):
        if query == "slow":
            slow_started.set()
            assert conn_arg.interrupted.wait(2), "slow search should be interrupted"
            raise RuntimeError("interrupted")
        return []

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.searched.connect(lambda *args: seen.append(args))
    worker.start()
    try:
        worker.request(1, "slow", "all")
        assert slow_started.wait(1), "slow search should start"

        worker.cancel()
        qtbot.waitUntil(lambda: worker.diagnostics()["interrupted"] >= 1, timeout=1500)
        qtbot.wait(80)

        assert seen == []
        assert worker.diagnostics()["pending_query_chars"] == 0
        assert worker.diagnostics()["active_query_chars"] == 0
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_records_diagnostics_for_interrupts(monkeypatch, qtbot, tmp_path):
    conn = InterruptibleConn()
    slow_started = threading.Event()

    def fake_search(conn_arg, query, exts=None):
        if query == "slow":
            slow_started.set()
            assert conn_arg.interrupted.wait(2), "slow search should be interrupted"
            raise RuntimeError("interrupted")
        return []

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        worker.request(1, "slow", "all")
        assert slow_started.wait(1), "slow search should start"
        worker.request(2, "fast", "all")
        qtbot.waitUntil(lambda: worker.diagnostics()["total"] >= 2, timeout=1500)

        diag = worker.diagnostics()
        assert diag["interrupted"] >= 1
        assert diag["last_query_chars"] == len("fast")
        assert any("interrupted" in line for line in worker.diagnostic_lines())
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_diagnostic_lines_redact_query_text(monkeypatch, qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    monkeypatch.setattr(search_worker_mod.search_mod, "search", lambda _conn, _query: [])
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000):
            worker.request(3, "客户并购预算-绝密", "all")

        lines = "\n".join(worker.diagnostic_lines())
        assert "客户并购预算-绝密" not in lines
        assert "query_chars=" in lines
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_diagnostics_show_active_search_without_query_text(monkeypatch, qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    sensitive = "客户并购预算-绝密"
    active_started = threading.Event()
    release = threading.Event()

    def slow_search(_conn, _query, exts=None):
        active_started.set()
        release.wait(2)
        return []

    monkeypatch.setattr(search_worker_mod.search_mod, "search", slow_search)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        worker.request(6, sensitive, "all")
        assert active_started.wait(1), "slow search should start"

        lines = "\n".join(worker.diagnostic_lines())

        assert "search_active:" in lines
        assert f"query_chars={len(sensitive)}" in lines
        assert sensitive not in lines
    finally:
        release.set()
        worker.stop()
        worker.wait(3000)


def test_search_worker_failure_logs_redact_query_text(monkeypatch, qtbot, tmp_path, caplog):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    sensitive = "客户并购预算-绝密"

    def fake_search(_conn, _query, exts=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with caplog.at_level(logging.WARNING, logger="pptx_finder.ui.search_worker"):
            with qtbot.waitSignal(worker.searched, timeout=3000):
                worker.request(4, sensitive, "all")

        assert sensitive not in caplog.text
        assert "query_chars=" in caplog.text
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_diagnostic_error_redacts_query_text(monkeypatch, qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    sensitive = "客户并购预算-绝密"

    def fake_search(_conn, query, exts=None):
        raise RuntimeError(f"bad query: {query}")

    monkeypatch.setattr(search_worker_mod.search_mod, "search", fake_search)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000):
            worker.request(5, sensitive, "all")

        lines = "\n".join(worker.diagnostic_lines())
        assert sensitive not in lines
        assert "bad query:" in lines
        assert "[query]" in lines
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_diagnostics_include_p95_and_max():
    worker = SearchWorker(conn=None)
    for elapsed in (10, 20, 30, 100, 500):
        worker._record_diagnostics("sensitive term", float(elapsed), None)

    diag = worker.diagnostics()
    assert diag["p95_elapsed_ms"] == 500
    assert diag["max_elapsed_ms"] == 500
    assert diag["sample_count"] == 5

    lines = "\n".join(worker.diagnostic_lines())
    assert "p95=500 ms" in lines
    assert "max=500 ms" in lines
    assert "sensitive term" not in lines
