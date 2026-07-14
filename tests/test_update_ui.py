from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QPushButton

from pptx_finder import updater
from pptx_finder.ui import update_ui
from pptx_finder.ui.update_ui import UpdateController


def test_update_ready_click_is_single_shot(monkeypatch, qtbot, tmp_path):
    chip = QPushButton()
    qtbot.addWidget(chip)
    applies = []
    quits = []

    def fake_apply_update(staging, dest, info, relaunch):
        applies.append((staging, dest, info, relaunch))

    monkeypatch.setattr(update_ui.updater, "apply_update", fake_apply_update)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: quits.append("quit"))
    ctrl._info = updater.UpdateInfo(version="0.9.1", notes="", changed=[], deleted=[])
    ctrl._staging = tmp_path / "staging"
    ctrl._state = "downloading"
    ctrl._on_done()

    qtbot.mouseClick(chip, Qt.LeftButton)

    assert len(applies) == 1
    assert quits == ["quit"]
    assert ctrl._state == "applying"
    assert chip.isEnabled() is False

    qtbot.mouseClick(chip, Qt.LeftButton)

    assert len(applies) == 1
    assert quits == ["quit"]


def test_update_late_progress_ignored_after_download_ready(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._state = "downloading"
    ctrl._on_done()
    ready_text = chip.text()

    ctrl._on_progress(90)

    assert ctrl._state == "ready"
    assert chip.text() == ready_text


def test_update_progress_clamps_percent_range(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._state = "downloading"

    ctrl._on_progress(135)

    assert "100%" in chip.text()

    ctrl._on_progress(-12)

    assert "0%" in chip.text()


def test_update_failure_chip_shows_short_reason_and_retries(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._state = "downloading"
    retries = []

    def retry_download():
        retries.append("retry")
        ctrl._state = "downloading"

    ctrl._start_download = retry_download

    ctrl._on_failed("ValueError: 哈希校验失败 app.exe：期望 abc 实得 def")

    assert ctrl._state == "error"
    assert chip.isEnabled()
    assert "校验失败" in chip.text()
    assert "重试" in chip.text()
    assert "哈希校验失败 app.exe" in chip.toolTip()
    lines = "\n".join(ctrl.diagnostic_lines())
    assert "download_error=ValueError: 哈希校验失败 app.exe" in lines

    qtbot.mouseClick(chip, Qt.LeftButton)

    assert retries == ["retry"]
    assert ctrl._state == "downloading"


def test_update_late_failure_ignored_after_download_ready(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._state = "downloading"
    ctrl._on_done()
    ready_text = chip.text()

    ctrl._on_failed("TimeoutError: late timeout")

    assert ctrl._state == "ready"
    assert chip.text() == ready_text
    assert ctrl._download_error == ""


def test_update_retry_ignores_late_failure_from_previous_download(monkeypatch, qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    downloads = []

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

    class FakeDownloadThread:
        def __init__(self, *_args, **_kwargs):
            self.progress = FakeSignal()
            self.done = FakeSignal()
            self.failed = FakeSignal()
            downloads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(update_ui, "_DownloadThread", FakeDownloadThread)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._info = updater.UpdateInfo(version="0.9.2", notes="", changed=[], deleted=[])
    ctrl._state = "available"

    qtbot.mouseClick(chip, Qt.LeftButton)
    first = downloads[-1]
    first.failed.emit("TimeoutError: first timeout")
    assert ctrl._state == "error"

    qtbot.mouseClick(chip, Qt.LeftButton)
    second = downloads[-1]
    assert second is not first
    assert ctrl._state == "downloading"
    downloading_text = chip.text()

    first.failed.emit("TimeoutError: late first timeout")

    assert ctrl._state == "downloading"
    assert chip.text() == downloading_text
    assert ctrl._download_error == ""


def test_update_check_failure_is_silent_but_diagnostic(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._state = "checking"

    ctrl._on_check_failed("TimeoutError: timed out")

    assert ctrl._state == "idle"
    assert chip.isHidden()
    lines = "\n".join(ctrl.diagnostic_lines())
    assert "update: state=idle check=failed" in lines
    assert "TimeoutError: timed out" in lines


def test_update_late_checked_ignored_after_update_found(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    info = updater.UpdateInfo(version="0.9.2", notes="new", changed=[], deleted=[])
    ctrl._state = "checking"
    ctrl._on_found(info)

    ctrl._on_check_done()

    assert ctrl._state == "available"
    assert chip.isVisible()
    assert "0.9.2" in chip.text()


def test_update_late_check_failed_ignored_after_update_found(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    info = updater.UpdateInfo(version="0.9.2", notes="new", changed=[], deleted=[])
    ctrl._state = "checking"
    ctrl._on_found(info)

    ctrl._on_check_failed("TimeoutError: late timeout")

    assert ctrl._state == "available"
    assert chip.isVisible()
    assert "0.9.2" in chip.text()


def test_update_check_not_started_after_parent_closing(monkeypatch, qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    parent = QObject()
    parent._closing = True
    started = []

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeCheckThread:
        found = FakeSignal()
        checked = FakeSignal()
        failed = FakeSignal()

        def __init__(self, *_args, **_kwargs):
            started.append("created")

        def start(self):
            started.append("started")

    monkeypatch.setattr(update_ui, "_CheckThread", FakeCheckThread)

    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None, parent=parent)
    ctrl.start_check()

    assert started == []
    assert ctrl._state == "idle"
    assert ctrl._check_status == "never"


def test_update_check_thread_registered_for_parent_shutdown(monkeypatch, qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    parent = QObject()
    parent._closing = False
    parent._bg_tasks = []
    threads = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeCheckThread:
        def __init__(self, *_args, **_kwargs):
            self.found = FakeSignal()
            self.checked = FakeSignal()
            self.failed = FakeSignal()
            self.finished = FakeSignal()
            threads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(update_ui, "_CheckThread", FakeCheckThread)

    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None, parent=parent)
    ctrl.start_check()
    thread = threads[-1]

    assert thread in parent._bg_tasks
    assert getattr(thread, "_label", "") == "update-check"

    thread.finished.emit()

    assert thread not in parent._bg_tasks


def test_update_download_thread_registered_for_parent_shutdown(monkeypatch, qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    parent = QObject()
    parent._closing = False
    parent._bg_tasks = []
    downloads = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeDownloadThread:
        def __init__(self, *_args, **_kwargs):
            self.progress = FakeSignal()
            self.done = FakeSignal()
            self.failed = FakeSignal()
            self.finished = FakeSignal()
            downloads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(update_ui, "_DownloadThread", FakeDownloadThread)

    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None, parent=parent)
    ctrl._info = updater.UpdateInfo(version="0.9.2", notes="", changed=[], deleted=[])
    ctrl._state = "available"

    qtbot.mouseClick(chip, Qt.LeftButton)
    thread = downloads[-1]

    assert thread in parent._bg_tasks
    assert getattr(thread, "_label", "") == "update-download"

    thread.finished.emit()

    assert thread not in parent._bg_tasks


def test_update_late_found_ignored_after_parent_closing(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    parent = QObject()
    parent._closing = False
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None, parent=parent)
    ctrl._state = "checking"
    info = updater.UpdateInfo(version="0.9.2", notes="new", changed=[], deleted=[])

    parent._closing = True
    ctrl._on_found(info)

    assert ctrl._info is None
    assert ctrl._state == "checking"
    assert chip.isHidden()


def test_update_late_download_done_ignored_after_parent_closing(qtbot):
    chip = QPushButton()
    qtbot.addWidget(chip)
    parent = QObject()
    parent._closing = False
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None, parent=parent)
    ctrl._state = "downloading"
    chip.setEnabled(False)
    chip.setText("downloading")

    parent._closing = True
    ctrl._on_done()

    assert ctrl._state == "downloading"
    assert chip.isEnabled() is False
    assert chip.text() == "downloading"


def test_update_threads_have_stop(qtbot, tmp_path):
    info = updater.UpdateInfo(version="0.9.1", notes="", changed=[], deleted=[])
    dl = update_ui._DownloadThread("http://127.0.0.1:9", info, tmp_path / "s")
    chk = update_ui._CheckThread("http://127.0.0.1:9")
    dl.stop()
    chk.stop()
    assert dl._cancel is True and chk._cancel is True


def test_controller_shutdown_never_force_terminates_network_threads(qtbot):
    events = []

    class SlowThread:
        def stop(self):
            events.append("stop")

        def isRunning(self):
            return True

        def wait(self, timeout_ms):
            events.append(("wait", timeout_ms))
            return False

        def terminate(self):
            events.append("terminate")

    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._check = SlowThread()

    ctrl.shutdown(wait_ms=1)

    assert "stop" in events
    assert "terminate" not in events


def test_controller_shutdown_stops_running_download(qtbot, tmp_path, monkeypatch):
    """P1 回归：下载进行中关窗，shutdown() 必须停下真 QThread，否则「运行中被析构」崩溃。"""
    started = threading.Event()

    def slow_download(
        base_url, info, staging, progress=None, timeout=30, cancel=None,
        response_callback=None,
    ):
        started.set()
        while not (cancel and cancel()):  # 尊重 cancel，模拟慢下载
            time.sleep(0.01)
        raise InterruptedError("cancelled")

    monkeypatch.setattr(update_ui.updater, "download_delta", slow_download)
    chip = QPushButton()
    qtbot.addWidget(chip)
    ctrl = UpdateController(chip, "http://127.0.0.1:9", lambda: None)
    ctrl._info = updater.UpdateInfo(version="0.9.1", notes="", changed=[("a", "h", 1)], deleted=[])
    ctrl._staging = tmp_path / "s"
    ctrl._state = "available"
    ctrl._start_download()                      # 启真 QThread 跑 slow_download
    assert started.wait(2.0)                    # 下载线程确在跑
    assert ctrl._dl.isRunning()
    ctrl.shutdown(wait_ms=3000)                 # 模拟关窗收尾
    assert not ctrl._dl.isRunning()             # 干净停下，不会运行中被析构
