"""05 结果卡片化 + 缩略图懒加载。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from test_ui import StubRender, _index

from pptx_finder.models import FileResult, SearchHit
from pptx_finder.ui import theme
from pptx_finder.ui.main_window import MainWindow, ResultItem
from pptx_finder.ui.thumb_worker import ThumbWorker


def _fr(path="C:/a.pptx", hits=None):
    return FileResult(file_id=1, path=path, name=path.split("/")[-1], ext=".pptx",
                      mtime=0, size=1, page_count=5, status="ok", score=1,
                      name_hit=False, hits=hits or [])


class StubThumb(QObject):
    """假缩略图 worker：记录请求，不真渲染。"""
    thumb_rendered = Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self.requests: list[tuple[str, int]] = []

    def request(self, path, page):
        self.requests.append((path, page))

    def clear(self):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def test_thumb_page_prefers_hit(qtbot):
    it = ResultItem(_fr(hits=[SearchHit(3, "a【b】c")]), theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(it)
    assert it.thumb_page == 3


def test_thumb_page_default_first(qtbot):
    it = ResultItem(_fr(hits=[]), theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(it)
    assert it.thumb_page == 1


def test_set_thumbnail(qtbot):
    it = ResultItem(_fr(), theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(it)
    pm = QPixmap(96, 72)
    pm.fill()
    it.set_thumbnail(pm)
    assert not it._thumb.pixmap().isNull()


def test_thumb_worker_clear(qtbot):
    tw = ThumbWorker()
    tw.request("a", 1)
    tw.request("b", 2)
    tw.clear()
    assert tw._q.empty()


def test_mainwindow_requests_thumbs_on_search(qtbot, tmp_path):
    conn = _index(tmp_path)
    stub = StubThumb()
    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=stub, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    assert len(stub.requests) >= 1            # 搜索后为可见结果请求了缩略图


def test_mainwindow_on_thumb_caches(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    png = tmp_path / "t.png"
    pm = QPixmap(96, 72)
    pm.fill()
    pm.save(str(png))
    win._on_thumb("C:/x.pptx", 1, str(png))
    assert ("C:/x.pptx", 1) in win._thumb_cache
