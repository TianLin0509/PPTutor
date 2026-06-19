"""frozen exe 端到端验证：启动打包 exe → 不崩 → 改存 Desktop 下 pptx → 全盘 watcher 自动留版本。

证明新架构（全盘监听、谁变管谁）在 PyInstaller frozen 下完整工作。
DECKS 放 Desktop（被 default_watch_paths 监听）——不能放 AppData/Temp（被排除）。
DATA(vault) 隔离在临时目录，不碰生产。
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

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
except Exception:  # noqa: BLE001
    pass

import fixtures_gen as fx  # noqa: E402

_data_tmp = Path(tempfile.mkdtemp(prefix="frozen_verify_"))
DATA = _data_tmp / "appdata"
DATA.mkdir(parents=True)
# DECKS 必须在被全盘 watcher 监听的位置（Desktop），不能放 AppData/Temp（被排除）
DECKS = Path(os.path.expanduser("~")) / "Desktop" / "_pptxverify_decks"
DECKS.mkdir(parents=True, exist_ok=True)
PPTX = DECKS / "测试方案.pptx"

ENV = dict(os.environ)
ENV["PPTX_FINDER_DATA_DIR"] = str(DATA)
ENV["PPTX_FINDER_ROOTS"] = str(DECKS)  # 限搜索索引范围（版本 watcher 走全盘 default_watch_paths）


def _cleanup() -> None:
    shutil.rmtree(_data_tmp, ignore_errors=True)
    shutil.rmtree(DECKS, ignore_errors=True)


def count_versions() -> int:
    os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
    from pptx_finder.versioning.manager import VersionManager
    m = VersionManager()
    n = len(m.list_versions(str(PPTX)))
    m.stop()
    return n


def main() -> int:
    if not EXE.exists():
        print("FAIL: exe 不存在", EXE)
        _cleanup()
        return 1

    # 预置：造 pptx + 源码建 v1
    fx.make_pptx(PPTX, [{"body": "封面 frozen 测试"}, {"body": "第二页 量子计算 章节"}])
    os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
    from pptx_finder.versioning.manager import VersionManager
    pre = VersionManager()
    pre.catch_up_root(str(DECKS))
    n1 = len(pre.list_versions(str(PPTX)))
    pre.stop()
    print(f"预置：建 v1 后版本数 = {n1}（期望 1）")

    print(f"启动 frozen exe: {EXE.name} ...")
    proc = subprocess.Popen([str(EXE)], env=ENV)
    time.sleep(8)  # 等启动 + watcher observer 就绪
    alive = proc.poll() is None
    print(f"层1 · exe 启动 8s 后存活（不崩溃）= {alive}")
    if not alive:
        print(f"FAIL: exe 退出码 {proc.returncode} —— 启动即崩")
        _cleanup()
        return 1

    # 层2：改存 Desktop 下 pptx → 全盘 watcher 应自动留版本
    time.sleep(1)
    fx.make_pptx(PPTX, [{"body": "封面 改稿"}, {"body": "第二页 经典计算"}, {"body": "新增第三页"}])
    print("已改存 pptx（Desktop 下），等全盘 watcher 防抖(1.5s)+快照 ...")
    time.sleep(6)

    proc.terminate()
    try:
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    time.sleep(0.5)

    n2 = count_versions()
    print(f"层2 · 改存后版本数 = {n2}（期望 2 = 全盘 watcher 在 frozen 下端到端工作）")

    ok = alive and n1 == 1 and n2 == 2
    print(f"\n=== frozen 全盘监听验证：{'PASS' if ok else 'FAIL'} ===")
    _cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
