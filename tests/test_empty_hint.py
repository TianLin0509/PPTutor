"""方向 02：零结果引导 —— 搜不到时给提示 + 可点补救建议。"""
from __future__ import annotations

from PySide6.QtCore import Qt

from test_ui import StubRender, _finish_fake_task, _index, _install_fake_background_task

import pptx_finder.ui.main_window as main_window_mod
from pptx_finder import search
from pptx_finder.ui.main_window import MainWindow, _empty_suggestions


def test_suggestions_unquote():
    assert "unquote" in _empty_suggestions('"精确短语"', "全部")


def test_suggestions_fewer_terms():
    assert "fewer" in _empty_suggestions("词一 词二", "全部")


def test_suggestions_switch_filename():
    assert "filename" in _empty_suggestions("xyz", "全部")
    assert "filename" not in _empty_suggestions("xyz", "仅文件名")


def test_suggestions_single_plain_in_filename_mode_can_restore_all_scope():
    assert _empty_suggestions("xyz", "仅文件名") == ["allmode"]


def test_query_suggestions_find_close_filename(tmp_path):
    conn = _index(tmp_path)

    suggestions = search.suggest_queries(conn, "算力方按", limit=2)

    assert suggestions
    assert "算力方案" in suggestions[0]


def test_zero_result_query_suggestion_can_be_applied(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("算力方按")
    win._do_search()

    qtbot.waitUntil(lambda: not win._sugg_btns["query"].isHidden(), timeout=2000)
    assert "算力方案" in win._empty_query_suggestion

    qtbot.mouseClick(win._sugg_btns["query"], Qt.LeftButton)

    assert win.result_list.count() == 1
    assert win.empty_hint.isHidden()


def test_zero_result_shows_hint(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    assert win.result_list.count() == 0
    assert not win.empty_hint.isHidden()    # 引导显示
    assert win.result_list.isHidden()       # 列表让位
    qtbot.waitUntil(lambda: "2 个文件" in win._empty_index_status.text(), timeout=2000)
    txt = win._empty_index_status.text()
    assert "索引状态" in txt
    assert "2 个文件" in txt
    assert "3 页" in txt
    assert "当前范围：全部范围" in txt
    assert win._empty_icon.text() == "🔍"
    assert win._empty_tip.text() == "换个说法试试"
    assert not any(token in win._empty_tip.text() for token in ("鏁", "鍚", "绱", "锛", "鈥", "鈫", "馃", "\ufffd"))


def test_zero_result_scope_status_reflects_content_mode(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.mode.setCurrentText("仅内容")
    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    qtbot.waitUntil(lambda: "当前范围：仅内容" in win._empty_index_status.text(), timeout=2000)
    assert "当前范围：仅内容" in win._empty_index_status.text()


def test_start_hint_explains_empty_index(qtbot, tmp_path):
    from pptx_finder import db
    conn = db.connect(tmp_path / "blank.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._do_search()
    qtbot.waitUntil(lambda: "索引库为空" in win._empty_index_status.text(), timeout=2000)
    assert "索引库为空" in win._empty_index_status.text()
    assert win._empty_icon.text() == "📂"
    assert "索引好后这里会列出最近文件" in win._empty_tip.text()
    assert "直接搜你写过的字" in win._empty_tip.text()
    assert not any(token in win._empty_tip.text() for token in ("鏁", "鍚", "绱", "锛", "鈥", "鈫", "馃", "\ufffd"))


def test_index_status_text_reuses_short_stats_cache(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    calls = 0

    def fake_stats(_conn, **_kwargs):
        nonlocal calls
        calls += 1
        return {"file_count": 2, "page_count": 3}

    monkeypatch.setattr(main_window_mod.db, "stats", fake_stats)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._index_status_cache = None
    calls = 0

    assert "已索引 2 个文件 / 3 页" in win._index_status_text()
    win.mode.setCurrentText("仅内容")
    assert "当前范围：仅内容" in win._index_status_text()
    assert calls == 1


def test_empty_mode_change_does_not_refresh_dashboard_stats(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._showing_recent = True
    win._list_stack.setCurrentWidget(win.dashboard)
    refreshes = []
    monkeypatch.setattr(win.dashboard, "schedule_refresh", lambda *, force=False: refreshes.append(force))

    win.mode.setCurrentText("仅内容")

    assert refreshes == []


def test_empty_hint_index_status_loads_in_background(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    def fake_stats(_conn, **_kwargs):
        calls.append("stats")
        return {"file_count": 2, "page_count": 3}

    monkeypatch.setattr(main_window_mod.db, "stats", fake_stats)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()
    calls.clear()

    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()

    assert calls == []
    status_task = next(task for task in tasks if task.label == "empty-index-status-refresh")
    assert "读取中" in win._empty_index_status.text()

    _finish_fake_task(status_task)

    assert calls == ["stats"]
    assert "已索引 2 个文件 / 3 页" in win._empty_index_status.text()


def test_empty_hint_reuses_inflight_status_refresh(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    def fake_stats(_conn, **_kwargs):
        calls.append("stats")
        return {"file_count": 2, "page_count": 3}

    monkeypatch.setattr(main_window_mod.db, "stats", fake_stats)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()
    calls.clear()

    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    first_task = next(task for task in tasks if task.label == "empty-index-status-refresh")
    win.search_box.setText("还是不存在的词abc456")
    win._do_search()
    win.search_box.setText("继续不存在的词def789")
    win._do_search()

    empty_tasks = [task for task in tasks if task.label == "empty-index-status-refresh"]
    assert empty_tasks == [first_task]
    assert calls == []

    _finish_fake_task(first_task)

    assert calls == ["stats"]
    assert "已索引 2 个文件 / 3 页" in win._empty_index_status.text()


def test_stale_empty_index_status_does_not_update_hidden_hint(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn, **_kwargs: {"file_count": 2, "page_count": 3},
    )
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()

    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    status_task = next(task for task in tasks if task.label == "empty-index-status-refresh")
    win.search_box.setText("昇腾")
    win._do_search()

    _finish_fake_task(status_task)

    assert win.empty_hint.isHidden()
    assert "已索引 2 个文件" not in win._empty_index_status.text()


def test_zero_result_can_open_health_diagnostics(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    calls = []
    win._request_full_rescan = lambda: calls.append("rescan")

    qtbot.mouseClick(win._diagnose_btn, Qt.LeftButton)

    assert win._settings_dialogs
    dlg = win._settings_dialogs[-1]
    qtbot.addWidget(dlg)
    assert dlg.tabs.tabText(dlg.tabs.currentIndex()) == "健康诊断"
    qtbot.waitUntil(lambda: "index:" in dlg.diagnostic_text.toPlainText(), timeout=1000)
    assert "index:" in dlg.diagnostic_text.toPlainText()
    assert dlg.rescan_btn.isEnabled()

    qtbot.mouseClick(dlg.rescan_btn, Qt.LeftButton)

    assert calls == ["rescan"]
    assert "rescan: requested in background" in dlg.diagnostic_text.toPlainText()


def test_closed_health_diagnostics_not_kept_before_reopen(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win._open_health_diagnostics()

    assert len(win._settings_dialogs) == 1
    first = win._settings_dialogs[0]
    qtbot.addWidget(first)

    first.close()
    qtbot.waitUntil(lambda: not first.isVisible(), timeout=1000)
    win._open_health_diagnostics()

    assert len(win._settings_dialogs) == 1
    assert win._settings_dialogs[0] is not first
    assert win._settings_dialogs[0].isVisible()


def test_result_hides_hint(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    assert not win.empty_hint.isHidden()
    win.search_box.setText("昇腾")
    win._do_search()
    assert win.empty_hint.isHidden()        # 有结果 → 引导隐藏
    assert win.result_list.count() == 1
