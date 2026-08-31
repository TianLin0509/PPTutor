# -*- coding: utf-8 -*-
"""用 Inno Setup 把 dist/PPT-Doctor 打成一个 .exe 安装程序。

用法：
    uv run python tools/build_installer.py
    ISCC=D:\\path\\to\\ISCC.exe uv run python tools/build_installer.py

为什么要有这个脚本，而不是直接跑 ISCC：**版本号**。installer.iss 里原来写死
`#define AppVersion "1.3.2"`，一路漂到 1.5.3 都没被发现——因为安装器从没真正构建过。
现在版本号只能由这里从 `pptx_finder.__version__` 取出、经 `/DAppVersion=` 传进去，
.iss 里有 `#ifndef AppVersion #error`，漏传直接编译失败，漂不了。

另外在编译前硬校验一遍 dist：目录在不在、exe 的版本资源对不对得上、有没有混进
不该分发的数据文件。安装器比 zip 更难回滚（用户机器上真装了东西），所以闸门要更严。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import __version__  # noqa: E402
from pptx_finder.config import DIST_DIR_NAME, EXE_NAME  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DIST = ROOT / "dist" / DIST_DIR_NAME
ISS = ROOT / "tools" / "installer.iss"
OUT_DIR = ROOT / "artifacts"
LEAK_EXTS = {".pptx", ".ppt", ".db", ".db-wal", ".db-shm"}
ALLOWED_DATA_FILES = {"_internal/assets/blank_16x9.pptx"}

_ISCC_CANDIDATES = [
    Path(r"C:\Users\lintian\pptx-finder\scratchpad\inno62") / "{app}" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
]


def find_iscc() -> Path | None:
    env = os.environ.get("ISCC", "").strip()
    if env and Path(env).is_file():
        return Path(env)
    which = shutil.which("ISCC.exe")
    if which:
        return Path(which)
    for c in _ISCC_CANDIDATES:
        if c.is_file():
            return c
    return None


def check_dist() -> int:
    if not DIST.is_dir():
        print(f"[!] 找不到 {DIST}\n  先构建: uv run pyinstaller pptx-finder.spec --noconfirm")
        return 2
    exe = DIST / EXE_NAME
    if not exe.is_file():
        print(f"[!] {exe} 不存在")
        return 2
    leaks = [
        p for p in DIST.rglob("*")
        if p.is_file() and p.suffix.lower() in LEAK_EXTS
        and p.relative_to(DIST).as_posix() not in ALLOWED_DATA_FILES
    ]
    if leaks:
        print("[!] 拒绝打包：dist 内混入了不该分发的数据文件：")
        for p in leaks[:20]:
            print("   ", p.relative_to(DIST))
        return 1
    # exe 的版本资源必须和包版本一致，否则装完「关于」里显示的是另一个版本
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(exe), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        built = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"
        if built != __version__:
            print(f"[!] dist 里的 exe 是 v{built}，但包版本是 v{__version__}；请先重新构建")
            return 1
    except ImportError:
        print("  (跳过 exe 版本资源校验：没有 pywin32)")
    return 0


def main() -> int:
    rc = check_dist()
    if rc:
        return rc
    iscc = find_iscc()
    if iscc is None:
        print("[!] 找不到 ISCC.exe。装 Inno Setup 6，或用 ISCC 环境变量指定路径：")
        for c in _ISCC_CANDIDATES:
            print("   ", c)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"PPT-Doctor-Setup-v{__version__}.exe"
    if out.exists():
        out.unlink()

    cmd = [str(iscc), f"/DAppVersion={__version__}", str(ISS)]
    print(f"[*] {iscc}")
    print(f"[*] 版本 v{__version__}（来自 pptx_finder.__version__）")
    proc = subprocess.run(cmd, cwd=str(ROOT / "tools"), capture_output=True, text=True)
    if proc.returncode != 0:
        print("[!] 编译失败：")
        print(proc.stdout[-4000:])
        print(proc.stderr[-2000:])
        return proc.returncode
    if not out.is_file():
        print(f"[!] 编译报成功但没有产物：{out}")
        print(proc.stdout[-2000:])
        return 1

    mb = out.stat().st_size / 1024 / 1024
    sha = hashlib.sha256(out.read_bytes()).hexdigest().upper()
    print(f"[OK] 安装程序: {out}")
    print(f"  {mb:.1f} MB")
    print(f"  SHA-256 {sha}")
    print("  装到 %LOCALAPPDATA%\\Programs\\PPT-Doctor（免 UAC，且自动更新需要可写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
