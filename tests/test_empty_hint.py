"""方向 02：零结果引导 —— 搜不到时给提示 + 可点补救建议。"""
from __future__ import annotations

from test_ui import StubRender, _index

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


def test_zero_result_shows_hint(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    assert win.result_list.count() == 0
    assert not win.empty_hint.isHidden()    # 引导显示
    assert win.result_list.isHidden()       # 列表让位


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
