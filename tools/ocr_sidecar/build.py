"""构建并发布「识别组件」——即按需下载的 OCR 侧车。

产出两样东西：
    dist/ocr/pptdoctor-ocr.exe 等文件      —— 侧车本体（含模型与 onnxruntime）
    dist/ocr/component.json + files/<sha>  —— 内容寻址清单，与增量更新同格式

为什么单独打包：这套依赖（onnxruntime + numpy + opencv + 模型）约 65 MB，塞进
主程序会让所有人的绿色包翻三倍，包括从不用这个功能的人。做成按需下载之后，
主程序的依赖清单一个字都不用改。

用法（需要一个装了 rapidocr-onnxruntime 的环境）：
    python tools/ocr_sidecar/build.py --python <那个环境的 python.exe>
构建完把 dist/ocr/ 整个目录传到更新源的 /ocr/ 下即可。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# 控制台默认可能是 GBK/cp1252，构建日志里有中文与箭头，先钉住 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NAME = "pptdoctor-ocr"

#: 构建产物里明确用不到、可以安全删掉的东西。OCR 只用 cv2 的
#: resize/warpPerspective/findContours 这类基础函数，视频编解码与人脸检测数据
#: 纯属搭车——单是 ffmpeg 那个 DLL 就 30 MB。
PRUNE_GLOBS = (
    "_internal/cv2/opencv_videoio_ffmpeg*.dll",
    "_internal/cv2/data/*",
    "_internal/cv2/misc/**/*",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_exe(python_exe: str, work: Path) -> Path:
    """用 PyInstaller 把 sidecar.py 打成 onedir。onedir 而不是 onefile：
    onefile 每次启动都要解包 65 MB 到临时目录，识别一张图的开销会翻好几倍。"""
    dist = work / "pyi-dist"
    build = work / "pyi-build"
    cmd = [
        python_exe, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", NAME, "--console",
        "--distpath", str(dist), "--workpath", str(build),
        "--specpath", str(work),
        "--collect-all", "rapidocr_onnxruntime",
        "--collect-binaries", "onnxruntime",
        str(HERE / "sidecar.py"),
    ]
    print("[build]", " ".join(cmd))
    subprocess.run(cmd, check=True)
    out = dist / NAME
    if not (out / f"{NAME}.exe").is_file():
        raise SystemExit(f"构建未产出 {NAME}.exe：{out}")
    return out


def prune(root: Path) -> int:
    """删掉确定用不到的搭车文件，返回省下的字节数。"""
    saved = 0
    for pattern in PRUNE_GLOBS:
        for path in root.glob(pattern):
            try:
                if path.is_file():
                    saved += path.stat().st_size
                    path.unlink()
                elif path.is_dir():
                    saved += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
    return saved


def make_manifest(root: Path, version: str) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        files[rel] = {"hash": sha256_file(path), "size": path.stat().st_size}
    return {"version": version, "files": files}


def publish(source: Path, out_dir: Path, version: str) -> dict:
    """打成一个 component.zip 并写 component.json（含逐文件 sha256）。

    为什么是单个 zip 而不是像自动更新那样按内容寻址分块：这是一次性安装的组件，
    没有「跨版本复用未变文件」的收益，而 zip 的压缩率实打实——DLL 与 .pyd 压下来
    通常能省掉一半以上下载量，而用户真正付的代价就是下载量。
    校验仍然是两层：先验 zip 整体 sha256，解开后再逐文件对 manifest 里的 sha256。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = make_manifest(source, version)
    archive = out_dir / "component.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in manifest["files"]:
            z.write(source / rel, rel)
    manifest["archive"] = {
        "name": archive.name,
        "hash": sha256_file(archive),
        "size": archive.stat().st_size,
    }
    (out_dir / "component.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable,
                        help="装了 rapidocr-onnxruntime 的解释器")
    parser.add_argument("--version", default="")
    parser.add_argument("--out", default=str(ROOT / "dist" / "ocr"))
    parser.add_argument("--skip-build", action="store_true",
                        help="复用上次的构建产物，只重新发布")
    args = parser.parse_args(argv)

    work = ROOT / "build" / "ocr-sidecar"
    work.mkdir(parents=True, exist_ok=True)
    version = args.version
    if not version:
        sys.path.insert(0, str(HERE))
        import sidecar  # noqa: E402
        version = sidecar.__version__

    source = work / "pyi-dist" / NAME
    if not args.skip_build:
        source = build_exe(args.python, work)
    if not source.is_dir():
        raise SystemExit(f"找不到构建产物：{source}")

    saved = prune(source)
    out_dir = Path(args.out)
    manifest = publish(source, out_dir, version)
    total = sum(m["size"] for m in manifest["files"].values())
    archive = manifest["archive"]
    print(f"\n识别组件 v{version}")
    print(f"  剔除搭车文件省下 {saved / 1024 / 1024:.1f} MB")
    print(f"  文件 {len(manifest['files'])} 个，解压后 {total / 1024 / 1024:.1f} MB")
    print(f"  下载体积 {archive['size'] / 1024 / 1024:.1f} MB"
          f"（压缩到 {archive['size'] / max(1, total):.0%}）")
    print(f"  已发布到 {out_dir}")
    print("  把 component.json 与 component.zip 传到更新源的 /ocr/ 下即可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
