"""v2.0 交互走查修复的回归测试（主窗口侧）。"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from pptx_finder import db
from pptx_finder.models import FileResult
from pptx_finder.ui.main_window import MainWindow


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def _win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    return win


def test_qt_dialogs_use_chinese_buttons(qapp):
    from pptx_finder.app import _install_qt_chinese
    assert _install_qt_chinese(qapp)
    box = QMessageBox(QMessageBox.Question, "t", "t", QMessageBox.Yes | QMessageBox.No)
    texts = {b.text().replace("&", "") for b in box.buttons()}
    assert any("是" in t for t in texts), texts


def test_title_shows_ampersand_literally(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    r = FileResult(file_id=1, path=str(tmp_path / "R&D <汇报>.pptx"), name="R&D <汇报>.pptx",
                   ext=".pptx", mtime=0, size=10, page_count=1, status="ok", score=1.0,
                   name_hit=True)
    win._update_preview_header(r)
    assert win.path_label.text() == "R&D <汇报>.pptx"
    assert win.path_label.textFormat() == Qt.PlainText


def test_health_locate_selects_exact_duplicate_by_path(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    seen = []
    win.preview_path_in_search = lambda p: seen.append(p)
    win._locate_health_item(r"D:\b\同名.pptx", "同名.pptx")
    assert seen == [r"D:\b\同名.pptx"]


def test_facet_empty_offers_clear_button(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._show_facet_empty()
    assert not win._clear_facet_btn.isHidden()
    win._show_empty_hint("abc")
    assert win._clear_facet_btn.isHidden()


def test_ctrl_c_on_results_copies_the_file(qtbot, tmp_path):
    from PySide6.QtGui import QKeySequence, QShortcut
    win = _win(qtbot, tmp_path)
    keys = {sc.key().toString(): sc for sc in win.result_list.findChildren(QShortcut)}
    copy = keys.get(QKeySequence(QKeySequence.Copy).toString())
    assert copy is not None and copy.context() == Qt.WidgetWithChildrenShortcut
    assert "Ctrl+Shift+C" in keys
    # 选中结果时：Ctrl+C 走「复制文件本体」
    called = []
    win._cur = type("R", (), {"path": str(tmp_path / "a.pptx")})()
    (tmp_path / "a.pptx").write_bytes(b"x")
    win._set_file_clipboard = lambda p: called.append(p)
    win._block_if_search_pending = lambda: False
    win._block_if_file_op_active = lambda: False
    copy.activated.emit()
    qtbot.waitUntil(lambda: bool(called), timeout=2000)


def test_dashboard_kpi_cards_are_entrances(qtbot, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QFrame
    win = _win(qtbot, tmp_path)
    cards = [w for w in win.dashboard.findChildren(QFrame) if w.objectName() == "dashCardLink"]
    assert len(cards) >= 5  # 4 张 KPI + 健康横条
    for c in cards:
        assert c.cursor().shape() == Qt.PointingHandCursor
    qtbot.mouseClick(cards[0], Qt.LeftButton, pos=QPoint(5, 5))
    assert win._current_page_key() == "search"


def test_folder_label_keeps_same_named_folders_apart():
    from pptx_finder.ui.dashboard_view import _folder_label
    assert _folder_label(r"D:\项目A\汇报\a.pptx") == "项目A/汇报"
    assert _folder_label(r"D:\项目B\汇报\b.pptx") == "项目B/汇报"


def test_dashboard_charts_paint_with_empty_week_and_single_folder(qtbot, tmp_path):
    """回归：空周时趋势图曾在同一控件上开第二支 QPainter，进程直接崩溃。"""
    win = _win(qtbot, tmp_path)
    dv = win.dashboard
    dv._week = [0] * 7
    dv._folders = [("项目A/汇报", 3)]
    dv._topics = [("本周", 0), ("半年内", 3)]
    dv.resize(1100, 800)
    for chart in (dv._spark, dv._hbars, dv._donut):
        chart.resize(400, 200)
        img = chart.grab()
        assert not img.isNull()
