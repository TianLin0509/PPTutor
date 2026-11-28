"""Resource regressions: bounded full-quality thumbnails and save-burst scheduling."""
from types import SimpleNamespace

from PySide6.QtGui import QImage

from pptx_finder.ui import material_tab as mt
from pptx_finder.versioning import watcher


def entry(key):
    return mt.Entry(key, key, "test", "test", SimpleNamespace(id=key))


def image():
    result = QImage(64, 64, QImage.Format_RGB32)
    result.fill(0xff3478f6)
    return result


def test_thumbnail_cache_evicts_least_recent_without_resizing():
    cache = mt.ThumbCache(None)
    cache.MAX_BYTES = 2 * 64 * 64 * 4
    cache.MAX_IMAGES = 100
    cache.store({"a": image(), "b": image()})
    assert cache.get(entry("a")).size().width() == 64
    cache.store({"c": image()})
    assert cache.get(entry("b")) is None
    assert cache.get(entry("a")) is not None
    assert cache.get(entry("c")) is not None


def test_thumbnail_cache_pins_current_view_and_releases_previous_page():
    cache = mt.ThumbCache(None)
    cache.MAX_BYTES = 1
    cache.set_pinned({"a", "b"})
    cache.store({"a": image(), "b": image()})
    assert all(cache.get(entry(key)) is not None for key in ("a", "b"))
    cache.set_pinned({"c"})
    cache.store({"c": image()})
    assert list(cache._images) == ["c"]
    assert cache.get(entry("c")).width() == 64
    cache.clear()
    assert not cache._images and not cache._missing
    assert cache._bytes == 0


def test_thumbnail_cache_missing_and_small_images_are_bounded():
    cache = mt.ThumbCache(None)
    cache.MAX_IMAGES = 2
    cache.MAX_MISSING = 2
    cache.store({str(i): image() for i in range(20)})
    cache.store({"missing" + str(i): None for i in range(20)})
    assert len(cache._images) == 2
    assert len(cache._missing) == 2


def test_thumbnail_decode_deduplicates_inflight_and_rejects_stale_generation(qtbot, monkeypatch):
    monkeypatch.setattr(mt.MaterialTabWidget, "_default_center", staticmethod(lambda: None))
    monkeypatch.setattr(mt.MaterialTabWidget, "_load", lambda self: {
        "team": None, "imported": [], "mine": [], "personal_bytes": 0})
    tab = mt.MaterialTabWidget()
    qtbot.addWidget(tab)
    qtbot.waitUntil(lambda: tab._loaded and not tab._tasks)
    tasks = []
    class Signal:
        def __init__(self): self.slots = []
        def connect(self, slot): self.slots.append(slot)
        def emit(self, *args):
            for slot in self.slots: slot(*args)
    class Task:
        def __init__(self, fn, *args):
            self.fn = fn
            self.done, self.finished = Signal(), Signal()
        def start(self): tasks.append(self)
        def wait(self, *args): return True
    monkeypatch.setattr(mt, "BackgroundTask", Task)
    tab._view = [entry("a")]
    tab.grid.set_entries(tab._view)
    tab._decode_visible()
    tab.grid.selected = 0
    tab._decode_visible()
    assert len(tasks) == 1, "paging and selection must share an in-flight decode"
    tab._load_gen += 1
    tab.cache.clear()
    tab._decode_visible()
    assert len(tasks) == 2
    old_image = image()
    tasks[0].done.emit({"a": old_image})
    tasks[0].finished.emit()
    assert tab.cache.needs(entry("a")), "an old library must not repopulate the new cache"
    fresh_image = image()
    tasks[1].done.emit({"a": fresh_image})
    tasks[1].finished.emit()
    assert tab.cache.get(entry("a")) is fresh_image


def test_save_burst_does_not_rescan_every_pending_file(monkeypatch):
    class Timer:
        def __init__(self, *args): pass
        def start(self): pass
        def cancel(self): pass
    class CountingDict(dict):
        visits = 0
        def values(self):
            self.visits += len(self)
            return super().values()
    monkeypatch.setattr(watcher.threading, "Timer", Timer)
    handler = watcher._Handler(lambda path: None)
    handler._timers = CountingDict()
    for i in range(1000):
        handler._schedule(str(i), 1.5, 0)
    assert len(handler._timers) == 1000
    assert handler._timers.visits <= 1000, "save-burst work must grow linearly"
    handler.stop()


def test_save_deadline_replacement_and_earlier_retry_preserve_all_events(monkeypatch):
    clock = [100.0]
    created, fired = [], []
    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.cancelled = False
            created.append(self)
        def start(self): pass
        def cancel(self): self.cancelled = True
    monkeypatch.setattr(watcher.threading, "Timer", Timer)
    monkeypatch.setattr(watcher.time, "monotonic", lambda: clock[0])
    handler = watcher._Handler(lambda path: None)
    monkeypatch.setattr(handler, "_fire", lambda path, attempt, **kw: fired.append((path, attempt)))
    handler._schedule("a", 10, 0)
    handler._schedule("a", 20, 1)
    handler._schedule("b", 5, 2)
    assert created[0].cancelled
    assert handler._wake_deadline == 105
    clock[0] = 105
    created[-1].callback()
    assert fired == [("b", 2)]
    assert handler._wake_deadline == 120
    clock[0] = 120
    created[-1].callback()
    assert fired == [("b", 2), ("a", 1)]
    assert not handler._timers


def test_closed_report_releases_owner_reference_and_native_objects(qtbot, tmp_path):
    import gc
    import weakref
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QWidget
    from pptx_finder import db, stats
    from pptx_finder.ui.report_overlay import ReportOverlay
    from pptx_finder.ui import theme
    conn = db.connect(tmp_path / "report.db")
    db.init_db(conn)
    parent = QWidget()
    qtbot.addWidget(parent)
    overlay = ReportOverlay(stats.build_report(conn, year=None), theme.tok("atelier"), parent)
    parent._stats_overlay = overlay
    native = overlay
    ref = weakref.ref(overlay)
    overlay.close()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert parent._stats_overlay is None
    assert not shiboken6.isValid(native)
    del native, overlay
    gc.collect()
    assert ref() is None
    conn.close()


def test_idle_main_window_has_no_preview_or_decorative_timer(qtbot, tmp_path):
    from PySide6.QtCore import QObject, QTimer, Signal
    from pptx_finder import db
    from pptx_finder.ui.main_window import MainWindow
    class Render(QObject):
        rendered = Signal(int, str)
        def request(self, *a, **kw): pass
    conn = db.connect(tmp_path / "idle.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=Render(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(300)
    active = [timer for timer in win.findChildren(QTimer)
              if timer.isActive() and not timer.isSingleShot() and timer.interval() < 250]
    assert all(timer is win._ui_loop_timer for timer in active)
    assert not win._spin_timer.isActive()
    win._to_tray_on_close = False
    win.close()
    conn.close()
