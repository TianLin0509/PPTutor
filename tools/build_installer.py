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

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import __version__  # noqa: E402
from pptx_finder.config import DIST_DIR_NAME, EXE_NAME  # noqa: E402
from pptx_finder.updater import safe_relpath  # noqa: E402

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


def check_runtime(root: Path) -> None:
    """初始化 Python 之前就要用的库，不能等应用内异常处理来兜底。"""
    runtime = root / "_internal"
    if not any(p.stat().st_size for p in runtime.glob("python3[0-9]*.dll")):
        raise ValueError(f"缺少 Python 运行库：{runtime}")
    with zipfile.ZipFile(runtime / "base_library.zip") as archive:
        required = {"encodings/__init__.pyc", "encodings/aliases.pyc", "encodings/utf_8.pyc"}
        if not required.issubset(archive.namelist()):
            raise ValueError("base_library.zip 缺少 Python 启动所需的 encodings 模块")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"base_library.zip 内容损坏：{bad}")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_ocr_bundle(root: Path, manifest: dict) -> None:
    files = manifest.get("files") or {}
    if "pptdoctor-ocr.exe" not in files or not manifest.get("version"):
        raise ValueError("OCR 清单缺少入口或版本")
    for rel, meta in files.items():
        path = root / safe_relpath(rel)
        if (not path.is_file() or path.stat().st_size != meta["size"]
                or _sha256(path) != meta["hash"]):
            raise ValueError(f"OCR 文件缺失或校验失败：{rel}")
    check_runtime(root)


def check_ocr_archive(root: Path, manifest: dict) -> Path:
    meta = manifest["archive"]
    archive = root / safe_relpath(meta["name"])
    if meta["name"] != "component.zip":
        raise ValueError("内置 OCR 压缩包必须命名为 component.zip")
    if archive.stat().st_size != meta["size"] or _sha256(archive) != meta["hash"]:
        raise ValueError("OCR 压缩包校验失败")
    return archive


def bundle_ocr(component: Path, dist: Path) -> Path:
    """校验所有文件，分发压缩包以保留短路径预算；首次转字再在本地展开。"""
    manifest = json.loads((component / "component.json").read_text("utf-8"))
    archive = check_ocr_archive(component, manifest)
    runtime = dist / "_internal"
    runtime.mkdir(parents=True, exist_ok=True)
    target = runtime / "ocr"
    # 要更换已打包组件时重新构建 dist，避免失败后留下混版/额外文件。
    if target.exists():
        check_ocr_archive(target, manifest)
        if json.loads((target / "component.json").read_text("utf-8")) != manifest:
            raise ValueError("已有 OCR 清单不同，请重新构建 dist")
        return target
    with tempfile.TemporaryDirectory(prefix="ocr-bundle-", dir=runtime) as tmp:
        unpacked = Path(tmp) / "payload"
        unpacked.mkdir()
        seen = set()
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                rel = safe_relpath(item.filename)
                if item.is_dir():
                    continue
                if rel not in manifest["files"] or rel.lower() in seen:
                    raise ValueError(f"OCR 压缩包有未知或重复文件：{rel}")
                seen.add(rel.lower())
                dest = unpacked / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        check_ocr_bundle(unpacked, manifest)
        packed = Path(tmp) / "packed"
        packed.mkdir()
        shutil.copyfile(archive, packed / "component.zip")
        (packed / "component.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        packed.rename(target)
    return target


def check_dist() -> int:
    if not DIST.is_dir():
        print(f"[!] 找不到 {DIST}\n  先构建: uv run pyinstaller pptx-finder.spec --noconfirm")
        return 2
    exe = DIST / EXE_NAME
    if not exe.is_file():
        print(f"[!] {exe} 不存在")
        return 2
    try:
        check_runtime(DIST)
        bundled = DIST / "_internal" / "ocr"
        if bundled.exists():
            check_ocr_archive(bundled, json.loads((bundled / "component.json").read_text("utf-8")))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"[!] 拒绝打包不完整的运行库：{exc}")
        return 1
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-ocr", action="store_true", help="内置 OCR，首次转字无需下载")
    parser.add_argument("--ocr-component", type=Path, default=ROOT / "dist" / "ocr",
                        help="含 component.json/component.zip 的离线组件目录")
    args = parser.parse_args(argv)
    rc = check_dist()
    if rc:
        return rc
    if args.full_ocr:
        try:
            bundle_ocr(args.ocr_component, DIST)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            print(f"[!] 完整包 OCR 准备失败：{exc}")
            return 1
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
    full = (DIST / "_internal" / "ocr").is_dir()
    suffix = "-Full" if full else ""
    out = OUT_DIR / f"PPT-Doctor-Setup-v{__version__}{suffix}.exe"
    if out.exists():
        out.unlink()

    cmd = [str(iscc), f"/DAppVersion={__version__}", str(ISS)]
    if full:
        cmd.insert(2, "/DFullOcr=1")
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
