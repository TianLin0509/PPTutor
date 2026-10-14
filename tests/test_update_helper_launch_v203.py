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
