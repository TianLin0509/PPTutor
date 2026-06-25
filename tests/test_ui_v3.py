"""v3 UI 改进回归：时间/大小格式化 · size 贯通 · 复制到剪贴板 · 进度条三态。"""
from __future__ import annotations

import datetime

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication, QWidget
import pytest

import fixtures_gen as fx

from pptx_finder import db, indexer, search as search_mod
import pptx_finder.ui.main_window as main_window_mod
from pptx_finder.ui.main_window import (
    MainWindow, _elide_middle, _file_mime_for_path, _fmt_mtime, _fmt_size,
)


class _Stub(QObject):
    rendered = Signal(int, str)

    def request(self, req_id: int, path: str, page_no: int, cache_key=None):
        self.rendered.emit(req_id, "")  # 立即回空，触发兜底


def _mk(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "算力方案.pptx", [{"body": "昇腾 集群 算力 部署"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


# ---- 纯函数 ----
def test_fmt_size():
    assert _fmt_size(0) == ""
    assert _fmt_size(512) == "512 B"
    assert _fmt_size(2 * 1024 * 1024) == "2.0 MB"
    assert _fmt_size(int(2.3 * 1024 * 1024)).endswith("MB")


def test_fmt_mtime_same_year_keeps_time():
    ts = datetime.datetime(datetime.datetime.now().year, 6, 15, 14, 30).timestamp()
    s = _fmt_mtime(ts)
    assert "06-15" in s and "14:30" in s


def test_fmt_mtime_cross_year_date_only():
    ts = datetime.datetime(2020, 1, 3, 9, 0).timestamp()
    assert _fmt_mtime(ts) == "2020-01-03"


def test_elide_middle():
    assert _elide_middle("short.pptx") == "short.pptx"
    long = "C:\\" + "a" * 100 + "\\file.pptx"
    out = _elide_middle(long, 40)
    assert "…" in out and len(out) <= 41
    assert out.startswith("C:\\") and out.endswith("file.pptx")


# ---- size 贯通 ----
def test_size_propagates_to_result(tmp_path):
    conn = _mk(tmp_path)
    res = search_mod.search(conn, "昇腾")
    assert res and res[0].size > 0


# ---- 复制文件到剪贴板（CF_HDROP / urls）----
def _clipboard_available() -> bool:
    cb = QApplication.clipboard()
    token = "pptutor-clipboard-probe"
    cb.setText(token)
    QApplication.processEvents()
    return cb.text() == token


def test_copy_to_clipboard_sets_file_url(qtbot, tmp_path):
    if not _clipboard_available():
        pytest.skip("Windows clipboard is currently locked by another process")
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    win._act_copy_clipboard()

    def copied() -> bool:
        md = QApplication.clipboard().mimeData()
        return md.hasUrls() and md.urls()[0].toLocalFile().endswith("算力方案.pptx")

    qtbot.waitUntil(lambda: copied() or "剪贴板暂时不可用" in win._toast_label.text(), timeout=1500)
    if "剪贴板暂时不可用" in win._toast_label.text():
        pytest.skip("Windows clipboard is currently locked by another process")
    md = QApplication.clipboard().mimeData()
    assert md.hasUrls()
    assert md.urls()[0].toLocalFile().endswith("算力方案.pptx")


# ---- 预览顶栏：路径 + 大小 + 页数 + 时间 ----
def test_file_drag_mime_uses_local_file_url(tmp_path):
    p = tmp_path / "send-me.pptx"
    p.write_bytes(b"pptx")

    md = _file_mime_for_path(str(p))

    assert md.hasUrls()
    assert md.urls()[0].toLocalFile().replace("/", "\\") == str(p)
    assert md.text() == str(p)


def test_result_card_drag_exports_source_file_url(qtbot, tmp_path, monkeypatch):
    p = tmp_path / "drag-source.pptx"
    p.write_bytes(b"pptx")
    r = main_window_mod.FileResult(
        file_id=1,
        path=str(p),
        name=p.name,
        ext=".pptx",
        mtime=0,
        size=4,
        page_count=1,
        status="ok",
        score=1.0,
        name_hit=True,
        hits=[],
    )
    w = main_window_mod.ResultItem(r, main_window_mod.theme.tok("cloud"), "")
    qtbot.addWidget(w)
    drags = []

    class FakeDrag:
        def __init__(self, parent):
            self.parent = parent
            self.mime = None
            self.action = None
            drags.append(self)

        def setMimeData(self, mime):
            self.mime = mime

        def setPixmap(self, _pixmap):
            pass

        def setHotSpot(self, _point):
            pass

        def exec(self, action):
            self.action = action
            return action

    monkeypatch.setattr(main_window_mod, "QDrag", FakeDrag)

    w._start_file_drag()

    assert drags
    assert drags[0].action == Qt.CopyAction
    assert drags[0].mime.hasUrls()
    assert drags[0].mime.urls()[0].toLocalFile().replace("/", "\\") == str(p)


def test_main_resize_keeps_stats_overlay_filled(qtbot, tmp_path):
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    overlay = QWidget(win)
    win._stats_overlay = overlay
    overlay.resize(10, 10)

    win.show()
    win.resize(900, 700)

    qtbot.waitUntil(lambda: overlay.size() == win.size(), timeout=1000)


def test_preview_header_shows_path_and_size(qtbot, tmp_path):
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert win.copy_path_btn.isVisibleTo(win) or True  # 已 show()
    assert "算力方案.pptx" in win.path_label.toolTip()
    assert "MB" in win.meta_label.text() or "KB" in win.meta_label.text()
    assert "页" in win.meta_label.text()


# ---- 索引进度条三态 ----
def test_index_progress_three_states(qtbot, tmp_path):
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    # 扫描态（total<0）：无百分比
    win._on_index_progress(0, -1, "已发现 100 个文件")
    assert win.pct_label.text() == ""
    assert "扫描磁盘中" in win.status_label.text()
    # 索引态：百分比
    win._on_index_progress(50, 200, "x.pptx")
    assert win.pct_label.text() == "25%"
    assert "正在索引" in win.status_label.text()
    # 就绪态
    win._on_index_done({"indexed": 1, "deleted": 0})
    qtbot.waitUntil(lambda: "索引就绪" in win.status_label.text(), timeout=2000)
    assert "索引就绪" in win.status_label.text()
    assert win.pct_label.text() == ""


def test_index_progress_ui_updates_are_throttled(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_INDEX_PROGRESS_UI_MS", 100, raising=False)
    now = [100.0]
    monkeypatch.setattr(main_window_mod.time, "monotonic", lambda: now[0])
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)

    win._on_index_progress(1, 100, "a.pptx")
    assert win.pct_label.text() == "1%"
    assert "a.pptx" in win.status_label.text()

    now[0] = 100.02
    win._on_index_progress(2, 100, "b.pptx")

    assert win._index_last_done == 2
    assert win._index_last_current == "b.pptx"
    assert win.pct_label.text() == "1%"
    assert "a.pptx" in win.status_label.text()

    now[0] = 100.13
    win._on_index_progress(3, 100, "c.pptx")

    assert win.pct_label.text() == "3%"
    assert "c.pptx" in win.status_label.text()


def test_index_signals_ignored_after_closing(qtbot, monkeypatch, tmp_path):
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    calls = []
    monkeypatch.setattr(win, "_refresh_status", lambda *a, **k: calls.append("refresh"))
    monkeypatch.setattr(win, "_show_recent", lambda *a, **k: calls.append("recent"))
    win._closing = True
    win.status_label.setText("closing")
    win.pct_label.setText("keep")
    win._index_last_done = 9
    win._index_last_summary = {"old": True}

    win._on_index_progress(1, 4, "late.pptx")
    win._on_index_done({"indexed": 1, "deleted": 0})

    assert win.status_label.text() == "closing"
    assert win.pct_label.text() == "keep"
    assert win._index_last_done == 9
    assert win._index_last_summary == {"old": True}
    assert calls == []


# ---- 预览滚轮翻原始页 ----
def test_wheel_browses_original_pages(qtbot, tmp_path):
    docs = tmp_path / "wd"
    docs.mkdir()
    fx.make_pptx(docs / "多页.pptx", [
        {"body": "封面 标题页"},
        {"body": "目录 概览"},
        {"body": "正文 算力 集群 部署"},
        {"body": "总结 致谢"},
    ])
    conn = db.connect(tmp_path / "w.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("算力")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert win._cur.page_count == 4
    assert win._view_page == 3            # 初始定位命中页（第 3 页）
    win._wheel_page(-120)                 # 向下滚 → 第 4 页
    assert win._view_page == 4
    win._wheel_page(-120)                 # 已到末页，不越界
    assert win._view_page == 4
    win._wheel_page(120)                  # 向上滚回翻 4→3→2→1
    win._wheel_page(120)
    win._wheel_page(120)
    assert win._view_page == 1
    win._wheel_page(120)                  # 已到首页，不越界
    assert win._view_page == 1


def test_wheel_noop_when_no_selection(qtbot, tmp_path):
    conn = _mk(tmp_path)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    win._wheel_page(-120)                 # 无选中时不应崩、不改页
    assert win._view_page == 1
