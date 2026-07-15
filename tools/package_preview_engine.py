r"""制作带独立 PPT 图片预览引擎的全量绿色包。

默认从 The Document Foundation 官方镜像下载 LibreOffice Portable Standard，
静默解包后与 ``dist/PPT Doctor`` 一起写入一个全量 zip。基础增量更新清单
不会包含 preview-engine，因此以后更新 PPT Doctor 时不会误删这个可选引擎。

用法：
  uv run python tools/package_preview_engine.py
  uv run python tools/package_preview_engine.py --engine-dir D:\LibreOfficePortable
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import __version__  # noqa: E402
from pptx_finder.updater import MANIFEST_NAME, build_manifest  # noqa: E402

LIBREOFFICE_VERSION = "26.2.4"
INSTALLER_NAME = (
    f"LibreOfficePortable_{LIBREOFFICE_VERSION}_MultilingualStandard.paf.exe"
)
OFFICIAL_URL = (
    "https://download.documentfoundation.org/libreoffice/portable/"
    f"{LIBREOFFICE_VERSION}/{INSTALLER_NAME}"
)
MIN_INSTALLER_BYTES = 180 * 1024 * 1024
LEAK_SUFFIXES = {".ppt", ".pptx", ".db", ".db-wal", ".db-shm"}


def _portable_soffice(root: Path) -> Path:
    return root / "App" / "libreoffice" / "program" / "soffice.com"


def _validated_portable_root(candidate: Path) -> Path | None:
    roots = [candidate, candidate / "LibreOfficePortable"]
    for root in roots:
        if _portable_soffice(root).is_file():
            return root
    return None


def download_installer(destination: Path, url: str = OFFICIAL_URL) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size >= MIN_INSTALLER_BYTES:
        print(f"[OK] 复用已下载的官方安装包：{destination}")
        return destination
    fd, temp_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        print(f"[1/3] 下载 LibreOffice Portable：{url}")
        with urllib.request.urlopen(url, timeout=60) as response, temp_path.open("wb") as out:
            total = int(response.headers.get("Content-Length", "0") or 0)
            received = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                out.write(block)
                received += len(block)
                if total and received % (32 * 1024 * 1024) < len(block):
                    print(f"      {received / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MB")
        if temp_path.stat().st_size < MIN_INSTALLER_BYTES:
            raise RuntimeError("官方安装包下载不完整")
        os.replace(temp_path, destination)
        return destination
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def extract_portable(installer: Path, cache_dir: Path) -> Path:
    cached_candidates = [cache_dir / "extracted"]
    if cache_dir.is_dir():
        cached_candidates.extend(sorted(cache_dir.glob("extracted-*"), reverse=True))
    for candidate in cached_candidates:
        cached = _validated_portable_root(candidate)
        if cached is not None:
            print(f"[OK] 复用已解包的预览引擎：{cached}")
            return cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    extract_dir = Path(tempfile.mkdtemp(prefix="extracted-", dir=cache_dir))
    print(f"[2/3] 解包到：{extract_dir}")
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    seven_zip = (
        shutil.which("7z")
        or shutil.which("7zz")
        or (r"C:\Program Files\7-Zip\7z.exe" if os.name == "nt" else "")
    )
    if seven_zip and Path(seven_zip).is_file():
        # PAF is an NSIS archive.  Direct extraction is deterministic and does
        # not trust installer switches that can still show a modal window on
        # some PortableApps installer builds.
        command = [
            str(seven_zip),
            "x",
            "-tNsis",
            "-y",
            "-bso0",
            "-bsp0",
            f"-o{extract_dir}",
            str(installer),
        ]
    else:
        command = [
            str(installer),
            f"/DESTINATION={extract_dir}",
            "/AUTOCLOSE=true",
            "/HIDEINSTALLER=true",
            "/SILENT=true",
        ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20 * 60,
        check=False,
        creationflags=creationflags,
    )
    root = _validated_portable_root(extract_dir)
    if completed.returncode != 0 or root is None:
        raise RuntimeError(
            "LibreOffice Portable 解包失败："
            f"rc={completed.returncode} stderr={completed.stderr[-500:]}"
        )
    return root


def _write_tree(zf: zipfile.ZipFile, source: Path, archive_root: Path) -> int:
    count = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        zf.write(path, str(archive_root / path.relative_to(source)))
        count += 1
    return count


def build_all_in_one(app_dist: Path, portable_root: Path, output: Path) -> tuple[int, int]:
    if not app_dist.is_dir():
        raise FileNotFoundError(f"未找到主程序构建目录：{app_dist}")
    if _validated_portable_root(portable_root) != portable_root:
        raise FileNotFoundError(
            "无效的 LibreOfficePortable 目录，缺少 "
            "LibreOfficePortable/App/libreoffice/program/soffice.com"
        )
    leaks = [
        path for path in app_dist.rglob("*")
        if path.is_file() and path.suffix.lower() in LEAK_SUFFIXES
    ]
    if leaks:
        raise RuntimeError(f"主程序目录混入用户 PPT/数据库：{leaks[0]}")

    # 主程序清单必须在引擎写入 zip 之前生成；这样自动更新只管理主程序，
    # 后续版本不会把用户另行安装的 preview-engine 当成旧文件删除。
    manifest = build_manifest(app_dist, __version__, f"PPT Doctor v{__version__}")
    (app_dist / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_suffix(output.suffix + ".tmp")
    temp_output.unlink(missing_ok=True)
    print(f"[3/3] 制作带图片预览引擎的全量包：{output}")
    app_count = engine_count = 0
    with zipfile.ZipFile(
        temp_output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as zf:
        app_count = _write_tree(zf, app_dist, Path("PPT Doctor"))
        engine_count = _write_tree(
            zf,
            portable_root,
            Path("PPT Doctor") / "preview-engine" / "LibreOfficePortable",
        )
        zf.writestr(
            "PPT Doctor/预览引擎说明.txt",
            "本包内含 LibreOffice Portable，仅用于 PowerPoint 正在运行时隔离生成原始页图。\n"
            "PPT Doctor 不会让它接管或关闭你已经打开的 PowerPoint。\n"
            f"版本：{LIBREOFFICE_VERSION}\n来源：{OFFICIAL_URL}\n"
            "许可：https://www.libreoffice.org/licenses/\n",
        )
    os.replace(temp_output, output)
    return app_count, engine_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-dir", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "build" / f"preview-engine-{LIBREOFFICE_VERSION}",
    )
    parser.add_argument("--app-dist", type=Path, default=ROOT / "dist" / "PPT Doctor")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / f"PPT-Doctor-v{__version__}-with-preview-engine.zip",
    )
    args = parser.parse_args(argv)

    if args.engine_dir:
        portable_root = _validated_portable_root(args.engine_dir)
        if portable_root is None:
            raise SystemExit(f"[!] 无效的 --engine-dir：{args.engine_dir}")
    else:
        installer = args.installer or args.cache_dir / INSTALLER_NAME
        if not installer.is_file() or installer.stat().st_size < MIN_INSTALLER_BYTES:
            installer = download_installer(installer)
        portable_root = extract_portable(installer, args.cache_dir)

    app_count, engine_count = build_all_in_one(
        args.app_dist.resolve(),
        portable_root.resolve(),
        args.output.resolve(),
    )
    size_mb = args.output.resolve().stat().st_size / 1024 / 1024
    print(
        f"[OK] {args.output.resolve()} | 主程序 {app_count} 文件 | "
        f"预览引擎 {engine_count} 文件 | {size_mb:.1f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
