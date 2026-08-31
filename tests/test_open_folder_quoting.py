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
