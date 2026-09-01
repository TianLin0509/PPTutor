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


DELETE_OPS = ("DelTree(", "DeleteFile(", "RemoveDir(")


def deletion_calls(code: str) -> list[str]:
    """[Code] 段里所有会删东西的调用。

    「不许提到用户数据目录」这类断言不能对整段做——`.iss` 里 `{ }` 既是 Pascal
    注释也是常量语法，一刀切既会打在解释设计意图的注释上，也会误伤 `{app}`。
    真正的不变量只有一条：**没有任何删除调用指向用户数据**。就查这个。
    """
    return [ln.strip() for ln in code.splitlines()
            if any(op in ln for op in DELETE_OPS)]


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

    只看 [Code] 之前的指令行：头部注释里恰恰要写清楚「用户数据不在 app 目录下，
    卸载不触碰」，把注释也算进去等于禁止解释设计意图。[Code] 段另有专门的用例。
    """
    directives = [ln for ln in iss.split("[Code]", 1)[0].splitlines()
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


# ============ 清理旧版本（v1.5.5） ============
#
# 真机验证过 12 条：最老的 PPTutor.exe 目录、带空格的 PPT Doctor.exe 目录都被清掉；
# 「有 exe 名但没有 _internal\base_library.zip 签名」的诱饵目录一根汗毛没动；
# 用户把 zip 解压在自己文件夹里的情况，只删程序文件，「我的年终总结.docx」和目录
# 本身都保留；启动项被**改指向**新版而不是删掉。


def test_cleanup_is_opt_out_not_silent(iss):
    """删东西必须让用户看得见、能取消。"""
    assert 'Name: "cleanlegacy"' in iss
    tasks = iss.split("[Tasks]", 1)[1].split("[", 1)[0]
    assert "cleanlegacy" in tasks, "清理必须是一个可见的 Task"


def test_cleanup_recognises_every_exe_name_ever_shipped(iss):
    """最早叫 PPTutor.exe（0.8~0.9.1）。漏了它，最该清的那批正好清不掉。"""
    for name in ("'PPTutor.exe'", "'PPT Doctor.exe'", "'PPT-Doctor.exe'"):
        assert name in iss, f"清理逻辑不认得 {name}"


def test_cleanup_requires_a_positive_signature(iss):
    r"""只按 exe 名字判断会误伤；必须同时看到 _internal\base_library.zip。"""
    fn = iss.split("function LooksLikeInstallDir", 1)[1].split("end;", 1)[0]
    assert "base_library.zip" in fn
    assert "and (" in fn, "签名必须是「zip 存在 且 某个 exe 存在」"


def test_cleanup_never_deletes_a_whole_directory_tree(iss):
    r"""用户可能把 zip 解压在「下载」根目录。只能删点名的载荷，目录用 RemoveDir（空了才删）。"""
    proc = iss.split("procedure RemoveLegacyPayload", 1)[1].split("\nend;", 1)[0]
    assert "DelTree(AddBackslash(Dir) + '_internal'" in proc, "只该整树删 _internal"
    assert "DelTree(Dir" not in proc and "DelTree(AddBackslash(Dir),"not in proc, \
        "绝不能整树删目录本身"
    assert "RemoveDir(Dir)" in proc, "目录本身只能 RemoveDir（非空会失败，正是我们要的）"


def test_cleanup_never_touches_the_new_install(iss):
    proc = iss.split("procedure RemoveLegacyPayload", 1)[1].split("\nend;", 1)[0]
    assert "SameDir(Dir, ExpandConstant('{app}'))" in proc


def test_cleanup_never_touches_user_data(iss):
    r"""索引库和版本库在 %LOCALAPPDATA%\pptx-finder，任何删除调用都不许指向那里。"""
    calls = deletion_calls(iss.split("[Code]", 1)[1])
    assert calls, "一个删除调用都没有？那这个测试没在测东西"
    for ln in calls:
        low = ln.lower().replace("pptx-finder.lnk", "")   # 自启快捷方式就叫这个名字
        for token in ("localappdata", "userappdata", "pptx-finder", "vault", "index.db"):
            assert token not in low, f"删除调用指向了用户数据：{ln}"


def test_startup_shortcut_is_repointed_not_deleted(iss):
    """删掉的话，用户不手动开一次应用，开机自启就没了。

    而它恰恰是最要紧的一处：老版本每次开机会把自启快捷方式改指向自己，
    用户于是一直在用旧版还不自知。
    """
    code = iss.split("[Code]", 1)[1]
    assert "PointShortcutAt" in code
    assert "STARTUP_LNK, True" in code, "自启那一处必须走 repoint 分支"


def test_cleanup_runs_after_the_new_files_are_in_place(iss):
    """要拿新安装目录当排除项，也要把自启指向新装好的 exe。"""
    assert "CurStep = ssPostInstall" in iss


def test_pascal_comments_contain_no_closing_brace(iss):
    """Inno 的注释是花括号；正文里写 app 常量会提前闭合注释（构建时踩过）。"""
    code = iss.split("[Code]", 1)[1]
    depth = 0
    for lineno, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}") and stripped.count("{") == 1:
            continue                      # 单行注释，成对
        if stripped.startswith("{") and "}" not in stripped:
            depth += 1
        elif depth and stripped.endswith("}"):
            depth -= 1
    assert depth == 0, "有注释没闭合——多半是正文里出现了裸的花括号"
