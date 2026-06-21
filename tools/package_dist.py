"""把 dist/PPTutor 打成「干净」可分发 zip：只含程序本身，绝不夹带 demo_decks / 索引库 / 用户数据。

用法: python tools/package_dist.py

血泪（2026-06-21）：曾把整个项目文件夹（含 demo_decks 的假样本 PPT「Q3算力方案」等）一起发给
同学，对方首次运行 PPTutor 全盘扫描把它们索引进「最近活跃」，看着像机密文件泄露（其实是 demo
样本）。本脚本只打包 dist/PPTutor，并在打包前硬校验包内无任何 .pptx/.ppt/.db，杜绝再次误发。
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import __version__
from pptx_finder.updater import MANIFEST_NAME, build_manifest

DIST = ROOT / "dist" / "PPTutor"
LEAK_EXTS = {".pptx", ".ppt", ".db", ".db-wal", ".db-shm"}


def main() -> int:
    if not DIST.is_dir():
        print(f"[!] 找不到 {DIST}\n  先构建: uv run pyinstaller pptx-finder.spec --noconfirm")
        return 2

    # 安全闸：分发包内绝不能有用户数据 / demo 样本 / 索引库
    leaks = [p for p in DIST.rglob("*") if p.is_file() and p.suffix.lower() in LEAK_EXTS]
    if leaks:
        print("[!] 拒绝打包：dist 内混入了不该分发的数据文件，请清理后重试：")
        for p in leaks[:20]:
            print("   ", p.relative_to(DIST))
        return 1

    # 刷新增量更新清单（随包发布，供自动更新比对）
    m = build_manifest(DIST, __version__, f"PPTutor v{__version__}")
    (DIST / MANIFEST_NAME).write_text(json.dumps(m, ensure_ascii=False, indent=0), encoding="utf-8")

    out = ROOT / "dist" / f"PPTutor-v{__version__}.zip"
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(DIST.rglob("*")):
            if p.is_file():
                z.write(p, str(Path("PPTutor") / p.relative_to(DIST)))
                n += 1
    mb = out.stat().st_size / 1024 / 1024
    print(f"[OK] 干净分发包: {out}")
    print(f"  {n} 文件, {mb:.1f} MB（仅 PPTutor 程序，无 demo / 无索引库 / 无用户数据）")
    print("  - 只发这个 zip 给同学；别发整个项目文件夹（demo_decks 假样本会被对方全盘扫描索引）。")
    print("  - 首次手动发一次，装上后之后自动增量更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
