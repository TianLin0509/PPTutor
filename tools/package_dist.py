"""把 dist/PPT-Doctor 打成「干净」可分发 zip：只含程序本身，绝不夹带 demo_decks / 索引库 / 用户数据。

用法: python tools/package_dist.py

血泪（2026-06-21）：曾把整个项目文件夹（含 demo_decks 的假样本 PPT「Q3算力方案」等）一起发给
同学，对方首次运行 PPT Doctor 全盘扫描把它们索引进「最近活跃」，看着像机密文件泄露（其实是 demo
样本）。本脚本只打包 dist/PPT-Doctor，并在打包前硬校验包内无任何 .pptx/.ppt/.db，杜绝再次误发。
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import __version__  # noqa: E402
from pptx_finder.updater import MANIFEST_NAME, build_manifest  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows/CI may inherit cp1252.
except Exception:  # noqa: BLE001 Console encoding must not block packaging.
    pass

from pptx_finder.config import DIST_DIR_NAME  # noqa: E402

# v1.5.2 起产物目录不带空格。旧目录仍认，方便手上还留着上一版 dist 的机器。
_LEGACY_DIST_NAME = "PPT Doctor"


def _resolve_dist() -> Path:
    new = ROOT / "dist" / DIST_DIR_NAME
    if new.is_dir():
        return new
    legacy = ROOT / "dist" / _LEGACY_DIST_NAME
    return legacy if legacy.is_dir() else new


DIST = _resolve_dist()
LEAK_EXTS = {".pptx", ".ppt", ".db", ".db-wal", ".db-shm"}
# 「图片转可编辑文字」的空白母版（28 KB）是程序资源，不是用户数据——spec 里显式
# 打进来的。此前它没在白名单上，闸门一律拒绝，于是官方打包路径根本跑不通，只能
# 手工压 zip；v1.5.1 的分发包因此漏掉了 manifest.json（首次启动要自己重算一遍全量哈希）。
# 白名单按**精确相对路径**匹配，不是按扩展名放行：新混进来的 pptx 照样拦。
ALLOWED_DATA_FILES = {"_internal/assets/blank_16x9.pptx"}


def main() -> int:
    if not DIST.is_dir():
        print(f"[!] 找不到 {DIST}\n  先构建: uv run pyinstaller pptx-finder.spec --noconfirm")
        return 2

    # 安全闸：分发包内绝不能有用户数据 / demo 样本 / 索引库
    leaks = [
        p for p in DIST.rglob("*")
        if p.is_file() and p.suffix.lower() in LEAK_EXTS
        and p.relative_to(DIST).as_posix() not in ALLOWED_DATA_FILES
    ]
    if leaks:
        print("[!] 拒绝打包：dist 内混入了不该分发的数据文件，请清理后重试：")
        for p in leaks[:20]:
            print("   ", p.relative_to(DIST))
        return 1

    # 刷新增量更新清单（随包发布，供自动更新比对）
    m = build_manifest(DIST, __version__, f"PPT Doctor v{__version__}")
    (DIST / MANIFEST_NAME).write_text(json.dumps(m, ensure_ascii=False, indent=0), encoding="utf-8")

    out = ROOT / "dist" / f"PPT-Doctor-v{__version__}.zip"
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(DIST.rglob("*")):
            if p.is_file():
                # 包内目录名恒用新名：用户解压出来的路径不能带空格，
                # 哪怕本机 dist 目录还是旧名。
                z.write(p, str(Path(DIST_DIR_NAME) / p.relative_to(DIST)))
                n += 1
    mb = out.stat().st_size / 1024 / 1024
    print(f"[OK] 干净分发包: {out}")
    print(f"  {n} 文件, {mb:.1f} MB（仅 PPT Doctor 程序，无 demo / 无索引库 / 无用户数据）")
    print("  - 只发这个 zip 给同学；别发整个项目文件夹（demo_decks 假样本会被对方全盘扫描索引）。")
    print("  - 首次手动发一次，装上后之后自动增量更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
