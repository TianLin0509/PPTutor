"""文件类型选择器 UI：type_filter 下拉过滤搜索的文件类型，默认 PPT。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from pptx_finder import db
from pptx_finder.ui.main_window import MainWindow


class _StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, req_id, path, page_no, cache_key=None):
        self.rendered.emit(req_id, "")


def _win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=_StubRender(), do_index=False)
    qtbot.addWidget(win)
    return win


def test_type_filter_default_ppt_and_exts_mapping(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)

    assert win.type_filter.currentText() == "PPT"  # 默认 PPT
    assert win._search_exts() == (".pptx", ".ppt")

    win.type_filter.setCurrentIndex(win.type_filter.findText("全部"))
    assert win._search_exts() is None  # 全部 = 不过滤

    win.type_filter.setCurrentIndex(win.type_filter.findText("Word"))
    assert win._search_exts() == (".docx",)

    win.type_filter.setCurrentIndex(win.type_filter.findText("PDF"))
    assert win._search_exts() == (".pdf",)


def test_type_filter_change_reruns_search(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    seen = []
    win._do_search = lambda: seen.append(1)

    win.type_filter.setCurrentIndex(win.type_filter.findText("全部"))

    assert seen == [1]  # 切换文件类型触发重新搜索
