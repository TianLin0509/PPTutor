"""搜索范围选择器 UI：type_filter 决定搜哪些文件，默认 PPT，末位永远是「全部文件」。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from pptx_finder import db
from pptx_finder.ui.main_window import ALL_FILES_SCOPE_LABEL, MainWindow


class _StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, req_id, path, page_no, cache_key=None):
        self.rendered.emit(req_id, "")


def _win(qtbot, tmp_path, *, document_search_enabled=False):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(
        conn=conn,
        render_worker=_StubRender(),
        do_index=False,
        document_search_enabled=document_search_enabled,
    )
    qtbot.addWidget(win)
    return win


def _select(win, label):
    idx = win.type_filter.findText(label)
    assert idx >= 0, f"范围选择器里没有「{label}」"
    win.type_filter.setCurrentIndex(idx)


def test_type_filter_default_is_ppt_with_all_files_fallback(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)

    assert win.type_filter.currentText() == "PPT"  # 默认 PPT
    assert win._search_exts() == (".pptx", ".ppt")
    # 「全部文件」是常开能力，不跟随任何开关；但排在最后，不抢 PPT 的默认位
    assert [win.type_filter.itemText(i) for i in range(win.type_filter.count())] == [
        "PPT", ALL_FILES_SCOPE_LABEL,
    ]


def test_type_filter_advanced_document_mapping(qtbot, tmp_path):
    win = _win(qtbot, tmp_path, document_search_enabled=True)

    _select(win, "全部文档")
    assert win._search_exts() == (".pptx", ".ppt", ".docx", ".pdf")

    _select(win, "Word")
    assert win._search_exts() == (".docx",)

    _select(win, "PDF")
    assert win._search_exts() == (".pdf",)

    # 全部文件：不按扩展名过滤，且强制走「仅文件名」口径
    _select(win, ALL_FILES_SCOPE_LABEL)
    assert win._search_exts() is None
    assert win._mode_key() == "any_filename"


def test_type_filter_change_reruns_search(qtbot, tmp_path):
    win = _win(qtbot, tmp_path, document_search_enabled=True)
    seen = []
    win._do_search = lambda: seen.append(1)

    _select(win, "全部文档")

    assert seen == [1]  # 切换搜索范围触发重新搜索
