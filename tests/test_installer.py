# -*- coding: utf-8 -*-
r"""安装器的几条硬约束。

`tools/installer.iss` 里的版本号原本是写死的 `#define AppVersion "1.3.2"`，从 1.3.2
一路漂到 1.5.3 都没被发现——**因为这个安装器从来没被真正构建过**。没有构建，就没有
任何东西会因为它过期而失败。所以这里钉的第一条就是「版本号不许写死」。

其余几条是装出去之后很难补救的：装错位置会让自动更新永久失效、写了 Run 项会和应用
内的自启开关双重注册、卸载删错东西会毁用户数据。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "tools" / "installer.iss"
BUILDER = ROOT / "tools" / "build_installer.py"


@pytest.fixture(scope="module")
def iss() -> str:
    return ISS.read_text(encoding="utf-8-sig")


def test_iss_is_utf8_with_bom():
    """Inno 靠 BOM 认 Unicode，没有 BOM 会把中文按 ANSI 读成乱码。"""
    assert ISS.read_bytes().startswith(b"\xef\xbb\xbf")


def test_version_is_never_hardcoded(iss):
    """漂了 1.3.2 -> 1.5.3 那次的直接原因。"""
    assert not re.search(r'#define\s+AppVersion\s+"', iss), \
        "版本号不许写死在 .iss 里，必须由 /DAppVersion= 传入"
    assert "#ifndef AppVersion" in iss and "#error" in iss, \
        "漏传版本号必须直接编译失败，不能默默用一个旧值"
    assert "AppVersion={#AppVersion}" in iss


def test_builder_takes_the_version_from_the_package(iss):
    src = BUILDER.read_text(encoding="utf-8")
    assert "from pptx_finder import __version__" in src
    assert "/DAppVersion=" in src


def test_builder_refuses_a_dist_whose_exe_version_disagrees():
    """装完「关于」里显示另一个版本号，是很难解释的那种 bug。"""
    src = BUILDER.read_text(encoding="utf-8")
    assert "GetFileVersionInfo" in src
    assert "!= __version__" in src


def test_installs_per_user_so_auto_update_keeps_working(iss):
    """装进 Program Files 需要 UAC，且安装目录不可写 —— 增量更新会永久失效。"""
    assert "PrivilegesRequired=lowest" in iss
    assert "DefaultDirName={localappdata}\\Programs\\" in iss
    assert "{pf}" not in iss and "{commonpf}" not in iss and "{pf64}" not in iss


def test_install_dir_has_no_space(iss):
    """和 v1.5.2 起的产物命名约定一致，理由见 tests/test_dist_name_has_no_space.py。"""
    m = re.search(r"DefaultDirName=(.+)", iss)
    assert m, "找不到 DefaultDirName"
    leaf = m.group(1).strip().rsplit("\\", 1)[-1]
    assert " " not in leaf, f"安装目录名不该带空格：{leaf!r}"


def test_installer_does_not_register_autostart(iss):
    """自启由应用内设置写启动文件夹快捷方式；安装器再写一份就是双重注册。"""
    assert "[Registry]" not in iss
    assert "CurrentVersion\\Run" not in iss


def test_uninstall_never_nukes_a_user_chosen_folder(iss):
    r"""对 {app} 用 filesandordirs，会在用户把安装目录选到已有内容的文件夹时删掉别人的东西。"""
    block = iss.split("[UninstallDelete]", 1)[1] if "[UninstallDelete]" in iss else ""
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("Type:"):
            continue
        if "filesandordirs" in line:
            assert 'Name: "{app}"' not in line, f"太危险：{line}"


def test_uninstall_leaves_user_data_alone(iss):
    """索引库和版本库在 %LOCALAPPDATA%\\pptx-finder，**指令**里一个字都不该提它。

    只看指令行：头部注释里恰恰要写清楚「用户数据不在 {app}，卸载不触碰」，
    把注释也算进去等于禁止解释设计意图。
    """
    directives = [ln for ln in iss.splitlines()
                  if ln.strip() and not ln.lstrip().startswith(";")]
    offenders = [ln for ln in directives if "pptx-finder" in ln]
    assert not offenders, f"安装器指令不该触碰用户数据目录：{offenders}"


def test_it_closes_the_tray_app_before_overwriting(iss):
    """托盘常驻，不先请它退出就覆盖，exe/dll 会被占用导致装一半。"""
    assert "CloseApplications=yes" in iss


def test_old_spaced_entry_is_removed_on_upgrade(iss):
    """v1.5.2 改名后，旧 exe 不清掉的话桌面旧快捷方式点开的是旧壳。"""
    assert "[InstallDelete]" in iss
    assert r'Type: files; Name: "{app}\PPT Doctor.exe"' in iss


def test_icons_point_at_the_renamed_exe(iss):
    from pptx_finder.config import EXE_NAME

    icons = iss.split("[Icons]", 1)[1].split("[", 1)[0]
    assert EXE_NAME in icons
    assert "PPT Doctor.exe" not in icons
