"""入口注入单测：主窗口非侵入装上「胶片报告」入口 + 能弹出浮层。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QPushButton

from pptx_finder import db
from pptx_finder import stats
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
    # 合一工具栏后报告入口是图标按钮（无文字），按 accessibleName / tooltip 识别
    btns = [b for b in win.findChildren(QPushButton) if b.accessibleName() == "打开胶片报告"]
    assert len(btns) == 1
    assert "胶片报告" in btns[0].toolTip()
    assert not btns[0].icon().isNull()


def test_status_label_clickable(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    assert win.status_label.cursor().shape() == Qt.PointingHandCursor


def test_open_report_shows_overlay(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry
    stats_entry._open_report(win)
    qtbot.waitUntil(lambda: len(win.findChildren(ReportOverlay)) == 1, timeout=1000)
    assert len(win.findChildren(ReportOverlay)) == 1


def test_report_overlay_close_clears_main_window_reference(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry

    stats_entry._open_report(win)
    qtbot.waitUntil(lambda: len(win.findChildren(ReportOverlay)) == 1, timeout=1000)

    ov = win._stats_overlay
    ov.close()

    qtbot.waitUntil(lambda: getattr(win, "_stats_overlay", None) is None, timeout=1000)
    qtbot.waitUntil(lambda: len(win.findChildren(ReportOverlay)) == 0, timeout=1000)


def test_open_report_builds_in_background(qtbot, tmp_path, monkeypatch):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry

    calls = []
    seen_conns = []
    tasks = []
    fake_report = stats.build_report(win._conn, year=None)

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    def fake_build_report(conn, *, year=None):
        calls.append(year)
        seen_conns.append(conn)
        return fake_report

    monkeypatch.setattr(stats_entry, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(stats_entry.stats, "build_report", fake_build_report)

    stats_entry._open_report(win)

    assert calls == []
    assert tasks and tasks[-1].label == "stats-report-build"
    assert len(win.findChildren(ReportOverlay)) == 0

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert calls == [None]
    assert seen_conns[0] is not win._conn
    assert len(win.findChildren(ReportOverlay)) == 1


def test_open_report_task_registered_for_main_window_shutdown(qtbot, tmp_path, monkeypatch):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry

    waits = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self._label = label
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            pass

        def wait(self, ms):
            waits.append((self.label, ms))
            return False

    monkeypatch.setattr(stats_entry, "BackgroundTask", FakeTask, raising=False)
    win._bg_tasks = []

    stats_entry._open_report(win)

    assert win._stats_report_tasks
    task = win._stats_report_tasks[-1]
    assert task in win._bg_tasks

    win._shutdown()

    assert ("stats-report-build", win._BG_LIGHT_SHUTDOWN_WAIT_MS) in waits


def test_open_report_reuses_existing_overlay(qtbot, tmp_path, monkeypatch):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry

    tasks = []

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeTask:
        done = FakeSignal()
        finished = FakeSignal()

        def __init__(self, *_args, **_kwargs):
            tasks.append(self)

        def start(self):
            tasks.append("started")

    class ExistingOverlay(QObject):
        def __init__(self):
            super().__init__()
            self.geometries = []
            self.shown = 0
            self.raised = 0
            self.activated = 0

        def setGeometry(self, rect):
            self.geometries.append(rect)

        def show(self):
            self.shown += 1

        def raise_(self):
            self.raised += 1

        def activateWindow(self):
            self.activated += 1

    overlay = ExistingOverlay()
    win._stats_overlay = overlay
    monkeypatch.setattr(stats_entry, "BackgroundTask", FakeTask, raising=False)

    stats_entry._open_report(win)

    assert tasks == []
    assert getattr(win, "_stats_report_loading", False) is False
    assert overlay.geometries
    assert overlay.shown == 1
    assert overlay.raised == 1
    assert overlay.activated == 1


def test_stats_report_late_result_ignored_after_main_window_closing(qtbot, tmp_path, monkeypatch):
    win = _win(qtbot, tmp_path)
    from pptx_finder.ui import stats_entry

    tasks = []
    overlays = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class FakeOverlay:
        def __init__(self, *args, **kwargs):
            overlays.append((args, kwargs))

        def setGeometry(self, *_args):
            pass

        def show(self):
            pass

        def raise_(self):
            pass

    monkeypatch.setattr(stats_entry, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(stats_entry, "ReportOverlay", FakeOverlay, raising=False)

    stats_entry._open_report(win)

    assert tasks and tasks[-1].label == "stats-report-build"
    assert win._stats_report_loading is True

    win._closing = True
    tasks[-1].done.emit(stats.build_report(win._conn, year=None))

    assert overlays == []
    assert getattr(win, "_stats_overlay", None) is None
    assert win._stats_report_loading is False
