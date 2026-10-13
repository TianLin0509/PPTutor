"""OCR 侧车：主程序不碰 onnxruntime，只用 stdin/stdout 说 JSON。

为什么做成独立进程而不是直接 import：
  识别模型那套依赖（onnxruntime + numpy + opencv）打包后约 65 MB，塞进主程序
  等于让所有人——包括从不用这个功能的人——的绿色包翻三倍。做成按需下载的
  独立 exe 之后，基础包可以按需下载；完整包则把同一套侧车放在安装目录内。
  副作用还挺好：识别崩了也只崩侧车，主程序照常。

批量转换用完即退；截图连续识别复用模型，空闲 45 秒自动释放。
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


def active_component_dir() -> Path:
    """内置包使用独立本地缓存，保留用户原来下载的组件。"""
    if bundled_component_dir() is not None:
        return data_dir() / "ocr-bundled"
    return component_dir()


def bundled_component_dir() -> Path | None:
    runtime = getattr(sys, "_MEIPASS", None)
    if runtime:
        bundled = Path(runtime) / SIDE_CAR_DIRNAME
        if (bundled / VERSION_FILE).is_file() and (bundled / "component.zip").is_file():
            return bundled
    return None


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
    exe = active_component_dir() / SIDE_CAR_EXE
    bundled = bundled_component_dir()
    if bundled is not None:
        try:
            manifest = json.loads((bundled / VERSION_FILE).read_text("utf-8"))
            marker = active_component_dir() / VERSION_FILE
            cached = json.loads(marker.read_text("utf-8")) if marker.is_file() else {}
            if not exe.is_file() or cached.get("archive_hash") != manifest["archive"]["hash"]:
                install(manifest, archive_path=bundled / "component.zip",
                        target_dir=active_component_dir())
        except (OSError, ValueError, KeyError) as exc:
            raise OcrUnavailable(f"内置识别组件准备失败：{exc}") from exc
    return [str(exe)] if exe.is_file() else None


def is_installed() -> bool:
    # UI 刷新只探测；190 MB 解包留到转换 worker 内第一次真正识别时进行。
    return (bool(_dev_command()) or bundled_component_dir() is not None
            or (component_dir() / SIDE_CAR_EXE).is_file())


def installed_version() -> str:
    try:
        root = bundled_component_dir() or active_component_dir()
        data = json.loads((root / VERSION_FILE).read_text("utf-8"))
        return str(data.get("version") or "")
    except (OSError, ValueError):
        return ""


def component_size_bytes() -> int:
    root = active_component_dir()
    if not root.is_dir():
        root = bundled_component_dir()
        if root is None:
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


def recognize_one_cached(image, *, cancelled=None) -> list[dict]:
    """Screenshot OCR: reuse compatible models briefly, support old components."""
    version = installed_version()
    try:
        compatible = tuple(int(p) for p in version.split('.')) >= (1, 1, 0)
    except ValueError:
        compatible = False
    if not compatible:
        return recognize_one(image)
    cmd = command()
    if cmd is None:
        raise OcrUnavailable('识别组件尚未安装')
    from .ocr_session import session
    path = str(Path(image).resolve())
    rows = session.recognize(cmd, [path], cancelled=cancelled)[path]
    return [{'text': r['text'], 'box': tuple(r['box']), 'score': float(r.get('score', 1))} for r in rows]


# ---------- 按需下载 ----------
#: 组件清单与内容寻址块的地址。与增量更新同源、同格式，因此下面直接复用
#: updater 里那套「逐块 sha256 校验 + 非法路径整批拒绝」的逻辑，不再写一遍。
COMPONENT_MANIFEST = "component.json"


#: GitHub Release 上识别组件的独立 tag（版本节奏与主程序不同步，所以单独打 tag 让 URL 稳定）。
#: v2.0 起对照 AI Hub：GitHub 在前、自建服务器作备用；两个源同一份清单、同一套两层
#: sha256 校验，安全性一致。一个源被代理换成拦截网页、超时或 404，自动换下一个。
COMPONENT_FALLBACK_URL = (
    "https://github.com/TianLin0509/PPTutor/releases/download/ocr-v1.1.0")


def component_sources(kind: str = "ocr", github_base: str = COMPONENT_FALLBACK_URL
                      ) -> list[tuple[str, str]]:
    """[(label, base_url)] 按尝试顺序。显式改了更新地址（测试/灰度）时它排最前。"""
    from .config import update_base_url
    from .updater import _is_default_base
    hosted_root = update_base_url().split(";")[0].rstrip("/")
    hosted = ("self-hosted", hosted_root + "/" + kind)
    github = ("github-release", github_base)
    if os.environ.get("PPTX_FINDER_UPDATE_NO_GITHUB", "").strip() in ("1", "true", "yes"):
        return [hosted]
    if _is_default_base(hosted_root):
        return [github, hosted]
    return [hosted, github]


def component_base_urls() -> list[str]:
    return [base for _label, base in component_sources()]


def component_base_url() -> str:
    return component_base_urls()[0]


def fetch_manifest_from_sources(sources: list[tuple[str, str]], timeout: float = 8.0,
                                what: str = "识别组件") -> dict:
    """各源都问一遍清单，挑版本最高的；同一版本的其它源按顺序留作下载备用。

    清单里带上 `_base`（首选源）和 `_bases`（全部可用源），install() 只从这些源取包——
    不能出现「清单来自 A、包来自不相干的 B」的混搭。`_checks` 记每个源的结果给界面看。
    """
    import json as _json
    import urllib.request

    from .netfetch import SourceFailure, looks_like_web_page, reason_of, source_name
    from .updater import _ver_tuple

    found = []
    checks = []
    for label, base in sources:
        try:
            req = urllib.request.Request(base + "/" + COMPONENT_MANIFEST,
                                         headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024)
                headers = getattr(response, "headers", None)
            ctype = ""
            try:
                ctype = (headers.get("Content-Type") if headers is not None else "") or ""
            except AttributeError:
                pass
            if looks_like_web_page(ctype, raw):
                raise SourceFailure("收到的是网页而不是组件清单，可能被公司代理拦截")
            data = _json.loads(raw.decode("utf-8"))
            if not (isinstance(data, dict) and data.get("files")):
                raise SourceFailure("组件清单无效")
            found.append((label, base, data))
            checks.append({"label": label, "name": source_name(label), "ok": True,
                           "version": str(data.get("version", "")), "reason": ""})
        except Exception as exc:      # noqa: BLE001 逐个源试，全部失败才抛
            reason = reason_of(exc)
            checks.append({"label": label, "name": source_name(label), "ok": False,
                           "version": "", "reason": reason})
            log.info("%s清单取不到（%s）：%s", what, base, reason)
    if not found:
        detail = "；".join(f"{c['name']}：{c['reason']}" for c in checks)
        err = OcrUnavailable(f"{what}清单不可达：{detail}")
        err.checks = checks  # 界面逐个源列出原因
        raise err
    best = max(found, key=lambda item: _ver_tuple(item[2].get("version", "")))
    version = best[2].get("version", "")
    data = dict(best[2])
    data["_base"] = best[1]
    data["_bases"] = [(label, base) for label, base, d in found if d.get("version", "") == version]
    data["_checks"] = checks
    return data


def fetch_component_manifest(timeout: float = 8.0) -> dict:
    """文字识别组件的清单（多源，挑版本最高）。"""
    return fetch_manifest_from_sources(component_sources(), timeout, "识别组件")


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def install(manifest: dict | None = None, *, progress=None, cancel=None,
            archive_path: Path | None = None, target_dir: Path | None = None,
            status=None, what: str = "识别组件") -> str:
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
        if archive_path is not None:
            # 安装包内置的离线组件：本地拷贝，不联网
            hasher = hashlib.sha256()
            done = 0
            with archive_path.open("rb") as src, open(bundle, "wb") as out:
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
        else:
            from .netfetch import AllSourcesFailed, download_with_fallback
            bases = manifest.get("_bases") or []
            if not bases:
                first = str(manifest.get("_base") or "")
                known = dict((b, label) for label, b in component_sources())
                bases = ([(known.get(first, "self-hosted"), first)] if first else []) + [
                    item for item in component_sources() if item[1] != first]
            candidates = [(label, base + "/" + archive_name) for label, base in bases]
            size = int(archive_meta.get("size", 0)) or None

            def _status(st):
                if progress:
                    progress(st.get("received", 0), st.get("total") or total)
                if status is not None:
                    status(st)

            try:
                download_with_fallback(
                    candidates, bundle, expect_size=size, expect_sha256=archive_hash,
                    timeout=60, cancel=cancel, on_status=_status, what=what,
                    on_attempts=(lambda at: status({"attempts": at})) if status else None)
            except AllSourcesFailed as exc:
                raise OcrUnavailable(f"{what}下载失败：" + "；".join(
                    f"{a.name}：{a.reason}" for a in exc.attempts)) from exc
            if progress:
                progress(total, total)

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
            json.dumps({"version": str(manifest.get("version") or ""),
                        "archive_hash": archive_hash},
                       ensure_ascii=False), encoding="utf-8")
        bundle.unlink(missing_ok=True)
        staging_payload = unpacked

        target = target_dir if target_dir is not None else component_dir()
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
        return str(manifest.get("version") or "")
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
            f"size={size_mb:.0f}MB path={active_component_dir()}")


if __name__ == "__main__":  # 手动排查：python -m pptx_finder.imgtext_ocr 图片
    sys.stdout.reconfigure(encoding="utf-8")
    print(self_test())
    for arg in sys.argv[1:]:
        rows = recognize_one(arg)
        print(f"{arg}: {len(rows)} 行")
        for r in rows[:8]:
            print(f"   {r['score']:.3f} {r['box']} {r['text']}")
