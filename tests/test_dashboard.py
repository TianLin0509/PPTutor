from __future__ import annotations

import pptx_finder.ui.dashboard_view as dashboard_mod
from pptx_finder import db
from pptx_finder.ui.dashboard_view import DashboardView


class FakeWindow:
    _conn = None
    _version_mgr = None
    _tok = {}
    _closing = False


def _payload():
    return {
        "kpi_vals": [("1", "docs", "ok"), ("0", "versions", ""), ("2", "pages", ""), ("0", "week", "")],
        "folders": [("A", 1)],
        "topics": [("A", 1)],
        "week": [0] * 7,
        "week_shield_text": "shield",
        "recent": [],
    }


def test_dashboard_refresh_throttles_repeated_recompute(qtbot, monkeypatch):
    calls = []

    def fake_build_payload(self, *_args, **_kwargs):
        calls.append("payload")
        return _payload()

    monkeypatch.setattr(DashboardView, "_build_payload", fake_build_payload)

    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    assert calls == []

    dash.refresh()
    dash.refresh()

    qtbot.waitUntil(lambda: calls == ["payload"] and not dash._refresh_tasks, timeout=1000)

    dash.refresh(force=True)

    qtbot.waitUntil(lambda: calls == ["payload", "payload"] and not dash._refresh_tasks, timeout=1000)


def test_dashboard_init_defers_expensive_recompute(qtbot, monkeypatch):
    calls = []

    def fake_recompute(self):
        calls.append("recompute")

    monkeypatch.setattr(DashboardView, "_recompute", fake_recompute)

    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    assert calls == []


def test_dashboard_labels_real_rollback_count_not_managed_doc_count(qtbot, tmp_path):
    class VersionManager:
        def summary_stats(self):
            return {
                "protected_docs": 3865,
                "total_versions": 4079,
                "rollback_docs": 99,
            }

    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    payload = dash._build_payload(fallback_conn=conn, version_mgr=VersionManager())

    assert payload["kpi_vals"][1] == (
        "99",
        "可回退 PPT",
        "已留版 3,865 · 共 4,079 版",
    )
    assert "3865 份" in payload["week_shield_text"]
    assert "99 份可回退" in payload["week_shield_text"]


def test_dashboard_scheduled_refresh_coalesces(qtbot, monkeypatch):
    calls = []

    def fake_build_payload(self, *_args, **_kwargs):
        calls.append("payload")
        return _payload()

    monkeypatch.setattr(DashboardView, "_build_payload", fake_build_payload)

    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    dash.schedule_refresh()
    dash.schedule_refresh()

    assert calls == []
    qtbot.waitUntil(lambda: calls == ["payload"] and not dash._refresh_tasks, timeout=1000)


def test_dashboard_refresh_recomputes_in_background(qtbot, monkeypatch):
    tasks = []
    calls = []

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

    def fake_build_payload(self, *_args, **_kwargs):
        calls.append("payload")
        return _payload()

    monkeypatch.setattr(dashboard_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(DashboardView, "_build_payload", fake_build_payload, raising=False)

    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    dash.refresh(force=True)

    assert calls == []
    assert tasks and tasks[-1].label == "dashboard-refresh"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert calls == ["payload"]
    assert dash._kpi_vals[0][0] == "1"


def test_dashboard_refresh_task_registered_for_parent_shutdown(qtbot, monkeypatch):
    tasks = []

    class Window(FakeWindow):
        def __init__(self):
            self._bg_tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self._label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(dashboard_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(DashboardView, "_build_payload", lambda self, *_args, **_kwargs: _payload(), raising=False)

    win = Window()
    dash = DashboardView(win)
    qtbot.addWidget(dash)

    dash.refresh(force=True)
    task = tasks[-1]

    assert task in dash._refresh_tasks
    assert task in win._bg_tasks

    task.finished.emit()

    assert task not in dash._refresh_tasks
    assert task not in win._bg_tasks
    assert dash._refresh_inflight_token is None


def test_dashboard_force_refresh_reuses_inflight_task(qtbot, monkeypatch):
    tasks = []
    calls = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                if value is None:
                    callback()
                else:
                    callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    def fake_build_payload(self, *_args, **_kwargs):
        calls.append("payload")
        return _payload()

    monkeypatch.setattr(dashboard_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(DashboardView, "_build_payload", fake_build_payload, raising=False)

    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    dash.refresh(force=True)
    first_task = tasks[-1]
    dash.refresh(force=True)
    dash.refresh(force=True)

    refresh_tasks = [task for task in tasks if task.label == "dashboard-refresh"]
    assert refresh_tasks == [first_task]
    assert calls == []

    first_task.done.emit(first_task.fn())
    first_task.finished.emit()

    assert calls == ["payload"]
    assert dash._kpi_vals[0][0] == "1"


def test_dashboard_force_refresh_supersedes_nonforce_inflight(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                if value is None:
                    callback()
                else:
                    callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(dashboard_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(DashboardView, "_build_payload", lambda self, *_args, **_kwargs: _payload(), raising=False)

    dash = DashboardView(FakeWindow())
    qtbot.addWidget(dash)

    dash.refresh()
    nonforce_task = tasks[-1]
    dash.refresh(force=True)
    force_task = tasks[-1]

    assert force_task is not nonforce_task


def test_dashboard_scheduled_refresh_ignored_after_parent_closing(qtbot, monkeypatch):
    scheduled = []
    tasks = []

    class Window(FakeWindow):
        _closing = False

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(dashboard_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(dashboard_mod, "BackgroundTask", FakeTask, raising=False)

    win = Window()
    dash = DashboardView(win)
    qtbot.addWidget(dash)

    dash.schedule_refresh()
    win._closing = True
    scheduled[-1][1]()

    assert tasks == []


def test_dashboard_late_payload_ignored_after_parent_closing(qtbot, monkeypatch):
    tasks = []
    applied = []

    class Window(FakeWindow):
        _closing = False

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

    monkeypatch.setattr(dashboard_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(DashboardView, "_apply_payload", lambda self, payload: applied.append(payload))

    win = Window()
    dash = DashboardView(win)
    qtbot.addWidget(dash)

    dash.refresh(force=True)

    assert tasks and tasks[-1].label == "dashboard-refresh"

    win._closing = True
    tasks[-1].done.emit(_payload())

    assert applied == []
