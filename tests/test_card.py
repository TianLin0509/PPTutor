"""05 结果卡片化 + 缩略图懒加载。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from test_ui import StubRender, _index

from pptx_finder import renderer
from pptx_finder.models import FileResult, SearchHit
from pptx_finder.ui import theme
from pptx_finder.ui.main_window import MainWindow, ResultItem
from pptx_finder.ui.thumb_worker import ThumbWorker, _STOP


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


def test_thumb_worker_stop_discards_pending_before_stop_signal():
    tw = ThumbWorker()
    tw.request("a", 1)
    tw.request("b", 2)

    tw.stop()

    assert tw._q.get_nowait()[2] is _STOP
    assert tw._q.empty()
    assert tw._queued == set()
    assert tw._queued_priority == {}


def test_thumb_worker_upgrades_queued_duplicate_priority():
    tw = ThumbWorker()

    tw.request("a.pptx", 1, priority=90)
    tw.request("a.pptx", 1, priority=5)

    first = tw._q.get_nowait()
    assert first[0] == 5
    assert first[2] == ("a.pptx", 1)
    assert tw._queued_priority[("a.pptx", 1)] == 5
    lines = "\n".join(tw.diagnostic_lines())
    assert "queued=1" in lines
    assert "upgraded=1" in lines
    assert "completed=0/2" in lines


def test_thumb_worker_diagnostics_track_clear_and_dedupe():
    tw = ThumbWorker()

    tw.request("a.pptx", 1, priority=5)
    tw.request("a.pptx", 1, priority=50)
    tw.request("b.pptx", 2, priority=60)
    tw.clear()

    lines = "\n".join(tw.diagnostic_lines())
    assert "queued=0" in lines
    assert "deduped=1" in lines
    assert "cleared=2" in lines


def test_thumb_worker_coalesces_inflight_duplicate_requests(qtbot, monkeypatch, tmp_path):
    calls = []
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def fake_render(path, page_no, cache_key=None, long_edge=480, hi_priority=False, priority=None):
        with lock:
            calls.append((path, page_no, long_edge))
        started.set()
        release.wait(1)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    tw = ThumbWorker()
    tw.start()
    try:
        tw.request("a.pptx", 1)
        assert started.wait(1)
        tw.request("a.pptx", 1)
        release.set()
        qtbot.wait(250)

        with lock:
            got = list(calls)
        assert got == [("a.pptx", 1, 480)]
    finally:
        release.set()
        tw.stop()
        tw.wait(3000)


def test_thumb_worker_reuses_cached_large_preview_before_com(qtbot, monkeypatch, tmp_path):
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"png")
    calls: list[tuple[str, int]] = []

    def fake_cached(path, page_no, cache_key=None, min_long_edge=1):
        calls.append((path, page_no))
        return cached

    def fail_render(*_args, **_kwargs):
        raise AssertionError("thumbnail should reuse cached preview")

    monkeypatch.setattr(renderer, "find_cached_render", fake_cached)
    monkeypatch.setattr(renderer, "render_page", fail_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    tw = ThumbWorker()
    tw.start()
    try:
        with qtbot.waitSignal(tw.thumb_rendered, timeout=1000) as rendered:
            tw.request("a.pptx", 1, priority=5)

        assert rendered.args == ["a.pptx", 1, str(cached)]
        assert calls == [("a.pptx", 1)]
    finally:
        tw.stop()
        tw.wait(3000)


def test_thumb_worker_render_error_emits_failure_and_worker_survives(qtbot, monkeypatch, tmp_path):
    calls: list[tuple[str, int]] = []

    def fake_render(path, page_no, cache_key=None, long_edge=480, hi_priority=False, priority=None):
        calls.append((path, page_no))
        if path == "bad.pptx":
            raise RuntimeError("thumbnail render failed")
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    tw = ThumbWorker()
    tw.start()
    try:
        with qtbot.waitSignal(tw.thumb_rendered, timeout=1000) as failed:
            tw.request("bad.pptx", 1)
        assert failed.args == ["bad.pptx", 1, ""]

        with qtbot.waitSignal(tw.thumb_rendered, timeout=1000) as recovered:
            tw.request("ok.pptx", 2)
        assert recovered.args == ["ok.pptx", 2, str(tmp_path / "2.png")]
        assert calls == [("bad.pptx", 1), ("ok.pptx", 2)]
    finally:
        tw.stop()
        tw.wait(3000)


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
    win._thumb_items[("C:/x.pptx", 1)] = None
    win._on_thumb("C:/x.pptx", 1, str(png))
    assert ("C:/x.pptx", 1) in win._thumb_cache
