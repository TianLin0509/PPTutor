# -*- coding: utf-8 -*-
r"""v2.0.3：一键更新「点立即重启 → 程序关了，没更新也没重开」。

根因：v0.9 起 helper 用 DETACHED_PROCESS 启动，PowerShell 5.1 没有控制台时一行不执行、以 0 退出；
主程序照常退出。旧测试都是 subprocess.run 直接跑 apply.ps1（-MainPid 0），从没走过
「主程序 launch_helper 后立刻退出」这条真实路径，所以十几个版本都没发现。

这里按真实路径测：一个子进程调 launch_helper 后马上退出，helper 必须照样完成替换。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pptx_finder import updater

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows helper")

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _layout(tmp_path):
    dest, stg = tmp_path / "dest", tmp_path / "stg"
    dest.mkdir()
    stg.mkdir()
    (dest / "a.txt").write_text("old", encoding="utf-8")
    (stg / "a.txt").write_text("new", encoding="utf-8")
    (stg / "manifest.json").write_text("{}", encoding="utf-8")
    info = updater.UpdateInfo(version="9.9.9", notes="", changed=[("a.txt", "x", 3)], deleted=[])
    return dest, stg, info


def test_helper_still_swaps_after_the_launching_process_exits(tmp_path):
    dest, stg, info = _layout(tmp_path)
    h = updater.write_helper(stg, dest, info, "", log_path=tmp_path / "u.log",
                             result_path=tmp_path / "r.json")
    code = (f"import sys; sys.path.insert(0, {SRC!r}); import os; from pptx_finder import updater; "
            f"updater.launch_helper({h['ps1']!r}, {h['plan_path']!r}, os.getpid())")
    # 发起方是无控制台的进程（和打包后的 GUI 程序一样），握手后立刻退出
    r = subprocess.run([sys.executable, "-c", code], creationflags=0x08000000, timeout=60)
    assert r.returncode == 0
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (tmp_path / "r.json").exists():
        time.sleep(0.2)
    result = json.loads((tmp_path / "r.json").read_text("utf-8-sig"))
    assert result["ok"] is True, result
    assert (dest / "a.txt").read_text(encoding="utf-8") == "new"
    assert "helper start" in (tmp_path / "u.log").read_text("utf-8", "replace")


def test_helper_that_exits_without_running_is_reported_not_ignored(tmp_path, monkeypatch):
    """helper 没跑起来时必须抛错——主程序据此留在原地，而不是关掉自己。"""
    dest, stg, info = _layout(tmp_path)
    h = updater.write_helper(stg, dest, info, "")

    class Dead:
        def __init__(self, *a, **k):
            pass

        def poll(self):
            return 0

    monkeypatch.setattr(updater.subprocess, "Popen", Dead)
    with pytest.raises(updater.HelperNotStarted):
        updater.launch_helper(h["ps1"], h["plan_path"], 1, handshake_s=2)


def test_helper_that_never_handshakes_times_out(tmp_path, monkeypatch):
    dest, stg, info = _layout(tmp_path)
    h = updater.write_helper(stg, dest, info, "")

    class Hung:
        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(updater.subprocess, "Popen", Hung)
    t0 = time.monotonic()
    with pytest.raises(updater.HelperNotStarted):
        updater.launch_helper(h["ps1"], h["plan_path"], 1, handshake_s=0.5)
    assert time.monotonic() - t0 < 5


def test_ui_stays_open_and_can_retry_when_helper_fails(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from pptx_finder.ui import update_ui
    chip = QPushButton()
    qtbot.addWidget(chip)
    quits, calls = [], []

    def failing(*a, **k):
        calls.append(1)
        raise updater.HelperNotStarted("更新程序（PowerShell）启动后立即退出了")

    monkeypatch.setattr(update_ui.updater, "apply_update", failing)
    ctrl = update_ui.UpdateController(chip, "http://127.0.0.1:9", lambda: quits.append(1))
    ctrl._info = updater.UpdateInfo(version="9.9.9", notes="", changed=[], deleted=[])
    ctrl._staging = tmp_path / "stg"
    ctrl._staging.mkdir()
    ctrl._state = "downloading"
    ctrl._on_done()
    ctrl.apply()
    assert quits == [] and ctrl.phase() == "error"
    snap = ctrl.snapshot()
    assert snap["apply_failed"] and "程序没有关闭" in snap["error"]
    downloads = []
    monkeypatch.setattr(ctrl, "_start_download", lambda: downloads.append(1))
    monkeypatch.setattr(update_ui.updater, "apply_update", lambda *a, **k: calls.append(2))
    ctrl.retry_apply()
    assert downloads == [] and calls == [1, 2] and quits == [1]


# ---- v2.0.3 同批：侧栏「更新」入口、设置窗口不出横向滚动、版本库弹窗只问一次 ----

def test_update_entry_sits_in_sidebar_and_tracks_state(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("PPTX_FINDER_ROOTS", str(tmp_path / "d"))
    from PySide6.QtWidgets import QPushButton
    from pptx_finder.ui import update_ui
    from pptx_finder.ui.main_window import MainWindow
    win = MainWindow(do_index=False)
    qtbot.addWidget(win)
    assert win.rail_update_btn.parentWidget() is win._nav_rail
    assert win.rail_update_btn.property("railLabel") == "检查更新"
    ctrl = update_ui.UpdateController(QPushButton(), "http://127.0.0.1:9", lambda: None, win)
    win._updater = ctrl
    ctrl.state_changed.connect(win._refresh_update_rail)
    ctrl._info = updater.UpdateInfo(version="9.9.9", notes="", changed=[], deleted=[])
    ctrl._state = "available"
    ctrl._notify()
    assert win.rail_update_btn.property("railLabel") == "新版本 v9.9.9"
    assert not win._update_dot.isHidden()
    opened = []
    monkeypatch.setattr(ctrl, "open_dialog", lambda **k: opened.append(1))
    win.rail_update_btn.click()
    assert opened == [1]


def test_settings_pages_never_need_horizontal_scrolling(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "d"))
    from PySide6.QtWidgets import QScrollArea
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager
    dlg = SettingsDialog(VersionManager())
    qtbot.addWidget(dlg)
    dlg.resize(760, 560)   # 允许的最小尺寸
    dlg.show()
    qtbot.waitExposed(dlg)
    for i in range(dlg.tabs.count()):
        dlg.tabs.setCurrentIndex(i)
        qtbot.wait(30)
        page = dlg.tabs.currentWidget()
        if isinstance(page, QScrollArea):
            inner = page.widget()
            assert inner.minimumSizeHint().width() <= page.viewport().width() + 1, \
                f"「{dlg.tabs.tabText(i)}」页内容比可视区宽，会被截掉"


def test_vault_storage_prompt_is_asked_once_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "d"))
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    import pptx_finder.ui.vault_storage_dialog as module
    from pptx_finder import config
    from pptx_finder.ui.main_window import MainWindow
    opened = []

    class Dialog:
        def __init__(self, manager, parent):
            opened.append(1)

        def exec(self):
            return QDialog.Rejected       # 用户点「暂不调整」

    class Mgr:
        def configure_storage(self, *a, **k):
            pass

    monkeypatch.setattr(module, "VaultStorageDialog", Dialog)
    MainWindow._prompt_version_storage(SimpleNamespace(_version_mgr=Mgr()))
    MainWindow._prompt_version_storage(SimpleNamespace(_version_mgr=Mgr()))   # 模拟重启后的新窗口
    assert opened == [1]
    assert not config.get_vault_storage_confirmed()
