"""方向 04：搜索历史 —— 最近搜索词去重、提前、截断。"""
from __future__ import annotations

from test_ui import StubRender, _index

from pptx_finder import history
from pptx_finder.ui.main_window import MainWindow


def test_add_and_load(tmp_path):
    history.add_history("算力", base=tmp_path)
    history.add_history("昇腾", base=tmp_path)
    assert history.load_history(base=tmp_path)[:2] == ["昇腾", "算力"]  # 最近在前


def test_dedup_moves_to_front(tmp_path):
    history.add_history("算力", base=tmp_path)
    history.add_history("昇腾", base=tmp_path)
    history.add_history("算力", base=tmp_path)
    assert history.load_history(base=tmp_path) == ["算力", "昇腾"]  # 重复提到最前，不增条数


def test_limit(tmp_path):
    for i in range(15):
        history.add_history(f"q{i}", base=tmp_path)
    assert len(history.load_history(limit=10, base=tmp_path)) == 10


def test_blank_ignored(tmp_path):
    history.add_history("   ", base=tmp_path)
    assert history.load_history(base=tmp_path) == []


def test_ui_has_completer(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win.search_box.completer() is win._completer
