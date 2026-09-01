# -*- coding: utf-8 -*-
r"""落到磁盘上的产物名不能带空格，且改名不能把老用户更新丢了。

用户反馈：解压 `PPT Doctor` 之后跑不起来。空格本身在解压这一步只是诱因，真正的
问题是它会让后面每一个「自己拼命令行」的环节都可能裂开——批处理、快捷方式目标、
安装/解压工具的调用串，以及同一批修掉的 `explorer /select,`（见
tests/test_open_folder_quoting.py，那个 bug 的根因是一模一样的引号规则）。

所以 v1.5.2 把产物改成 `PPT-Doctor` / `PPT-Doctor.exe`，展示名仍是「PPT Doctor」。
改名的代价集中在增量更新：旧名字会进 deletes，helper 如果还按当前进程名重启，
就会去启动一个刚被自己删掉的文件。这里把两件事一起钉住。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pptx_finder import config, updater

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_names_contain_no_space():
    assert " " not in config.DIST_DIR_NAME
    assert " " not in config.EXE_NAME
    assert config.EXE_NAME == config.DIST_DIR_NAME + ".exe"
    # 展示名可以有空格——那是给人看的，不落磁盘
    assert config.APP_DISPLAY_NAME == "PPT Doctor"


def test_spec_matches_the_declared_names():
    """spec 和常量各写各的就会漂，这里是唯一的对齐点。"""
    spec = (ROOT / "pptx-finder.spec").read_text(encoding="utf-8")
    names = re.findall(r"^\s*name='([^']+)',\s*$", spec, re.MULTILINE)
    assert names, "spec 里找不到 EXE/COLLECT 的 name"
    assert set(names) == {config.DIST_DIR_NAME}, names


def test_version_info_original_filename_matches_the_exe():
    info = (ROOT / "assets" / "windows_version_info.txt").read_text(encoding="utf-8")
    assert f"'OriginalFilename', '{config.EXE_NAME}'" in info
    # FileDescription 才是任务管理器里显示的名字，那里保留空格
    assert config.APP_DISPLAY_NAME in info


@pytest.mark.parametrize("rel", [
    "tools/installer.iss",
    "scripts/gen_shortcut.py",
    "scripts/verify_frozen.py",
])
def test_packaging_scripts_no_longer_point_at_the_spaced_path(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    # installer.iss 的 [Code] 段整段豁免：v1.5.5 的「清理旧版本」功能**必须**认得
    # 历来的每个 exe 名（含带空格的旧名），那正是它的职责。这里要守的是「安装/图标
    # 等指令不再指向旧路径」。
    text = text.split("[Code]", 1)[0]
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith((";", "#")):
            continue
        # InstallDelete 必须提旧名——它的活儿就是把旧入口清掉
        if line.startswith("Type: files"):
            continue
        assert "PPT Doctor.exe" not in line, f"{rel}:{i} 仍指向旧 exe 名"
        assert "PPT Doctor\\" not in line and "PPT Doctor/" not in line, \
            f"{rel}:{i} 仍指向旧目录"


def test_installer_removes_the_renamed_old_entry():
    """不清掉的话 {app} 里会留一个旧 exe，桌面旧快捷方式点开的就是它。"""
    iss = (ROOT / "tools" / "installer.iss").read_text(encoding="utf-8")
    assert "[InstallDelete]" in iss
    assert r'Type: files; Name: "{app}\PPT Doctor.exe"' in iss


def test_zip_entries_use_the_space_free_folder():
    """打包脚本写进 zip 的顶层目录名必须来自常量，不能再写死带空格的字面量。"""
    src = (ROOT / "tools" / "package_dist.py").read_text(encoding="utf-8")
    assert "Path(DIST_DIR_NAME)" in src
    assert 'Path("PPT Doctor")' not in src


# ---- 改名当天的增量更新 ----

def _manifest(version, files, entry=""):
    m = {"version": version, "notes": "", "files": {
        name: {"hash": h, "size": 1} for name, h in files.items()}}
    if entry:
        m["entry"] = entry
    return m


def test_build_manifest_records_the_entry_exe(tmp_path):
    (tmp_path / "PPT-Doctor.exe").write_bytes(b"exe")
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "lib.dll").write_bytes(b"dll")
    m = updater.build_manifest(tmp_path, "1.5.2")
    assert m["entry"] == "PPT-Doctor.exe"


def test_relaunch_follows_the_new_manifest_when_the_entry_is_renamed():
    """1.5.1 -> 1.5.2：旧 exe 进 deletes，重启必须用新名字。"""
    local = _manifest("1.5.1", {"PPT Doctor.exe": "a" * 64}, entry="PPT Doctor.exe")
    remote = _manifest("1.5.2", {"PPT-Doctor.exe": "b" * 64}, entry="PPT-Doctor.exe")
    info = updater.compare(local, remote)
    assert info is not None
    assert "PPT Doctor.exe" in info.deleted          # 旧入口确实会被删
    assert updater.relaunch_name(info, "PPT Doctor.exe") == "PPT-Doctor.exe"


def test_relaunch_falls_back_when_the_manifest_says_nothing():
    """老清单没有 entry 字段，行为必须和以前完全一致。"""
    local = _manifest("1.5.0", {"PPT Doctor.exe": "a" * 64})
    remote = _manifest("1.5.1", {"PPT Doctor.exe": "b" * 64})
    info = updater.compare(local, remote)
    assert updater.relaunch_name(info, "PPT Doctor.exe") == "PPT Doctor.exe"


@pytest.mark.parametrize("evil", [
    "../../../AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/x.exe",
    "C:/Windows/System32/cmd.exe",
    "sub/dir/app.exe",
    "..",
])
def test_a_hostile_entry_never_becomes_the_relaunch_target(evil):
    """entry 来自网络，且会被 Start-Process 直接执行——只认落在本次文件集里的纯文件名。"""
    remote = _manifest("9.9.9", {"PPT-Doctor.exe": "b" * 64})
    remote["entry"] = evil
    info = updater.compare(_manifest("1.0.0", {"PPT-Doctor.exe": "a" * 64}), remote)
    assert updater.relaunch_name(info, "safe.exe") == "safe.exe"


def test_entry_must_actually_ship_in_this_update():
    """名字合法但根本不在清单里 —— 重启它同样是启动一个不存在的文件。"""
    remote = _manifest("9.9.9", {"PPT-Doctor.exe": "b" * 64}, entry="OtherApp.exe")
    info = updater.compare(_manifest("1.0.0", {"PPT-Doctor.exe": "a" * 64}), remote)
    assert updater.relaunch_name(info, "safe.exe") == "safe.exe"


def test_update_ui_asks_the_manifest_for_the_relaunch_target():
    """回归锁：别有人改回 Path(sys.executable).name。"""
    src = (ROOT / "src" / "pptx_finder" / "ui" / "update_ui.py").read_text(encoding="utf-8")
    assert "updater.relaunch_name(" in src
    assert "relaunch=Path(sys.executable).name" not in src
