"""方向 01：空查询默认视图 —— 列最近修改的 PPTX，打开即点。"""
from __future__ import annotations

import os

import fixtures_gen as fx
from test_ui import StubRender

from pptx_finder import db, indexer
from pptx_finder.ui.main_window import MainWindow


def _index_with_mtimes(tmp_path, names_mtimes):
    docs = tmp_path / "d"
    docs.mkdir()
    for nm, mt in names_mtimes:
        fx.make_pptx(docs / nm, [{"body": "内容页"}])
        os.utime(docs / nm, (mt, mt))
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


def test_recent_files_orders_by_mtime_desc(tmp_path):
    conn = _index_with_mtimes(tmp_path, [
        ("old.pptx", 1_000_000), ("new.pptx", 3_000_000), ("mid.pptx", 2_000_000)])
    recents = db.recent_files(conn, limit=10)
    assert [r.name for r in recents] == ["new.pptx", "mid.pptx", "old.pptx"]
    assert recents[0].hits == []      # 默认视图无命中片段
    assert recents[0].score == 0.0


def test_recent_files_limit(tmp_path):
    conn = _index_with_mtimes(tmp_path, [(f"f{i}.pptx", 1_000_000 + i) for i in range(8)])
    assert len(db.recent_files(conn, limit=3)) == 3


def test_empty_query_shows_recent(qtbot, tmp_path):
    conn = _index_with_mtimes(tmp_path, [("a.pptx", 1_000_000), ("b.pptx", 2_000_000)])
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._do_search()
    assert win.result_list.count() == 2        # 空查询 → 展示最近文件（不再空白）
    assert win._showing_recent is True
    assert win._results[0].name == "b.pptx"    # 最新在前
