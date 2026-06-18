"""入口注入单测：主窗口非侵入装上「胶片报告」入口 + 能弹出浮层。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QPushButton

from pptx_finder import db
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.report_overlay import ReportOverlay


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def _win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=1_700_000_000.0, content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    return win


def test_report_entry_button_in_topbar(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    btns = [b for b in win.findChildren(QPushButton) if b.text() == "🎞️"]
    assert len(btns) == 1


def test_status_label_clickable(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    assert win.status_label.cursor().shape() == Qt.PointingHandCursor


def test_open_report_shows_overlay(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry
    stats_entry._open_report(win)
    assert len(win.findChildren(ReportOverlay)) == 1
