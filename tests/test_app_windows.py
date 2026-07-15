from __future__ import annotations

from PySide6.QtWidgets import QWidget

from pptx_finder import app as app_mod
from pptx_finder import config


def test_closed_version_window_not_kept_before_reopen(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner._version_windows = []
    created = []

    class FakeVersionWindow(QWidget):
        def __init__(self, _manager):
            super().__init__()
            self._closing = False
            created.append(self)

    manager = object()

    app_mod._open_version_window(owner, manager, window_cls=FakeVersionWindow)

    assert len(owner._version_windows) == 1
    first = owner._version_windows[0]
    qtbot.addWidget(first)

    first.close()
    qtbot.waitUntil(lambda: not first.isVisible(), timeout=1000)
    app_mod._open_version_window(owner, manager, window_cls=FakeVersionWindow)

    assert len(owner._version_windows) == 1
    assert owner._version_windows[0] is not first
    assert owner._version_windows[0].isVisible()


def test_open_version_window_hands_owner_bg_tasks_to_window(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner._version_windows = []
    owner._bg_tasks = []

    class FakeVersionWindow(QWidget):
        def __init__(self, _manager):
            super().__init__()
            self._closing = False

    window = app_mod._open_version_window(owner, object(), window_cls=FakeVersionWindow)
    qtbot.addWidget(window)

    assert getattr(window, "_parent_bg_tasks", None) is owner._bg_tasks


def test_settings_dialog_from_app_receives_rescan_callback(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    manager = object()
    created = []

    def rescan():
        return True

    owner._request_full_rescan = rescan

    class FakeSettingsDialog:
        def __init__(self, _manager, parent=None, on_rescan=None):
            self.parent = parent
            self.on_rescan = on_rescan
            self.exec_called = False
            created.append(self)

        def exec(self):
            self.exec_called = True
            return 123

    result = app_mod._open_settings_dialog(
        owner,
        manager,
        dialog_cls=FakeSettingsDialog,
    )

    assert result == 123
    assert len(created) == 1
    assert created[0].parent is owner
    assert created[0].on_rescan is rescan
    assert created[0].exec_called is True


def test_sync_autostart_preference_enables_default(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    calls = []
    monkeypatch.setattr(app_mod.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(app_mod.autostart, "set_enabled", lambda on: calls.append(on) or True)

    assert config.get_autostart() is True
    assert app_mod._sync_autostart_preference() is True
    assert calls == [True]


def test_sync_autostart_preference_respects_user_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    config.set_autostart(False)
    calls = []
    monkeypatch.setattr(app_mod.autostart, "is_enabled", lambda: True)
    monkeypatch.setattr(app_mod.autostart, "set_enabled", lambda on: calls.append(on) or True)

    assert app_mod._sync_autostart_preference() is True
    assert calls == [False]


def test_singleton_name_can_be_isolated_for_frozen_smoke(monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_SINGLETON_NAME", "ppt-doctor-isolated-smoke")
    assert app_mod._singleton_name() == "ppt-doctor-isolated-smoke"

    monkeypatch.delenv("PPTX_FINDER_SINGLETON_NAME")
    assert app_mod._singleton_name() == app_mod.SINGLETON_NAME
