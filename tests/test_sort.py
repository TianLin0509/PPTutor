"""方向 07：结果排序切换 —— 相关度(默认)/最近修改/文件名。"""
from __future__ import annotations

from test_recent import _index_with_mtimes
from test_ui import StubRender

from pptx_finder.models import FileResult
from pptx_finder.ui.main_window import MainWindow, _sort_results


def _fr(name: str, mtime: float, score: float) -> FileResult:
    return FileResult(
        file_id=1, path=f"C:/{name}", name=name, ext=".pptx", mtime=mtime,
        size=1, page_count=1, status="ok", score=score, name_hit=False)


def test_sort_by_recent():
    rs = [_fr("a", 100, 0.9), _fr("b", 300, 0.1), _fr("c", 200, 0.5)]
    assert [r.name for r in _sort_results(rs, "recent")] == ["b", "c", "a"]


def test_sort_by_name():
    rs = [_fr("banana", 1, 0), _fr("Apple", 2, 0), _fr("cherry", 3, 0)]
    assert [r.name for r in _sort_results(rs, "name")] == ["Apple", "banana", "cherry"]


def test_sort_relevance_keeps_original_order():
    rs = [_fr("a", 100, 0.9), _fr("b", 300, 0.1)]
    assert [r.name for r in _sort_results(rs, "relevance")] == ["a", "b"]


def test_ui_sort_switch(qtbot, tmp_path):
    conn = _index_with_mtimes(tmp_path, [
        ("old.pptx", 1_000_000), ("new.pptx", 3_000_000), ("mid.pptx", 2_000_000)])
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._do_search()
    assert win._results[0].name == "new.pptx"            # recent 视图默认 mtime 序
    win.sort_combo.setCurrentText("文件名")
    assert [r.name for r in win._results] == ["mid.pptx", "new.pptx", "old.pptx"]
