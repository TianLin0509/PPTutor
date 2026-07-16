from __future__ import annotations

import socket
import sqlite3

import pytest

from pptx_finder import db, render_client, search
from pptx_finder.models import FileResult
from pptx_finder.text_tokenize import tokenize
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.result_utils import sort_results

from test_ui import PendingSearchWorker, StubRender


def _add_file(conn, *, file_id: int, name: str, text: str, mtime: float) -> None:
    path = f"C:/perf/{file_id}-{name}"
    fid = db.upsert_file(
        conn,
        path=path,
        name=name,
        ext=".pptx",
        size=100,
        mtime=mtime,
        content_hash=f"hash-{file_id}",
        page_count=1,
        status="ok",
        error="",
        indexed_at=mtime,
    )
    db.replace_pages(conn, fid, [(1, text, tokenize(text))])


def _result(name: str, *, mtime: float, score: float = 1.0) -> FileResult:
    return FileResult(
        file_id=abs(hash((name, mtime))) % 1_000_000,
        path=f"C:/perf/{name}",
        name=name,
        ext=".pptx",
        mtime=mtime,
        size=100,
        page_count=1,
        status="ok",
        score=score,
        name_hit=False,
    )


def test_relevance_tiers_put_all_filename_hits_before_content(tmp_path):
    conn = db.connect(tmp_path / "rank.db")
    db.init_db(conn)
    _add_file(conn, file_id=1, name="AI算力.pptx", text="封面", mtime=10)
    _add_file(conn, file_id=2, name="项目说明.pptx", text="这里完整写着 AI算力 平台", mtime=20)
    _add_file(conn, file_id=3, name="AI算力规划扩展.pptx", text="其它内容", mtime=30)
    conn.commit()

    # Separators do not demote exact matches, but the source tier remains absolute:
    # even a partial filename hit precedes an exact slide-content hit.
    rows = search.search(conn, "AI 算力")

    assert [r.name for r in rows[:3]] == [
        "AI算力.pptx",
        "AI算力规划扩展.pptx",
        "项目说明.pptx",
    ]
    assert [r.match_kind for r in rows[:3]] == [
        "filename_exact",
        "partial",
        "content_exact",
    ]


def test_search_verifies_raw_text_without_n_plus_one_queries(tmp_path):
    conn = db.connect(tmp_path / "nplus1.db")
    db.init_db(conn)
    fid = db.upsert_file(
        conn,
        path="C:/perf/many-pages.pptx",
        name="many-pages.pptx",
        ext=".pptx",
        size=100,
        mtime=1,
        content_hash="many-pages",
        page_count=160,
        status="ok",
        error="",
        indexed_at=1,
    )
    db.replace_pages(
        conn,
        fid,
        [(i, f"AI page {i}", tokenize(f"AI page {i}")) for i in range(1, 161)],
    )
    conn.commit()
    raw_selects: list[str] = []
    conn.set_trace_callback(
        lambda sql: raw_selects.append(sql)
        if "SELECT raw_text FROM pages_raw" in sql
        else None
    )

    rows = search.search(conn, "AI")

    conn.set_trace_callback(None)
    assert rows
    assert len(raw_selects) <= 1, f"raw-text N+1 queries: {len(raw_selects)}"


def test_multi_criteria_sort_supports_name_then_recent():
    rows = [
        _result("b.pptx", mtime=300),
        _result("a.pptx", mtime=100),
        _result("a.pptx", mtime=200),
    ]

    ordered = sort_results(rows, ("name", "recent"))

    assert [(r.name, r.mtime) for r in ordered] == [
        ("a.pptx", 200),
        ("a.pptx", 100),
        ("b.pptx", 300),
    ]


def test_main_window_exposes_primary_and_secondary_sort_controls(qtbot, tmp_path):
    conn = db.connect(tmp_path / "multi-sort-ui.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.sort_combo.setCurrentText("文件名")
    win.sort_secondary.setCurrentText("最近修改")
    win._results_raw = [
        _result("b.pptx", mtime=300),
        _result("a.pptx", mtime=100),
        _result("a.pptx", mtime=200),
    ]

    win._apply_sort_render()

    assert win._sort_keys() == ("name", "recent")
    assert [(r.name, r.mtime) for r in win._results] == [
        ("a.pptx", 200),
        ("a.pptx", 100),
        ("b.pptx", 300),
    ]


def test_tray_hidden_window_stops_ui_monitor_timer(qtbot, tmp_path):
    conn = db.connect(tmp_path / "tray-idle.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win.show()
    qtbot.waitUntil(win._ui_loop_timer.isActive, timeout=500)
    win.hide()

    assert not win._ui_loop_timer.isActive()
    assert not hasattr(win, "_visible_thumb_timer")

    win.show()
    qtbot.waitUntil(win._ui_loop_timer.isActive, timeout=500)


def test_result_cards_are_loaded_on_demand_instead_of_streaming_all(qtbot, tmp_path):
    conn = db.connect(tmp_path / "lazy-ui.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    rows = [_result(f"deck-{i:03d}.pptx", mtime=float(i)) for i in range(200)]

    win._finish_search("deck", rows, 12.0)
    qtbot.wait(1200)

    assert win.result_list.count() <= win._RENDER_FIRST + 1
    assert win._render_plan_pos < len(win._render_plan)


def test_search_completion_status_does_not_wait_for_stats_refresh(qtbot, monkeypatch, tmp_path):
    conn = db.connect(tmp_path / "status-ui.db")
    db.init_db(conn)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = pending
    monkeypatch.setattr(win, "_refresh_status", lambda *args, **kwargs: None)
    win.search_box.setText("AI")
    win._do_search()
    req_id, query, _mode = pending.requests[-1]

    win._on_search_done(req_id, query, [_result("AI.pptx", mtime=1)], 18.0, None)

    assert "搜索完成" in win.status_label.text()
    assert "正在搜索" not in win.status_label.text()


def test_live_index_does_not_immediately_repeat_a_just_finished_search(qtbot, tmp_path):
    conn = db.connect(tmp_path / "live-ui.db")
    db.init_db(conn)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = pending
    win.search_box.setText("AI")
    win._do_search()
    req_id, query, _mode = pending.requests[-1]
    win._do_live_refresh()

    win._on_search_done(req_id, query, [], 12.0, None)
    qtbot.wait(120)

    assert len(pending.requests) == 1
    assert win._live_refresh.isActive()


def test_renderer_timeout_is_not_retried_for_another_full_timeout(monkeypatch):
    client = render_client.RendererProcessClient(request_timeout=0.01)
    calls = []

    def timeout(_payload, *, abort_generation=None):
        calls.append(1)
        raise socket.timeout("renderer stuck")

    monkeypatch.setattr(client, "_request_locked", timeout)

    with pytest.raises(socket.timeout):
        client.request({"op": "render"})
    assert len(calls) == 1


def test_cancelled_fts_query_propagates_to_search_worker_instead_of_returning_partial_results():
    class InterruptedConnection:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("interrupted")

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        search._recall(InterruptedConnection(), ["AI"])
