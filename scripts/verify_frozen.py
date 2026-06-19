"""frozen exe 端到端验证：启动打包 exe（隔离 env）→ 不崩 → 改存 pptx → watcher 自动留版本。

证明版本管理子系统在 PyInstaller frozen 环境下：import 链 OK + 后台 watcher 真实工作。
比"截图证明窗口起来"更有力——它端到端跑通了 监听→快照→入库 全链路。

隔离：PPTX_FINDER_DATA_DIR(临时) + PPTX_FINDER_ROOTS(临时小目录)，不碰生产数据、不扫全盘。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "pptx-finder" / "pptx-finder.exe"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

import fixtures_gen as fx  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，中文/emoji 直接输出会崩
except Exception:  # noqa: BLE001
    pass

tmp = Path(tempfile.mkdtemp(prefix="frozen_verify_"))
DATA = tmp / "appdata"
DECKS = tmp / "decks"
DECKS.mkdir(parents=True)
DATA.mkdir(parents=True)
PPTX = DECKS / "测试方案.pptx"

ENV = dict(os.environ)
ENV["PPTX_FINDER_DATA_DIR"] = str(DATA)
ENV["PPTX_FINDER_ROOTS"] = str(DECKS)


def count_versions() -> int:
    """exe 退出后独占查 versions.db（源码 VersionManager 只读）。"""
    os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
    from pptx_finder.versioning.manager import VersionManager
    m = VersionManager()
    n = len(m.list_versions(str(PPTX)))
    m.stop()
    return n


def find_pptx_windows() -> list[str]:
    try:
        import win32gui
    except Exception:  # noqa: BLE001
        return []
    titles: list[str] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if t and ("pptx" in t.lower() or "PPTX" in t or "查询" in t or "版本" in t):
                titles.append(t)

    win32gui.EnumWindows(cb, None)
    return titles


def main() -> int:
    if not EXE.exists():
        print("FAIL: exe 不存在", EXE)
        return 1

    # —— 预置：造 pptx + 源码纳管（v1）——
    fx.make_pptx(PPTX, [{"body": "封面 frozen 测试"}, {"body": "第二页 量子计算 章节"}])
    os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
    from pptx_finder.versioning.manager import VersionManager
    pre = VersionManager()
    pre.add_root(str(DECKS))  # catch-up → v1
    n1 = len(pre.list_versions(str(PPTX)))
    pre.stop()
    print(f"预置：纳管后版本数 = {n1}（期望 1）")

    # —— 启动 frozen exe（隔离 env）——
    print(f"启动 frozen exe: {EXE.name} ...")
    proc = subprocess.Popen([str(EXE)], env=ENV)
    time.sleep(8)  # 等启动 + 索引 + watcher observer 就绪
    alive = proc.poll() is None
    print(f"层1 · exe 启动 8s 后存活（不崩溃）= {alive}")
    if not alive:
        print(f"FAIL: exe 退出码 {proc.returncode} —— 启动即崩（frozen import 问题）")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    wins = find_pptx_windows()
    print(f"层1 · 检测到主窗口 = {wins if wins else '（未匹配到标题，不影响判定）'}")

    # —— 层2：改存 pptx → frozen exe 的 watcher 应自动留新版本 ——
    time.sleep(1)
    fx.make_pptx(PPTX, [{"body": "封面 改稿"}, {"body": "第二页 经典计算"}, {"body": "新增第三页"}])
    print("已改存 pptx，等 frozen watcher 防抖(1.5s)+快照 ...")
    time.sleep(6)

    # 关 exe 释放 db 锁，再独占查版本
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    time.sleep(0.5)

    n2 = count_versions()
    print(f"层2 · 改存后版本数 = {n2}（期望 2 = frozen watcher 端到端工作）")

    ok = alive and n1 == 1 and n2 == 2
    print(f"\n=== frozen 端到端验证：{'PASS' if ok else 'FAIL'} ===")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
