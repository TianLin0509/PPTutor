"""方向 04：搜索历史 —— 最近搜索词去重、提前、截断。"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent

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


def _stub_history(monkeypatch):
    monkeypatch.setattr(history, "load_history", lambda limit=10, base=None: ["算力", "昇腾"])


def test_focusin_pops_history_only_on_mouse_reason(qtbot, tmp_path, monkeypatch):
    """焦点粘在搜索框时，窗口激活/浮层归还产生的 FocusIn 不再自动弹历史；
    只有真正鼠标点进框里（MouseFocusReason）才弹。"""
    _stub_history(monkeypatch)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    popup = win._completer.popup()

    win.eventFilter(win.search_box, QFocusEvent(QEvent.FocusIn, Qt.ActiveWindowFocusReason))
    assert not popup.isVisible()

    win.eventFilter(win.search_box, QFocusEvent(QEvent.FocusIn, Qt.PopupFocusReason))
    assert not popup.isVisible()

    win.eventFilter(win.search_box, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
    assert popup.isVisible()


def test_focus_search_pops_history_explicitly(qtbot, tmp_path, monkeypatch):
    """Ctrl+L / 全局热键显式唤起路径：空框聚焦后仍补弹历史下拉。"""
    _stub_history(monkeypatch)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()

    win.focus_search()

    assert win._completer.popup().isVisible()
