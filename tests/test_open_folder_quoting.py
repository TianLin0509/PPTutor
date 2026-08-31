# -*- coding: utf-8 -*-
r"""「打开文件夹」不能把路径丢给 explorer 的命令行解析器去猜。

用户反馈「经常打开成错误的文件夹」。真机复现（Windows 11，`Shell.Application`
读回资源管理器实际停留位置）：

    路径                      v1.5.1 打开了              期望
    ...\PPT Doctor\demo.pptx  C:\Users\<me>\Documents    ...\PPT Doctor
    ...\comma,dir\demo.pptx   C:\Users\<me>\Desktop      ...\comma,dir
    ...\a & b\demo.pptx       C:\Users\<me>\Documents    ...\a & b
    ...\plaindir\demo.pptx    ...\plaindir               ...\plaindir  (对)

原因是两条规则打架：`subprocess` 传列表时 `list2cmdline` 见参数含空格就整体加
引号，得到 `explorer "/select,C:\a b\c.pptx"`；而 explorer 的解析器要求引号只
包路径（`explorer /select,"C:\a b\c.pptx"`），整体加引号它认不出来，于是回落到
默认文件夹。不带引号时逗号又会被当分隔符截断。**只要路径含空格就必错**，而
「文档」「桌面」这类默认位置恰好看起来像是"打开了个别的文件夹"。

所以这里钉的是「不把引号交给 list2cmdline 决定」，而不是某一次调用的结果。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from pptx_finder import actions


TRICKY = [
    r"C:\Users\me\My Docs\PPT Doctor\deck.pptx",   # 空格：v1.5.1 的必错项
    r"C:\data\comma,dir\deck.pptx",                # 逗号：explorer 会截断
    r"C:\data\a & b\deck.pptx",                    # & ：交给 cmd 会被当分隔符
    r"C:\data\plain\deck.pptx",                    # 对照：本来就是对的
    r"C:\数据\季度 汇报\deck.pptx",                # 中文 + 空格
]


@pytest.mark.parametrize("target", TRICKY)
def test_fallback_command_quotes_the_path_not_the_switch(target):
    cmd = actions.explorer_select_command(target)
    assert '/select,"' in cmd, f"引号必须紧跟在 /select, 之后：{cmd}"
    assert '"/select,' not in cmd, f"引号不能包住 /select,（explorer 认不出）：{cmd}"
    assert cmd.endswith(f'/select,"{target}"'), cmd


def test_fallback_command_uses_an_absolute_explorer(monkeypatch):
    """别让 PATH 上的同名 explorer 顶上来。"""
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    cmd = actions.explorer_select_command(r"C:\x\y.pptx")
    assert cmd.startswith('"C:\\Windows\\explorer.exe" '), cmd


@pytest.mark.parametrize("target", [t for t in TRICKY if r"\plain" not in t])
def test_list2cmdline_would_have_broken_these(target):
    """守住「为什么不能传列表」这条理由本身，别让人以为换写法只是风格偏好。

    `plain` 那条不在这里：它本来就是旧实现唯一能走对的形状，正因为如此，
    开发机上随手一试往往是好的，bug 只在用户那些带空格的真实路径上出现。
    """
    broken = subprocess.list2cmdline(["explorer", f"/select,{target}"])
    quoted_whole = '"/select,' in broken
    comma_split = "," in broken.split("/select,", 1)[1] and not quoted_whole
    assert quoted_whole or comma_split, broken


def test_open_folder_never_hands_a_list_to_popen(monkeypatch, tmp_path):
    """真正的回归锁：命令行必须是字符串，一旦有人改回列表就红。"""
    folder = tmp_path / "PPT Doctor"
    folder.mkdir()
    deck = folder / "季度 汇报.pptx"
    deck.write_bytes(b"ppt")

    seen: list[object] = []
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: seen.append(cmd) or object())

    assert actions.open_folder(str(deck)) is True
    assert len(seen) == 1
    cmd = seen[0]
    assert isinstance(cmd, str), f"传列表就会被 list2cmdline 加错引号：{cmd!r}"
    assert f'/select,"{os.path.normpath(str(deck))}"' in cmd


def test_open_folder_does_not_block_the_caller_on_the_shell_api(monkeypatch, tmp_path):
    """explorer 能起来就不碰 shell API。

    `SHOpenFolderAndSelectItems` 是同步的：真机实测新开窗口要 1,465～1,742 ms，
    而 health_window / report_overlay 是直接在 UI 线程上调 open_folder 的。旧实现
    用 Popen 天然异步，修 bug 不能顺手换成一次可见卡顿。
    """
    deck = tmp_path / "a b.pptx"
    deck.write_bytes(b"ppt")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: object())
    monkeypatch.setattr(
        actions, "_select_via_shell_api",
        lambda _t: pytest.fail("explorer 起得来时不该走同步的 shell API"))
    assert actions.open_folder(str(deck)) is True


def test_shell_api_catches_the_case_where_explorer_cannot_start(monkeypatch, tmp_path):
    deck = tmp_path / "a b.pptx"
    deck.write_bytes(b"ppt")
    called: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no explorer")))
    monkeypatch.setattr(
        actions, "_select_via_shell_api",
        lambda t: called.append(t) or True)
    assert actions.open_folder(str(deck)) is True
    assert called == [os.path.normpath(str(deck))]


def test_open_folder_falls_back_to_the_parent_when_the_file_is_gone(monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    assert actions.open_folder(str(tmp_path / "gone.pptx")) is True
    assert opened == [str(tmp_path)]


def test_open_folder_reports_failure_when_nothing_exists(monkeypatch):
    assert actions.open_folder(r"C:\no\such\dir\gone.pptx") is False


# ============ 根路径：压测（62,177 条命令行 + 35 条真机）挖出来的 ============
#
# A 层用 Windows 自己的 CommandLineToArgvW 当裁判，62,177 条对抗性路径里只有 5 条
# 往返失败，全是尾部反斜杠——也就是盘符根 `C:\` 和 UNC 共享根。`"C:\"` 里的 `\"`
# 会被当成转义引号，参数变成 `/select,C:"` 且引号不闭合。
#
# 顺着这条线真机实测，得到几个反直觉的事实（B 层）：
#
#     os.startfile('C:\')                窗口集合完全没变化——什么都没发生
#     ShellExecuteW(open/explore/None)   同上，三种动词都没反应
#     explorer.exe "C:\\"                落到「文档」（explorer 不做反转义！）
#     explorer.exe /select,C:\           落到「此电脑」
#     explorer.exe C:\                   ✅ 正确
#     os.startfile('C:\Windows')         ✅ 正确（普通目录是好使的）
#     os.startfile('\\localhost\C$\')    ✅ 正确（UNC 共享根也是好使的）
#
# 结论：盘符根必须用**不加引号**的 `explorer.exe C:\`，其余用 os.startfile。


@pytest.mark.parametrize("path,is_root", [
    ("C:\\", True),
    ("D:\\", True),
    (r"\\server\share", True),
    (r"C:\Users", False),
    (r"C:\Users\me\deck.pptx", False),
    (r"\\server\share\dir", False),
])
def test_is_path_root_recognises_what_has_no_parent(path, is_root):
    assert actions.is_path_root(path) is is_root


def test_a_drive_root_never_reaches_the_select_command(monkeypatch):
    r"""根路径若流到 explorer_select_command，就会拼出 `/select,"C:\"` 那个坏形状。"""
    monkeypatch.setattr(
        actions, "explorer_select_command",
        lambda t: pytest.fail(f"根路径不该走 /select：{t!r}"))
    seen: list[str] = []
    monkeypatch.setattr(actions, "open_drive_root", lambda t: seen.append(t) or True)
    monkeypatch.setattr(os.path, "exists", lambda _p: True)
    assert actions.open_folder("C:\\") is True
    assert seen == ["C:\\"]


def test_drive_root_uses_an_unquoted_explorer_argument(monkeypatch):
    """explorer 不做反转义：加了引号的 `"C:\\\\"` 实测会落到「文档」。"""
    seen: list[str] = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: seen.append(cmd) or object())
    monkeypatch.setattr(
        os, "startfile",
        lambda *a, **k: pytest.fail("盘符根用 os.startfile 实测毫无反应"),
        raising=False)
    assert actions.open_drive_root("C:\\") is True
    assert len(seen) == 1
    assert seen[0].endswith(" C:\\"), seen[0]
    assert '"C:' not in seen[0].split("explorer.exe", 1)[1], f"路径不该加引号：{seen[0]}"


def test_unc_share_root_uses_startfile(monkeypatch):
    """UNC 共享根名字可能带空格，而 os.startfile 对它实测是好使的。"""
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: pytest.fail("UNC 共享根不该走不加引号的 explorer"))
    assert actions.open_drive_root(r"\\server\my share") is True
    assert opened == [r"\\server\my share"]


def test_double_clicking_a_drive_root_result_also_works(monkeypatch, tmp_path):
    """open_file 也踩同一个坑：os.startfile('D:\') 是完全没反应的。"""
    seen: list[str] = []
    monkeypatch.setattr(actions, "open_drive_root", lambda t: seen.append(t) or True)
    monkeypatch.setattr(os.path, "exists", lambda _p: True)
    monkeypatch.setattr(
        os, "startfile",
        lambda *a, **k: pytest.fail("盘符根不该走 os.startfile"), raising=False)
    assert actions.open_file("C:\\") is True
    assert seen == ["C:\\"]


def test_open_file_still_uses_startfile_for_everything_else(monkeypatch, tmp_path):
    deck = tmp_path / "a b.pptx"
    deck.write_bytes(b"x")
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    assert actions.open_file(str(deck)) is True
    assert opened == [str(deck)]


@pytest.mark.parametrize("raw", [
    r"C:\dir\\", r"C:\dir\\\\", r"C:\a\.\b\..\c", r"C:\a\b\\",
])
def test_normalising_leaves_no_trailing_slash_on_non_roots(raw):
    """`explorer_select_command` 的前置条件：归一化后非根路径不带尾部反斜杠。"""
    target = os.path.normpath(os.path.abspath(raw))
    assert not target.endswith("\\") or actions.is_path_root(target)


def test_select_command_is_verbatim_between_the_quotes():
    """explorer 拿到的就是两个引号之间的原文——不能有任何转义加工。"""
    for p in [r"C:\a b\c.pptx", r"C:\a,b\c.pptx", r"C:\a&b\c.pptx",
              r"C:\中文 目录\报告.pptx", r"C:\a^b\c.pptx"]:
        cmd = actions.explorer_select_command(p)
        assert cmd.split("/select,", 1)[1] == f'"{p}"'
