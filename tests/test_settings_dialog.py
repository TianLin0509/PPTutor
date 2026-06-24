"""设置面板：全盘自动后只剩说明 + 自启开关，能正常构造。

conftest 已把 PPTX_FINDER_DATA_DIR 指向临时目录，不碰生产 vault。
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

import pptx_finder.ui.settings_dialog as settings_dialog_mod
from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder import config
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture
def mgr():
    m = VersionManager()
    yield m
    m.stop()


def test_settings_builds_with_autostart_toggle(qtbot, mgr):
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "data_dir:" in dlg.diagnostic_text.toPlainText(), timeout=1000)
    assert dlg.auto is not None          # 自启开关存在
    assert "守护" in dlg.stat.text()       # 显示已守护文件数
    assert dlg.tabs.count() == 3
    assert "data_dir:" in dlg.diagnostic_text.toPlainText()
    assert "index:" in dlg.diagnostic_text.toPlainText()
    assert dlg.rescan_btn.isEnabled() is False


def test_autostart_toggle_persists_preference(qtbot, mgr, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    calls = []
    monkeypatch.setattr(settings_dialog_mod.autostart, "set_enabled", lambda on: calls.append(on) or True)

    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    assert dlg.auto.isChecked() is True

    dlg.auto.setChecked(False)

    assert config.get_autostart() is False
    assert calls == [False]


def test_health_rescan_button_invokes_callback(qtbot, mgr):
    calls = []
    dlg = SettingsDialog(mgr, on_rescan=lambda: calls.append("rescan"))
    qtbot.addWidget(dlg)

    dlg.tabs.setCurrentIndex(1)
    assert dlg.rescan_btn.text() == "重新扫描索引"
    assert dlg.rescan_btn.isEnabled()

    qtbot.mouseClick(dlg.rescan_btn, Qt.LeftButton)

    assert calls == ["rescan"]


def test_health_rescan_reports_already_running_when_callback_rejects(qtbot, mgr):
    dlg = SettingsDialog(mgr, on_rescan=lambda: False)
    qtbot.addWidget(dlg)

    qtbot.mouseClick(dlg.rescan_btn, Qt.LeftButton)

    text = dlg.diagnostic_text.toPlainText()
    assert "rescan: already running" in text
    assert "rescan: requested in background" not in text


def test_health_diagnostics_includes_search_worker_metrics(qtbot, mgr):
    class FakeSearchWorker:
        def diagnostic_lines(self):
            return [
                "search: total=3 slow=1 interrupted=1",
                "search_last: 850 ms · fast",
            ]

    parent = QWidget()
    parent._search_worker = FakeSearchWorker()

    dlg = SettingsDialog(mgr, parent)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "search: total=3" in dlg.diagnostic_text.toPlainText(), timeout=1000)

    text = dlg.diagnostic_text.toPlainText()
    assert "search: total=3 slow=1 interrupted=1" in text
    assert "search_last: 850 ms" in text


def test_health_diagnostics_includes_update_metrics(qtbot, mgr):
    class FakeUpdater:
        def diagnostic_lines(self):
            return ["update: state=idle check=failed error=TimeoutError: timed out"]

    parent = QWidget()
    parent._updater = FakeUpdater()

    dlg = SettingsDialog(mgr, parent)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "update: state=idle" in dlg.diagnostic_text.toPlainText(), timeout=1000)

    text = dlg.diagnostic_text.toPlainText()
    assert "update: state=idle check=failed" in text
    assert "TimeoutError: timed out" in text


def test_health_diagnostics_includes_parent_ui_loop_metrics(qtbot, mgr):
    class FakeParent(QWidget):
        def diagnostic_lines(self):
            return ["ui_loop: samples=4 last_gap=10 ms max_gap=900 ms slow_gaps=1"]

    parent = FakeParent()

    dlg = SettingsDialog(mgr, parent)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "ui_loop: samples=4" in dlg.diagnostic_text.toPlainText(), timeout=1000)

    text = dlg.diagnostic_text.toPlainText()
    assert "ui_loop: samples=4" in text
    assert "max_gap=900 ms" in text


def test_settings_close_does_not_wait_for_powerpoint_diagnostic(qtbot, mgr, monkeypatch):
    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)

    class SlowDiagnosticTask:
        def wait(self, _ms):
            raise AssertionError("settings close must not wait in the UI thread")

    dlg._diag_tasks.append(SlowDiagnosticTask())

    dlg.close()


def test_settings_constructor_defers_heavy_diagnostics(qtbot, monkeypatch):
    calls = []
    scheduled = []

    class FakeMgr:
        def list_docs(self):
            calls.append("list_docs")
            return []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(
        settings_dialog_mod.db,
        "stats",
        lambda _conn: calls.append("stats") or {"file_count": 0, "page_count": 0},
    )

    dlg = SettingsDialog(FakeMgr())
    qtbot.addWidget(dlg)

    assert calls == []
    assert scheduled
    assert "诊断加载中" in dlg.diagnostic_text.toPlainText()

    scheduled.pop(0)[1]()
    qtbot.waitUntil(
        lambda: bool(calls) and "index:" in dlg.diagnostic_text.toPlainText() and not dlg._diag_tasks,
        timeout=1000,
    )


def test_settings_diagnostics_refresh_runs_heavy_work_in_background(qtbot, monkeypatch):
    calls = []
    scheduled = []
    tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

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

    class FakeMgr:
        def list_docs(self):
            calls.append("list_docs")
            return []

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        settings_dialog_mod.db,
        "stats",
        lambda _conn: calls.append("stats") or {"file_count": 0, "page_count": 0},
    )

    dlg = SettingsDialog(FakeMgr())
    qtbot.addWidget(dlg)

    assert calls == []
    assert scheduled

    scheduled.pop(0)[1]()

    assert calls == []
    assert tasks and tasks[-1].label == "settings-diagnostics"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert calls
    assert "index:" in dlg.diagnostic_text.toPlainText()


def test_settings_diagnostics_task_registered_for_parent_shutdown(qtbot, monkeypatch):
    scheduled = []
    tasks = []

    class Parent(QWidget):
        def __init__(self):
            super().__init__()
            self._bg_tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

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

    class FakeMgr:
        def list_docs(self):
            return []

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        settings_dialog_mod.db,
        "stats",
        lambda _conn: {"file_count": 0, "page_count": 0},
    )

    parent = Parent()
    qtbot.addWidget(parent)
    dlg = SettingsDialog(FakeMgr(), parent)
    qtbot.addWidget(dlg)

    scheduled.pop(0)[1]()
    task = tasks[-1]

    assert task in dlg._diag_tasks
    assert task in parent._bg_tasks

    task.finished.emit()

    assert task not in dlg._diag_tasks
    assert task not in parent._bg_tasks
    assert dlg._diag_inflight_token is None


def test_settings_diagnostics_refresh_reuses_inflight_task(qtbot, monkeypatch):
    scheduled = []
    tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

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
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class FakeMgr:
        def list_docs(self):
            return []

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        settings_dialog_mod.db,
        "stats",
        lambda _conn: {"file_count": 0, "page_count": 0},
    )

    dlg = SettingsDialog(FakeMgr())
    qtbot.addWidget(dlg)

    scheduled.pop(0)[1]()
    assert len(tasks) == 1

    scheduled_count = len(scheduled)
    dlg.schedule_diagnostics_refresh()
    for _delay_ms, callback in scheduled[scheduled_count:]:
        callback()
    scheduled_count = len(scheduled)
    dlg.schedule_diagnostics_refresh()
    for _delay_ms, callback in scheduled[scheduled_count:]:
        callback()

    assert [task.label for task in tasks] == ["settings-diagnostics"]

    result = tasks[0].fn()
    tasks[0].done.emit(result)
    tasks[0].finished.emit()
    assert not dlg._diag_tasks

    dlg.schedule_diagnostics_refresh()
    scheduled.pop(0)[1]()

    assert [task.label for task in tasks] == ["settings-diagnostics", "settings-diagnostics"]


def test_settings_rescan_message_survives_inflight_diagnostics(qtbot, monkeypatch):
    scheduled = []
    tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

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
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class FakeMgr:
        def list_docs(self):
            return []

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)

    dlg = SettingsDialog(FakeMgr(), on_rescan=lambda: False)
    qtbot.addWidget(dlg)

    scheduled.pop(0)[1]()
    assert len(tasks) == 1

    scheduled_count = len(scheduled)
    dlg._request_rescan()
    for _delay_ms, callback in scheduled[scheduled_count:]:
        callback()

    assert [task.label for task in tasks] == ["settings-diagnostics"]

    tasks[0].done.emit({"lines": ["baseline"], "guarded_docs": 0})
    tasks[0].finished.emit()

    text = dlg.diagnostic_text.toPlainText()
    assert "baseline" in text
    assert "rescan: already running" in text
    assert dlg._diag_extra_lines == []


def test_settings_rescan_error_survives_inflight_diagnostics(qtbot, monkeypatch):
    scheduled = []
    tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

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
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class FakeMgr:
        def list_docs(self):
            return []

    def failing_rescan():
        raise RuntimeError("scan boom")

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)

    dlg = SettingsDialog(FakeMgr(), on_rescan=failing_rescan)
    qtbot.addWidget(dlg)

    scheduled.pop(0)[1]()
    assert len(tasks) == 1

    dlg._request_rescan()
    assert "rescan: failed (RuntimeError: scan boom)" in dlg.diagnostic_text.toPlainText()

    tasks[0].done.emit({"lines": ["baseline"], "guarded_docs": 0})
    tasks[0].finished.emit()

    text = dlg.diagnostic_text.toPlainText()
    assert "baseline" in text
    assert "rescan: failed (RuntimeError: scan boom)" in text
    assert dlg._diag_extra_lines == []


def test_settings_powerpoint_check_reuses_inflight_task(qtbot, monkeypatch):
    tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

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
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class FakeMgr:
        def list_docs(self):
            return []

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)

    dlg = SettingsDialog(FakeMgr())
    qtbot.addWidget(dlg)

    dlg._check_powerpoint()
    first_task = tasks[-1]
    dlg._check_powerpoint()
    dlg._check_powerpoint()

    ppt_tasks = [task for task in tasks if task.label == "powerpoint-diagnostic"]
    assert ppt_tasks == [first_task]

    first_task.done.emit("PowerPoint COM 可用，版本 16。")
    first_task.finished.emit()

    assert dlg.powerpoint_btn.isEnabled()
    assert "版本 16" in dlg.powerpoint_status.text()

    dlg._check_powerpoint()
    ppt_tasks = [task for task in tasks if task.label == "powerpoint-diagnostic"]
    assert ppt_tasks == [first_task, tasks[-1]]


def test_settings_powerpoint_task_registered_for_parent_shutdown(qtbot, monkeypatch):
    tasks = []

    class Parent(QWidget):
        def __init__(self):
            super().__init__()
            self._bg_tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

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

    class FakeMgr:
        def list_docs(self):
            return []

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(settings_dialog_mod, "BackgroundTask", FakeTask, raising=False)

    parent = Parent()
    qtbot.addWidget(parent)
    dlg = SettingsDialog(FakeMgr(), parent)
    qtbot.addWidget(dlg)

    dlg._check_powerpoint()
    task = tasks[-1]

    assert task.label == "powerpoint-diagnostic"
    assert task in dlg._diag_tasks
    assert task in parent._bg_tasks

    task.finished.emit()

    assert task not in dlg._diag_tasks
    assert task not in parent._bg_tasks


def test_settings_late_diagnostics_ignored_after_close(qtbot, mgr, monkeypatch):
    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)

    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.diagnostic_text.setPlainText("before-close")
    token = dlg._diag_refresh_token

    dlg.close()
    dlg._on_diagnostics_ready(token, {"lines": ["after-close"], "guarded_docs": 0})

    assert dlg.diagnostic_text.toPlainText() == "before-close"


def test_settings_late_diagnostics_ignored_after_owner_closing(qtbot, mgr, monkeypatch):
    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

    class Parent(QWidget):
        _closing = False

    parent = Parent()
    qtbot.addWidget(parent)
    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)

    dlg = SettingsDialog(mgr, parent)
    qtbot.addWidget(dlg)
    dlg.diagnostic_text.setPlainText("before-owner-close")
    token = dlg._diag_refresh_token

    parent._closing = True
    dlg._on_diagnostics_ready(token, {"lines": ["after-owner-close"], "guarded_docs": 0})

    assert dlg.diagnostic_text.toPlainText() == "before-owner-close"


def test_settings_late_powerpoint_result_ignored_after_close(qtbot, mgr, monkeypatch):
    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)

    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.powerpoint_btn.setEnabled(False)
    dlg.powerpoint_status.setText("checking")

    dlg.close()
    dlg._on_powerpoint_checked("ok")

    assert dlg.powerpoint_btn.isEnabled() is False
    assert dlg.powerpoint_status.text() == "checking"


def test_settings_late_powerpoint_result_ignored_after_owner_closing(qtbot, mgr, monkeypatch):
    class FakeTimer:
        @staticmethod
        def singleShot(_delay_ms, _callback):
            return None

    class Parent(QWidget):
        _closing = False

    parent = Parent()
    qtbot.addWidget(parent)
    monkeypatch.setattr(settings_dialog_mod, "QTimer", FakeTimer, raising=False)

    dlg = SettingsDialog(mgr, parent)
    qtbot.addWidget(dlg)
    dlg.powerpoint_btn.setEnabled(False)
    dlg.powerpoint_status.setText("checking")

    parent._closing = True
    dlg._on_powerpoint_checked("ok")

    assert dlg.powerpoint_btn.isEnabled() is False
    assert dlg.powerpoint_status.text() == "checking"
