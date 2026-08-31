# -*- coding: utf-8 -*-
r"""分发包内的最深路径要留够 MAX_PATH 余量。

用户反馈「解压容易失败」。真机复现（把 v1.5.2 的包解到一个 149 字符的目录）：

    FileNotFoundError: [Errno 2] No such file or directory:
      '...\\PPT-Doctor\\_internal\\lxml\\isoschematron\\resources\\xsl\\
       iso-schematron-xslt1\\iso_schematron_skeleton_for_xslt1.xsl'

失败**只发生在最深的那几个文件上**，前面几百个都解得出来——所以现象是「解压到一半
报错」「解出来跑不起来」，而不是干脆利落的失败，很难让人联想到路径长度。

算一下就知道有多紧：Windows 的 MAX_PATH 是 260，包内最深条目原本 112 字符，
留给用户的解压目录只剩 147。而「下载目录 + 压缩包同名文件夹 + 包内 PPT-Doctor/」
本身就要吃掉几十个字符，中文用户名再占几个——147 并不宽裕。

最长的 6 条全是 `lxml/isoschematron/`（Schematron 校验资源，本项目只用 lxml.etree
读写 OOXML，全库无引用），第 7 条是 `qtvirtualkeyboardplugin.dll`（它 import 的
Qt6VirtualKeyboard.dll 早就被 spec 剔除了，读 PE 导入表确认过，任何已发布的包里
它都从未加载成功）。删掉这两棵树：最深 112 → 79，解压目录余量 147 → 180。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 包内最深条目（含顶层 PPT-Doctor/ 前缀）的上限。留 180 字符给用户的解压目录。
MAX_ENTRY_LEN = 85
WINDOWS_MAX_PATH = 260


def _spec_drop_block() -> str:
    spec = (ROOT / "pptx-finder.spec").read_text(encoding="utf-8")
    return spec.split("_DROP = (", 1)[1].split("\n)", 1)[0]


@pytest.mark.parametrize("needle", [
    "'lxml/isoschematron/'",
    "'plugins/platforminputcontexts/'",
])
def test_the_deepest_dead_trees_stay_pruned(needle):
    """这两棵树是最长路径的来源，且都证明过用不到——重新打包时不能悄悄回来。"""
    assert needle in _spec_drop_block(), f"{needle} 不在 spec 的 _DROP 里"


def _dist_dir() -> Path | None:
    from pptx_finder.config import DIST_DIR_NAME

    d = ROOT / "dist" / DIST_DIR_NAME
    return d if d.is_dir() else None


def test_built_package_leaves_room_for_a_deep_extract_dir():
    """有 dist 就量真的；没有就跳过（CI 不一定构建）。"""
    dist = _dist_dir()
    if dist is None:
        pytest.skip("尚未构建 dist，跳过实测")
    from pptx_finder.config import DIST_DIR_NAME

    worst_len, worst = 0, ""
    for dirpath, _dirs, files in os.walk(dist):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), dist)
            # zip 里还要加一层顶层目录名
            entry = f"{DIST_DIR_NAME}/{rel}".replace(os.sep, "/")
            if len(entry) > worst_len:
                worst_len, worst = len(entry), entry
    assert worst_len <= MAX_ENTRY_LEN, (
        f"最深条目 {worst_len} 字符，超出预算 {MAX_ENTRY_LEN}：{worst}\n"
        f"解压目录只剩 {WINDOWS_MAX_PATH - worst_len - 1} 字符可用")


def test_shipped_zip_matches_the_same_budget():
    import zipfile

    from pptx_finder import __version__

    zip_path = ROOT / "dist" / f"PPT-Doctor-v{__version__}.zip"
    if not zip_path.is_file():
        pytest.skip("尚未打包 zip，跳过实测")
    with zipfile.ZipFile(zip_path) as z:
        names = [i.filename for i in z.infolist() if not i.filename.endswith("/")]
    worst = max(names, key=len)
    assert len(worst) <= MAX_ENTRY_LEN, f"{len(worst)} 字符：{worst}"
    assert not any("isoschematron" in n for n in names)
