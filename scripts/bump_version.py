#!/usr/bin/env python
"""版本号抬升 —— 由 scripts/merge_task.py 调用，工作位不要手动跑。

为什么这件事必须在合并那一刻做：
    版本号写在三个文件里，而这几行是**所有并行分支都要改的同几行**。
    两个群聊同时基于同一个主干开工，第二个合进来的必然遇到
    「数值和主干撞了」或「文本冲突」二选一 —— 而分支自己无从知道
    它会是第几个合进去的，那个信息只有合并那一刻才存在。
    合并本身是串行的（merge_task.py 拿着锁），所以把抬版本挪到那里，
    这个冲突就从「结构性必然」变成「不可能发生」。

为什么是四处而不是两处：
    tests/test_package_spec.py 断言 assets/windows_version_info.txt 里的
    filevers / prodvers / FileVersion / ProductVersion 必须等于 __version__。
    只抬 pyproject.toml 和 __init__.py，这条测试会在抬完之后立刻变红 ——
    而闸门恰好在抬版本之后跑测试，等于每一次合并都过不去。
    uv.lock 里也记着本项目自己的版本，`uv run` 发现对不上就会顺手改写它。
    不在这里一起抬，闸门每跑一次测试就会让工作区多一个未提交改动
    （实测：入库的 uv.lock 停在 1.5.1，而 pyproject 已经是 1.5.6）。

为什么按行定点替换而不是重新序列化：
    三个文件都是 CRLF，且 windows_version_info.txt 是给 PyInstaller 读的
    Python 字面量。整体重写会改掉行尾和格式，把一次改 6 行的提交变成全文件改动。
    这里逐行替换，除版本号那几行外一个字节都不动（行尾也原样保留）。

用法：
    python scripts/bump_version.py               # patch +1
    python scripts/bump_version.py --set 1.6.0   # 指定版本
    python scripts/bump_version.py --print       # 只打印当前版本，不改文件
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 不加这两句，Windows 控制台会把中文和 ✗ 转义掉，合并脚本转述出来的报错就没法看
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parents[1]

PYPROJECT = REPO / "pyproject.toml"
INIT_PY = REPO / "src" / "pptx_finder" / "__init__.py"
VERSION_INFO = REPO / "assets" / "windows_version_info.txt"
UV_LOCK = REPO / "uv.lock"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_text(path: Path) -> str:
    # newline="" —— 原样保留 CRLF，写回时才不会把整个文件的行尾改掉
    # （Path.read_text 到 3.13 才收 newline，这里走 open，3.12 也能用）
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def current_version() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', read_text(INIT_PY), re.M)
    if not m:
        raise RuntimeError(f"在 {INIT_PY.name} 里找不到 __version__")
    return m.group(1)


def bump_patch(version: str) -> str:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        raise RuntimeError(f"版本号不是纯数字三段式，脚本不敢猜：{version}")
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"


def sub_exactly_once(text: str, pattern: re.Pattern[str], repl: str, what: str) -> str:
    out, n = pattern.subn(repl, text)
    if n != 1:
        raise RuntimeError(f"定位不到（或匹配到 {n} 处）{what}，没有改任何文件")
    return out


def render_pyproject(text: str, ver: str) -> str:
    """只改 [project] 表里的 version，别的表（比如工具配置）一律不碰。"""
    lines = text.split("\n")
    table, hits = None, 0
    for i, line in enumerate(lines):
        cr = "\r" if line.endswith("\r") else ""
        body = line[:-1] if cr else line
        stripped = body.strip()
        m_table = re.fullmatch(r"\[([^\]]+)\]", stripped)
        if m_table:
            table = m_table.group(1)
            continue
        if table != "project":
            continue
        m_ver = re.fullmatch(r'version\s*=\s*"[^"]*"', stripped)
        if m_ver:
            indent = body[: len(body) - len(body.lstrip())]
            lines[i] = f'{indent}version = "{ver}"{cr}'
            hits += 1
    if hits != 1:
        raise RuntimeError(f"pyproject.toml 的 [project].version 匹配到 {hits} 处，没有改任何文件")
    return "\n".join(lines)


def render_init(text: str, ver: str) -> str:
    return sub_exactly_once(
        text,
        re.compile(r'^__version__\s*=\s*"[^"]*"', re.M),
        f'__version__ = "{ver}"',
        "__init__.py 的 __version__",
    )


def render_version_info(text: str, ver: str) -> str:
    a, b, c = ver.split(".")
    for pattern, repl, what in (
        (re.compile(r"filevers=\(\d+, \d+, \d+, 0\)"), f"filevers=({a}, {b}, {c}, 0)", "filevers"),
        (re.compile(r"prodvers=\(\d+, \d+, \d+, 0\)"), f"prodvers=({a}, {b}, {c}, 0)", "prodvers"),
        (re.compile(r"StringStruct\('FileVersion', '[^']*'\)"),
         f"StringStruct('FileVersion', '{ver}')", "FileVersion"),
        (re.compile(r"StringStruct\('ProductVersion', '[^']*'\)"),
         f"StringStruct('ProductVersion', '{ver}')", "ProductVersion"),
    ):
        text = sub_exactly_once(text, pattern, repl, f"windows_version_info.txt 的 {what}")
    return text


def render_uv_lock(text: str, ver: str) -> str:
    """只改 [[package]] name = "pptx-finder" 这一块里的 version，别的包一律不碰。

    改错任何一个依赖的 version 都不会立刻报错，只会在下次 uv sync 时爆炸。
    """
    blocks = text.split("[[package]]")
    hits = 0
    for i, blk in enumerate(blocks):
        if not re.match(r'\s*\r?\nname = "pptx-finder"\r?\n', blk):
            continue
        blocks[i], n = re.subn(r'(?m)^version = "[^"]*"', f'version = "{ver}"', blk, count=1)
        hits += n
    if hits != 1:
        raise RuntimeError(f"uv.lock 里 pptx-finder 自己的 version 匹配到 {hits} 处，没有改任何文件")
    return "[[package]]".join(blocks)


TARGETS = (
    (PYPROJECT, render_pyproject),
    (INIT_PY, render_init),
    (VERSION_INFO, render_version_info),
    (UV_LOCK, render_uv_lock),
)


def write_version(ver: str) -> list[str]:
    """四处一起改。任何一处定位失败都整体抛错，不留「只改了一半」的状态——那比不改更糟。"""
    staged = []
    for path, render in TARGETS:
        if not path.exists():
            raise RuntimeError(f"找不到 {path}")
        staged.append((path, render(read_text(path), ver)))
    for path, text in staged:
        write_text(path, text)
    return [str(p.relative_to(REPO)).replace("\\", "/") for p, _ in staged]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="target", help="指定版本号，形如 1.6.0")
    ap.add_argument("--print", dest="show", action="store_true", help="只打印当前版本")
    args = ap.parse_args(argv)

    cur = current_version()
    if args.show:
        print(cur)
        return 0

    nxt = args.target.strip() if args.target else bump_patch(cur)
    if not SEMVER.fullmatch(nxt):
        raise RuntimeError(f"目标版本号非法：{nxt or '(空)'}")

    files = write_version(nxt)
    print(f"版本号 {cur} → {nxt}（{'、'.join(files)}）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 —— 顶层出口，要的就是「非 0 退出 + 人话」
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
