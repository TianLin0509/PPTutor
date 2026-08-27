"""OCR 侧车：主程序不碰 onnxruntime，只用 stdin/stdout 说 JSON。

为什么做成独立进程而不是直接 import：
  识别模型那套依赖（onnxruntime + numpy + opencv）打包后约 65 MB，塞进主程序
  等于让所有人——包括从不用这个功能的人——的绿色包翻三倍。做成按需下载的
  独立 exe 之后，主程序的依赖清单一个字都不用改，基础包仍是约 37 MB。
  副作用还挺好：识别崩了也只崩侧车，主程序照常。

一次调用可以处理多张图（模型加载只付一次）。侧车不常驻，用完即退，不占内存。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import data_dir

log = logging.getLogger(__name__)

SIDE_CAR_DIRNAME = "ocr"
SIDE_CAR_EXE = "pptdoctor-ocr.exe"
#: 侧车与模型一起作为「识别组件」按需下载；这个文件写入版本号，用来判断是否需要更新
VERSION_FILE = "component.json"
RECOGNIZE_TIMEOUT_SEC = 120.0


class OcrUnavailable(RuntimeError):
    """识别组件没装、或装了但起不来。UI 据此提示下载，而不是当成识别失败。"""


def component_dir() -> Path:
    return data_dir() / SIDE_CAR_DIRNAME


def _dev_command() -> list[str] | None:
    """开发期用环境变量直接指到源码侧车，免得每改一行都要重新打包。"""
    raw = os.environ.get("PPTUTOR_OCR_CMD", "").strip()
    if not raw:
        return None
    try:
        parts = json.loads(raw)
        return [str(p) for p in parts] if isinstance(parts, list) else None
    except json.JSONDecodeError:
        return None


def command() -> list[str] | None:
    """返回可执行的侧车命令；未安装返回 None。"""
    dev = _dev_command()
    if dev:
        return dev
    exe = component_dir() / SIDE_CAR_EXE
    return [str(exe)] if exe.is_file() else None


def is_installed() -> bool:
    return command() is not None


def installed_version() -> str:
    try:
        data = json.loads((component_dir() / VERSION_FILE).read_text("utf-8"))
        return str(data.get("version") or "")
    except (OSError, ValueError):
        return ""


def component_size_bytes() -> int:
    root = component_dir()
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def recognize(images) -> dict[str, list[dict]]:
    """识别若干张图，返回 {图片路径: [{"text","box","score"}, ...]}。

    box 为 (x0, y0, x1, y1)，源图像素坐标。
    """
    paths = [str(Path(p).resolve()) for p in images]
    if not paths:
        return {}
    cmd = command()
    if cmd is None:
        raise OcrUnavailable("识别组件尚未安装")

    with tempfile.TemporaryDirectory(prefix="pptdoctor-ocr-") as tmp:
        request = Path(tmp) / "request.json"
        response = Path(tmp) / "response.json"
        request.write_text(json.dumps({"images": paths}, ensure_ascii=False),
                           encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            done = subprocess.run(
                [*cmd, "--request", str(request), "--response", str(response)],
                capture_output=True, timeout=RECOGNIZE_TIMEOUT_SEC,
                creationflags=creationflags, check=False)
        except FileNotFoundError as exc:
            raise OcrUnavailable(f"识别组件不可执行：{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OcrUnavailable("识别超时，组件可能已损坏") from exc
        if done.returncode != 0 or not response.is_file():
            tail = (done.stderr or b"").decode("utf-8", "replace")[-400:]
            raise OcrUnavailable(f"识别组件返回 {done.returncode}：{tail.strip()}")
        payload = json.loads(response.read_text("utf-8"))

    out: dict[str, list[dict]] = {}
    for key, rows in (payload.get("results") or {}).items():
        out[key] = [
            {"text": r["text"], "box": tuple(r["box"]), "score": float(r.get("score", 1.0))}
            for r in rows
        ]
    return out


def recognize_one(image) -> list[dict]:
    path = str(Path(image).resolve())
    return recognize([path]).get(path, [])


# ---------- 按需下载 ----------
#: 组件清单与内容寻址块的地址。与增量更新同源、同格式，因此下面直接复用
#: updater 里那套「逐块 sha256 校验 + 非法路径整批拒绝」的逻辑，不再写一遍。
COMPONENT_MANIFEST = "component.json"


#: 主源之外的备用地址：GitHub Release 上的同名资产。
#: 为什么要备用源：识别组件 80 MB，托管在自建更新源上；那台机器不可达（或还没传）
#: 时，下载按钮就是死的。GitHub Release 是同一个仓库的官方分发位，不需要额外凭据，
#: 拿来做兜底最省事。两个源用的是同一份清单、同一套两层 sha256 校验，安全性一致。
COMPONENT_FALLBACK_URL = (
    "https://github.com/TianLin0509/PPTutor/releases/download/ocr-v1.0.0")


def component_base_urls() -> list[str]:
    from .config import update_base_url
    primary = update_base_url().rstrip("/") + "/ocr"
    return [primary, COMPONENT_FALLBACK_URL]


def component_base_url() -> str:
    return component_base_urls()[0]


def fetch_component_manifest(timeout: float = 8.0) -> dict:
    """按顺序试各个源，第一个能取到清单的就用它。

    清单里会带上 `_base`，让 install() 从**同一个源**取压缩包——不能出现
    「清单来自 A、包来自 B」的混搭，那样哈希校验的语义就散了。
    """
    import json as _json
    import urllib.request

    last = None
    for base in component_base_urls():
        try:
            req = urllib.request.Request(base + "/" + COMPONENT_MANIFEST,
                                         headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = _json.loads(response.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("files"):
                data["_base"] = base
                return data
        except Exception as exc:      # noqa: BLE001 逐个源试，最后一个失败才抛
            last = exc
            log.info("识别组件清单取不到（%s）：%s", base, exc)
    raise OcrUnavailable(f"识别组件清单不可达：{last}")


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def install(manifest: dict | None = None, *, progress=None, cancel=None) -> str:
    """下载识别组件并原子安装，返回安装好的版本号。

    两层校验：先验整包 sha256，解开后再逐文件对清单里的 sha256。全部就绪之后才
    整体换入——中途失败或取消都不会留下半套组件。这条铁律与自动更新一致：
    宁可这次不装，也不做部分应用。

    压缩包内的每条路径都过一遍更新器的路径守卫，越界条目整批拒绝——zip 解压的
    路径穿越（`../`）是老掉牙但依然有效的攻击面。
    """
    import hashlib
    import shutil
    import urllib.request
    import zipfile

    from . import updater

    manifest = manifest or fetch_component_manifest()
    files = manifest.get("files") or {}
    archive_meta = manifest.get("archive") or {}
    archive_name = updater.safe_relpath(str(archive_meta.get("name") or "component.zip"))
    archive_hash = str(archive_meta.get("hash") or "")
    if not files or not _HEX64.fullmatch(archive_hash):
        raise OcrUnavailable("组件清单不完整或哈希非法")
    expected = {}
    for rel, meta in files.items():
        digest = str(meta.get("hash") or "")
        if not _HEX64.fullmatch(digest):
            raise OcrUnavailable(f"组件清单含非法哈希：{rel}")
        expected[updater.safe_relpath(rel)] = digest
    total = int(archive_meta.get("size", 0)) or 1

    staging = Path(tempfile.mkdtemp(prefix="pptdoctor-ocr-dl-"))
    try:
        bundle = staging / "component.zip"
        hasher = hashlib.sha256()
        done = 0
        base = str(manifest.get("_base") or component_base_url())
        url = base + "/" + archive_name
        with urllib.request.urlopen(url, timeout=60) as src, open(bundle, "wb") as out:
            while True:
                chunk = src.read(1 << 18)
                if not chunk:
                    break
                out.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
                if cancel is not None and cancel():
                    raise InterruptedError("下载已取消")
        if hasher.hexdigest() != archive_hash:
            raise OcrUnavailable("组件压缩包校验失败")

        unpacked = staging / "unpacked"
        with zipfile.ZipFile(bundle) as z:
            for item in z.infolist():
                if item.is_dir():
                    continue
                rel = updater.safe_relpath(item.filename)
                if rel not in expected:
                    raise OcrUnavailable(f"压缩包含清单之外的文件：{item.filename}")
                dest = unpacked / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
        for rel, digest in expected.items():
            path = unpacked / rel
            if not path.is_file():
                raise OcrUnavailable(f"组件缺少文件：{rel}")
            check = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    check.update(chunk)
            if check.hexdigest() != digest:
                raise OcrUnavailable(f"组件文件校验失败：{rel}")
        (unpacked / VERSION_FILE).write_text(
            json.dumps({"version": str(manifest.get("version") or "")},
                       ensure_ascii=False), encoding="utf-8")
        bundle.unlink(missing_ok=True)
        staging_payload = unpacked

        target = component_dir()
        target.parent.mkdir(parents=True, exist_ok=True)
        retired = target.with_name(target.name + ".old")
        shutil.rmtree(retired, ignore_errors=True)
        if target.exists():
            os.replace(target, retired)
        try:
            os.replace(staging_payload, target)
        except OSError:
            # 跨卷时 os.replace 不可用，退回复制；失败仍要把旧组件放回去
            try:
                shutil.copytree(staging_payload, target)
            except Exception:
                if retired.exists() and not target.exists():
                    os.replace(retired, target)
                raise
        shutil.rmtree(retired, ignore_errors=True)
        return installed_version()
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def uninstall() -> bool:
    import shutil

    target = component_dir()
    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def self_test() -> str:
    """给「健康诊断」用的一行状态。"""
    if not is_installed():
        return "imgtext_ocr: 未安装"
    size_mb = component_size_bytes() / (1024 * 1024)
    return (f"imgtext_ocr: 已安装 version={installed_version() or '?'} "
            f"size={size_mb:.0f}MB path={component_dir()}")


if __name__ == "__main__":  # 手动排查：python -m pptx_finder.imgtext_ocr 图片
    sys.stdout.reconfigure(encoding="utf-8")
    print(self_test())
    for arg in sys.argv[1:]:
        rows = recognize_one(arg)
        print(f"{arg}: {len(rows)} 行")
        for r in rows[:8]:
            print(f"   {r['score']:.3f} {r['box']} {r['text']}")
