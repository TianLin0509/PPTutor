"""增量自动更新：内容寻址清单 diff + 只下变化块 + sha256 校验 + helper 原地替换。

机制
- 构建时 tools/gen_manifest.py 遍历 dist 生成 manifest.json（每文件 sha256 + 版本 + 说明），随包发布。
- 运行时拉远端 manifest.json，与本地比对算出「哈希变了的文件」→ 只下载这些（服务端按
  files/<hash> 内容寻址存储，天然去重）→ 逐个校验 sha256 → 落地 staging。
- 应用：写 plan.json + 生成 PowerShell helper，主程序退出后由 helper 等进程关闭、覆盖文件、
  删废弃、重启新版（Windows 不能覆盖运行中的 exe/dll，必须独立进程替换）。

数据安全铁律：只动程序安装目录，绝不碰 %LOCALAPPDATA%\\pptx-finder（索引 + 版本库）。
零新依赖：urllib + hashlib + subprocess 全为 Python 标准库，exe 体积不变。

本模块不依赖 Qt，纯函数 + 显式参数（便于真实单测）；Qt 线程封装在 ui/update_ui.py。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
_CHECK_TIMEOUT = 8       # 拉清单超时（秒）
_DL_TIMEOUT = 30         # 单文件下载超时（秒）
_BUF = 1 << 16

# GitHub Release 作为第二个更新源。`releases/latest/download/<资产名>` 是稳定入口，
# 永远指向最新一版，不用查 API、也不用把 tag 写死（实测可用）。
#
# 为什么要有第二个源：自建源 me.lt-stockpartner.tech 上的清单长期停在 v1.2.7，而
# `compare()` 只在远端版本更高时才返回更新——于是 >=1.2.7 的用户**永远收不到更新**，
# <1.2.7 的用户被更新到 1.2.7 就卡住。单源一旦不更新，整个自动更新链路是静默死的。
# 而「图片转文字」的识别组件早就是双源（主源 404，实际一直靠 GitHub 回落在服务
# 用户），这条通道已经被真实用户验证过可达。
GITHUB_RELEASE_LATEST = "https://github.com/TianLin0509/PPTutor/releases/latest/download"


@dataclass(frozen=True)
class UpdateSource:
    """一个更新源：从哪拿清单、从哪拿变化块、块缺失时从哪拿整包。"""

    label: str
    manifest_url: str
    block_url: str                # 含 "{hash}"
    package_url: str = ""         # 含 "{version}"；空 = 该源没有整包兜底

    @property
    def name(self) -> str:
        """界面上显示的源名：「GitHub」「备用服务器」。"""
        from .netfetch import source_name
        return source_name(self.label)

    def block(self, sha256: str) -> str:
        return self.block_url.format(hash=sha256)

    def package(self, version: str) -> str:
        return self.package_url.format(version=version) if self.package_url else ""


def _is_default_base(base: str) -> bool:
    from .config import _DEFAULT_UPDATE_URL
    return base.rstrip("/") == _DEFAULT_UPDATE_URL.rstrip("/")


def update_sources(base_url: str, *, edition='full') -> list[UpdateSource]:
    """更新源列表，**按下载时的尝试顺序**排好。

    v2.0 起（对照 AI Hub 的公司实测）：默认 GitHub 在前、自建服务器作备用。GitHub 在公网
    和公司网络都直连可达；自建源以后每次发版同步，挂掉或被代理拦截时自动换下一个。
    显式给了别的地址（环境变量 PPTX_FINDER_UPDATE_URL，测试/灰度用）时，它排在最前面；
    多个地址用分号隔开；PPTX_FINDER_UPDATE_NO_GITHUB=1 时不加 GitHub（本机端到端测试用）。
    """
    from .editions import manifest_name, package_suffix
    remote_name = manifest_name(edition)
    suffix = package_suffix(edition)
    bases = [b.strip().rstrip("/") for b in str(base_url or "").split(";") if b.strip()]
    hosted = []
    for i, base in enumerate(bases):
        hosted.append(UpdateSource(
            label="self-hosted" if i == 0 else f"self-hosted-{i + 1}",
            manifest_url=f"{base}/{remote_name}",
            block_url=base + "/files/{hash}",
            package_url=base + "/PPT-Doctor-v{version}" + suffix + ".zip",
        ))
    github = UpdateSource(
        label="github-release",
        manifest_url=f"{GITHUB_RELEASE_LATEST}/{remote_name}",
        block_url=GITHUB_RELEASE_LATEST + "/{hash}",
        # GitHub 的资产是平铺的，没有 files/ 子目录；块就以 sha256 为资产名上传。
        # 落后好几版的用户需要的块可能没随最新一版发布，那时回落到整包。
        package_url=GITHUB_RELEASE_LATEST + "/PPT-Doctor-v{version}" + suffix + '.zip',
    )
    if os.environ.get("PPTX_FINDER_UPDATE_NO_GITHUB", "").strip() in ("1", "true", "yes"):
        return hosted
    if bases and all(_is_default_base(b) for b in bases):
        return [github] + hosted
    return hosted + [github]


def _source_for_base(base_url: str) -> UpdateSource:
    """只给了一个地址的老调用方式（download_delta(base, info) 且 info 没带源）。"""
    hosted = [s for s in update_sources(base_url) if s.label.startswith("self-hosted")]
    return hosted[0] if hosted else update_sources(base_url)[0]


class BlockUnavailableError(RuntimeError):
    """某个变化块在该源上取不到（多半是落后太多版），应回落到整包。"""


@dataclass
class UpdateInfo:
    version: str
    notes: str
    changed: list          # [(relpath, sha256, size)] 需下载的文件
    deleted: list           # [relpath] 远端已移除、本地应删
    raw: dict = field(default_factory=dict)  # 远端完整清单（落地为新本地 manifest）
    source: UpdateSource | None = None       # 这份更新是从哪个源比出来的
    sources: list = field(default_factory=list)   # 有这一版的全部源（按尝试顺序），下载时逐个回落
    checks: list = field(default_factory=list)    # 查版本时每个源的结果（SourceCheck）
    attempts: list = field(default_factory=list)  # 下载时每个源的结果（netfetch.Attempt.as_dict）
    used_source: UpdateSource | None = None       # 实际下载成功的源
    kind: str = "incremental"                     # incremental / package（块缺失时回落整包）

    @property
    def total_bytes(self) -> int:
        return sum(sz for _, _, sz in self.changed)


# ---------- 清单 ----------
def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_entry(files: dict) -> str:
    """安装目录根下唯一的 .exe 就是入口。"""
    tops = [rel for rel in files if "/" not in rel and rel.lower().endswith(".exe")]
    return tops[0] if len(tops) == 1 else ""


def build_manifest(
    dist_dir: Path,
    version: str,
    notes: str = "",
    *,
    commit: str = "",
    built_at: str = "",
    entry: str = "",
) -> dict:
    """遍历 dist_dir 生成清单。relpath 用正斜杠；排除 manifest.json 自身。

    额外记 `entry`（入口 exe 文件名）：入口改名的那一版，helper 必须启动**新**名
    字，否则它会去启动一个刚被自己删掉的文件——用户看到的是「更新完程序没了」。
    """
    dist_dir = Path(dist_dir)
    files: dict[str, dict] = {}
    for p in sorted(dist_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(dist_dir).as_posix()
        if rel == MANIFEST_NAME:
            continue
        files[rel] = {"hash": _sha256_file(p), "size": p.stat().st_size}
    manifest = {"version": str(version), "notes": notes, "files": files}
    from .editions import MARKER, read_edition
    if (dist_dir / MARKER).is_file():
        manifest['edition'] = read_edition(dist_dir)
    entry = str(entry or "").strip() or _detect_entry(files)
    if entry:
        manifest["entry"] = entry
    if commit or built_at:
        manifest["build"] = {
            "commit": str(commit or ""),
            "built_at": str(built_at or ""),
        }
    return manifest


def _ver_tuple(v: str) -> tuple:
    out = []
    for part in str(v).split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


class UnsafeManifestError(ValueError):
    """清单里出现了会越出安装目录的条目——整批放弃，不做部分应用。"""


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def safe_relpath(rel: str) -> str:
    r"""把清单里的一条 relpath 收敛成「一定落在目标目录内」的相对路径。

    本模块开头写着数据安全铁律「只动程序安装目录」，但此前没有任何代码在执行它：
    relpath 来自远端 manifest，`staging / rel` 与 helper 的 `Join-Path $dest $rel`
    都会老老实实解析 `..`。一条 `../../../AppData/Roaming/.../Startup/x.bat`
    就能把文件写进开机启动项；而且首次投毒后本地 manifest 会被远端整份替换，
    下一轮的 deletes（取自本地清单）同样变成可控 → 任意删除。

    因此：绝对路径、盘符/数据流冒号、任何 `..` 段一律拒绝，且**整批拒绝**
    （fail closed，宁可这次不更新，也不做半套替换）。
    """
    raw = str(rel or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or ":" in raw:
        raise UnsafeManifestError(f"更新清单含非法路径：{rel!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UnsafeManifestError(f"更新清单含非法路径：{rel!r}")
    return "/".join(parts)


def _safe_hash(value: str, rel: str) -> str:
    """内容寻址块名会被拼进下载 URL；只接受 sha256 十六进制。"""
    h = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(h):
        raise UnsafeManifestError(f"更新清单含非法哈希（{rel!r}）：{value!r}")
    return h


def sanitize_entries(info: UpdateInfo) -> UpdateInfo:
    """在下载 / 生成 helper 计划之前再把关一次（直接构造 UpdateInfo 的调用方也走这道门）。

    只校验路径：越出目标目录是真正的越权面。哈希只在拼下载 URL 那一刻才需要校验，
    放在这里会让「取消先于任何网络请求」之类的既有契约提前失效。
    """
    for rel, _hash, _size in info.changed:
        safe_relpath(rel)
    for rel in info.deleted:
        safe_relpath(rel)
    return info


def relaunch_name(info: UpdateInfo, fallback: str = "") -> str:
    """更新装完之后该启动哪个 exe——以**新版清单**为准，不是当前进程的名字。

    v1.5.2 把入口从 `PPT Doctor.exe` 改成 `PPT-Doctor.exe`（去掉空格）。这一版的
    更新里，旧名字会出现在 deletes 里；如果还按 `Path(sys.executable).name` 去
    relaunch，helper 就会在删完之后启动一个不存在的文件，用户看到的是「更新完就
    再也打不开了」。所以入口名必须跟着清单走。

    清单来自网络，所以只认「纯文件名 + 确实在本次要落地的文件集合里」；不符合就
    退回 fallback（当前 exe 名），最坏情况等于旧行为，不会更糟。
    """
    raw = info.raw or {}
    entry = str(raw.get("entry") or "").strip()
    files = raw.get("files") or {}
    if entry and entry == os.path.basename(entry) and entry not in ("", ".", "..") \
            and entry in files:
        return entry
    return fallback


def compare(local: dict, remote: dict) -> UpdateInfo | None:
    """对比本地/远端清单。仅当远端版本号更高才算更新，否则 None。

    返回需下载的变化文件（哈希不同或新增）+ 待删文件（本地有远端无）。
    清单里任一条目越界（绝对路径 / `..` / 非法哈希）即整批放弃：安装目录一个字节不动。
    """
    lver = local.get("version", "0")
    rver = remote.get("version", "0")
    if _ver_tuple(rver) <= _ver_tuple(lver):
        return None
    lf = local.get("files", {})
    rf = remote.get("files", {})
    changed = [
        (safe_relpath(rel), meta.get("hash"), int(meta.get("size", 0)))
        for rel, meta in rf.items()
        if lf.get(rel, {}).get("hash") != meta.get("hash")
    ]
    # deleted 取自本地清单，但本地清单正是上一轮远端清单落地的产物——同样要过门。
    deleted = [safe_relpath(rel) for rel in lf if rel not in rf]
    return UpdateInfo(version=str(rver), notes=remote.get("notes", ""),
                      changed=changed, deleted=deleted, raw=remote)


# ---------- 网络 ----------
def fetch_manifest_from(
    source: UpdateSource,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> dict:
    return _fetch_manifest_url(source.manifest_url, timeout, response_callback)


def fetch_remote_manifest(
    base_url: str,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> dict:
    return _fetch_manifest_url(
        base_url.rstrip("/") + "/" + MANIFEST_NAME, timeout, response_callback)


def _fetch_manifest_url(
    url: str,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> dict:
    from .netfetch import SourceFailure, fmt_bytes, looks_like_web_page
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if response_callback is not None:
            response_callback(r)
        try:
            raw = r.read(8 * 1024 * 1024)
        finally:
            if response_callback is not None:
                response_callback(None)
        headers = getattr(r, "headers", None)
        try:
            ctype = (headers.get("Content-Type") if headers is not None else "") or ""
        except AttributeError:
            ctype = ""
    if looks_like_web_page(ctype, raw):
        raise SourceFailure(f"收到的是网页（{fmt_bytes(len(raw))}）而不是版本清单，可能被公司代理拦截")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SourceFailure(f"版本清单无效：{e}"[:120]) from e
    if not isinstance(data, dict):
        raise SourceFailure("版本清单无效：不是 JSON 对象")
    return data


@dataclass
class SourceCheck:
    """查版本时一个源的结果，界面逐条显示。"""

    label: str
    name: str
    ok: bool
    version: str = ""
    reason: str = ""
    ms: int = 0

    def as_dict(self) -> dict:
        return {"label": self.label, "name": self.name, "ok": self.ok,
                "version": self.version, "reason": self.reason, "ms": self.ms}


@dataclass
class CheckReport:
    """一次检查的完整结果：最佳更新（可能没有）+ 各源状态。"""

    best: "UpdateInfo | None"
    checks: list
    current: str = ""
    error: str = ""          # 全部源都连不上 / 未打包等，给界面的人话

    @property
    def all_failed(self) -> bool:
        return bool(self.checks) and not any(c.ok for c in self.checks)


def download_delta(base_url: str, info: UpdateInfo, staging: Path,
                   progress=None, timeout: float = _DL_TIMEOUT, cancel=None,
                   response_callback=None) -> Path:
    """下载 info.changed 的每个 files/<hash> 到 staging/<relpath> 并校验 sha256；
    最后把远端清单写成 staging/manifest.json（随更新落地为新本地清单）。

    progress(done_bytes, total_bytes) 可选回调（UI 进度条用）。校验失败抛 ValueError。
    cancel() 可选：返回真则尽快中止（每块检查一次），抛 InterruptedError——供关窗时打断下载，
    避免下载线程「运行中被析构」崩溃。
    """
    sanitize_entries(info)  # 越界条目在落盘之前就整批拦下
    source = info.source or _source_for_base(base_url)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    total = info.total_bytes
    done = 0
    from .netfetch import SourceFailure, check_response_is_not_page, fmt_bytes, \
        looks_like_web_page
    for rel, h, _size in info.changed:
        if cancel is not None and cancel():
            raise InterruptedError("下载已取消")
        h = _safe_hash(h, rel)
        existing = staging / safe_relpath(rel)
        if existing.is_file() and _sha256_file(existing) == h:
            # 上一个源已经下好并校验过的块，换源时不必重下
            done += int(_size or 0)
            if progress:
                progress(done, total)
            continue  # 块名直接拼进 URL，落到这一刻才校验
        url = source.block(h)
        dest = staging / safe_relpath(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        try:
            opened = urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                # 该块没随最新一版发布（用户落后好几版）。不是错误，换整包。
                raise BlockUnavailableError(f"{rel} 的块在 {source.label} 上取不到") from e
            raise
        got_bytes = 0
        head = b""
        with opened as r, open(dest, "wb") as f:
            if response_callback is not None:
                response_callback(r)
            # 公司代理会把文件换成几 KB 的拦截网页：拿到响应头就先认出来
            check_response_is_not_page(r, int(_size or 0) or None)
            while True:
                chunk = r.read(_BUF)
                if not chunk:
                    break
                if not head:
                    head = chunk[:256]
                f.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                got_bytes += len(chunk)
                if progress:
                    progress(done, total)
                if cancel is not None and cancel():
                    raise InterruptedError("下载已取消")
        if response_callback is not None:
            response_callback(None)
        got = hasher.hexdigest()
        if got != h:
            if got_bytes < 256 * 1024 and looks_like_web_page("", head):
                raise SourceFailure(f"收到 {fmt_bytes(got_bytes)} 的网页而不是更新文件，可能被公司代理拦截")
            raise ValueError(f"哈希校验失败 {rel}：期望 {h[:12]}… 实得 {got[:12]}…")
    if info.raw:
        (staging / MANIFEST_NAME).write_text(
            json.dumps(info.raw, ensure_ascii=False), encoding="utf-8")
    return staging


def download_package(info: UpdateInfo, staging: Path,
                     progress=None, timeout: float = _DL_TIMEOUT, cancel=None) -> Path:
    """整包兜底：下发布 zip，只把**本次要换的那些文件**解出来。

    用在「用户落后好几版、需要的块没随最新一版发布」的情况。只解 info.changed 里
    的条目，helper 的 updates 列表才和 staging 里的内容对得上（多解无用文件不会
    出错，但会白占磁盘和时间）。
    """
    source = info.source
    if source is None or not source.package_url:
        raise BlockUnavailableError("该源没有整包可回落")
    sanitize_entries(info)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    wanted = {safe_relpath(rel): _safe_hash(h, rel) for rel, h, _ in info.changed}

    url = source.package(info.version)
    tmp = staging / "_package.zip"
    done = 0
    from .netfetch import check_response_is_not_page
    with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
        check_response_is_not_page(r, None, "安装包")
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            if cancel is not None and cancel():
                raise InterruptedError("下载已取消")
            chunk = r.read(_BUF)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total or done)
    try:
        with zipfile.ZipFile(tmp) as z:
            names = {n.replace("\\", "/"): n for n in z.namelist()}
            # 包内是 PPT-Doctor/<relpath>；剥掉这一层顶层目录
            roots = {n.split("/", 1)[0] for n in names if "/" in n}
            prefix = (roots.pop() + "/") if len(roots) == 1 else ""
            for rel, want_hash in wanted.items():
                member = names.get(prefix + rel)
                if member is None:
                    raise ValueError(f"整包里缺少 {rel}")
                dest = staging / rel          # rel 已过 safe_relpath，无法越界
                dest.parent.mkdir(parents=True, exist_ok=True)
                data = z.read(member)
                got = hashlib.sha256(data).hexdigest()
                if got != want_hash:
                    raise ValueError(
                        f"哈希校验失败 {rel}：期望 {want_hash[:12]}… 实得 {got[:12]}…")
                dest.write_bytes(data)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if info.raw:
        (staging / MANIFEST_NAME).write_text(
            json.dumps(info.raw, ensure_ascii=False), encoding="utf-8")
    return staging


def download_update(base_url: str, info: UpdateInfo, staging: Path,
                    progress=None, timeout: float = _DL_TIMEOUT, cancel=None,
                    response_callback=None, on_status=None, on_attempts=None) -> Path:
    """按顺序逐个源下载，一个源不行就写明原因换下一个。UI 只需要调这一个。

    每个源内部：先试增量块，块取不到（用户落后好几版）就下该源的整包。实测相邻两版
    之间只有 2 个文件真的变（exe 5.8 MB + base_library.zip 1.3 MB），增量 7.0 MB /
    全量 91.3 MB = 7.6%——所以增量这条路值得留，整包只是兜底。

    on_status(dict)：进度快照（源名、已收/总字节、平滑速度、剩余秒数）。
    on_attempts(list[dict])：每个源开始/结束时，全部源的结果（给弹窗的源列表）。
    全部源失败抛 netfetch.AllSourcesFailed（消息里逐个列出原因）。
    """
    from .netfetch import AllSourcesFailed, Attempt, ProgressMeter, reason_of

    sources = list(info.sources or [])
    if not sources:
        sources = [info.source or _source_for_base(base_url)]
    staging = Path(staging)
    attempts: list[Attempt] = []

    def _emit():
        info.attempts = [a.as_dict() for a in attempts]
        if on_attempts is not None:
            on_attempts(list(info.attempts))

    for source in sources:
        if cancel is not None and cancel():
            raise InterruptedError("下载已取消")
        attempt = Attempt(label=source.label, name=source.name, url=source.manifest_url)
        attempts.append(attempt)
        _emit()
        meter = ProgressMeter(info.total_bytes, source.name)

        def _progress(done, total, meter=meter):
            if progress:
                progress(done, total)
            snap = meter.update(done, total)
            if snap is not None and on_status is not None:
                on_status(snap)

        sub = UpdateInfo(version=info.version, notes=info.notes, changed=info.changed,
                         deleted=info.deleted, raw=info.raw, source=source)
        try:
            try:
                download_delta(base_url, sub, staging, progress=_progress, timeout=timeout,
                               cancel=cancel, response_callback=response_callback)
                info.kind = "incremental"
            except BlockUnavailableError:
                # 半截的 staging 会让 helper 的 preflight 直接放弃本次更新，先清干净
                shutil.rmtree(staging, ignore_errors=True)
                if not source.package_url:
                    raise
                meter.total = 0
                info.kind = "package"
                download_package(sub, staging, progress=_progress, timeout=timeout,
                                 cancel=cancel)
        except InterruptedError:
            attempt.finish("cancelled", "已取消")
            _emit()
            raise
        except Exception as exc:  # noqa: BLE001 这个源不行，记下原因换下一个
            attempt.finish("failed", reason_of(exc))
            _emit()
            continue
        attempt.finish("ok", "", meter.received or info.total_bytes)
        info.used_source = source
        _emit()
        return staging
    raise AllSourcesFailed("下载更新", attempts)


# ---------- 应用（Windows 原地替换） ----------
# helper 纯 ASCII（PS5.1 无 BOM 文件按 ANSI 读，纯 ASCII 不会乱码）。日志用英文。
_HELPER_PS1 = r'''param([int]$MainPid, [string]$Plan)
$script:LogPath = ($env:LOCALAPPDATA + '\pptx-finder\update.log')
function Log($m){ try { Add-Content -LiteralPath $script:LogPath -Value ((Get-Date -Format o) + '  ' + $m) } catch {} }
function Result($ok, $rolled, $why){
  if (-not $script:ResultPath) { return }
  try {
    $o = [ordered]@{ version = [string]$script:Version; ok = [bool]$ok; rolled_back = [bool]$rolled; reason = [string]$why }
    Set-Content -LiteralPath $script:ResultPath -Value ($o | ConvertTo-Json -Compress) -Encoding UTF8
  } catch { Log "result write: $_" }
}
try {
  $p = Get-Content -Raw -LiteralPath $Plan | ConvertFrom-Json
  if ($p.log_path) { $script:LogPath = [string]$p.log_path }
  $script:ResultPath = [string]$p.result_path
  $script:Version = [string]$p.version
  Log "helper start pid=$MainPid"
  if ($MainPid -gt 0) { try { Wait-Process -Id $MainPid -Timeout 60 -ErrorAction Stop } catch { Log "wait: $_" } }
  Start-Sleep -Milliseconds 500
  $staging = $p.staging; $dest = $p.dest
  $updates = @($p.updates) | Where-Object { $_ }
  # Preflight. A half-applied swap leaves exe and DLLs from two different builds
  # in the install dir, which typically cannot start at all -- and the user has
  # no way back. Verify the whole staging set first; if anything is missing,
  # touch nothing and relaunch the still-working old build.
  $missing = @()
  foreach ($rel in $updates) {
    if (-not (Test-Path -LiteralPath (Join-Path $staging ($rel -replace '/','\')))) { $missing += $rel }
  }
  $swapped = $false
  if ($missing.Count -gt 0) {
    Log ("ABORT preflight: " + $missing.Count + " staged file(s) missing, install untouched")
    Result $false $false ("missing " + $missing.Count + " staged file(s)")
  } else {
    $backup = Join-Path ([System.IO.Path]::GetTempPath()) ("pptutor_rollback_" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $restore = New-Object System.Collections.ArrayList
    $failed = $null
    foreach ($rel in $updates) {
      $r = ($rel -replace '/','\')
      $src = Join-Path $staging $r
      $dst = Join-Path $dest $r
      $dir = Split-Path -Parent $dst
      if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
      if (Test-Path -LiteralPath $dst) {
        $bak = Join-Path $backup $r
        $bdir = Split-Path -Parent $bak
        if ($bdir -and -not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        try { Copy-Item -LiteralPath $dst -Destination $bak -Force; [void]$restore.Add($r) }
        catch { $failed = $rel; Log "FAILED backup $rel"; break }
      }
      $ok = $false
      for ($i=0; $i -lt 40; $i++) {
        try { Copy-Item -LiteralPath $src -Destination $dst -Force; $ok = $true; break }
        catch { Start-Sleep -Milliseconds 300 }
      }
      if (-not $ok) { $failed = $rel; Log "FAILED copy $rel"; break }
    }
    if ($failed) {
      foreach ($r in $restore) {
        try { Copy-Item -LiteralPath (Join-Path $backup $r) -Destination (Join-Path $dest $r) -Force }
        catch { Log "ROLLBACK FAILED $r $_" }
      }
      Log ("ROLLED BACK " + $restore.Count + " file(s) after failure on " + $failed + "; backup kept at " + $backup)
      Result $false $true ("replace failed: " + $failed)
    } else {
      $swapped = $true
      foreach ($rel in @($p.deletes)) {
        if (-not $rel) { continue }
        $dst = Join-Path $dest ($rel -replace '/','\')
        if (Test-Path -LiteralPath $dst) { try { Remove-Item -LiteralPath $dst -Force } catch { Log "del $rel $_" } }
      }
      Log "swap done -> v$($p.version)"
      Result $true $false ""
      try { Remove-Item -LiteralPath $backup -Recurse -Force } catch {}
    }
  }
  if ($p.relaunch) {
    try {
      # Explicit cwd avoids inheriting the helper/control directory. Besides
      # helping relative resources, this makes ShellExecute of .bat/.cmd tests
      # and the packaged exe much more deterministic under machine load.
      Start-Process -FilePath (Join-Path $dest $p.relaunch) -WorkingDirectory $dest
    } catch { Log "relaunch $_" }
  }
  # Keep staging when the swap did not happen: the next attempt can reuse it
  # instead of downloading the whole delta again.
  if ($swapped) { try { Remove-Item -LiteralPath $staging -Recurse -Force } catch {} }
  Log "helper done"
} catch { Log "FATAL $_"; Result $false $false ("helper error: " + $_) }
'''


def write_helper(
    staging: Path,
    dest: Path,
    info: UpdateInfo,
    relaunch: str,
    *,
    log_path: str | Path | None = None,
    result_path: str | Path | None = None,
) -> dict:
    """把 plan.json + apply.ps1 写到一个独立控制目录（不在 staging 内，免被 helper 自删时连累）。

    返回 {plan, plan_path, ps1, ctrl}，供 launch_helper / 测试直接驱动。
    """
    sanitize_entries(info)  # helper 拿到 plan.json 就直接 Join-Path $dest，这是最后一道门
    ctrl = Path(tempfile.mkdtemp(prefix="pptutor_apply_"))
    plan = {
        "staging": str(staging),
        "dest": str(dest),
        "updates": [safe_relpath(rel) for rel, _, _ in info.changed] + [MANIFEST_NAME],
        "deletes": [safe_relpath(rel) for rel in info.deleted],
        "relaunch": relaunch,
        "version": info.version,
        "log_path": str(log_path) if log_path is not None else "",
        "result_path": str(result_path) if result_path is not None else "",
    }
    plan_path = ctrl / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    ps1 = ctrl / "apply.ps1"
    ps1.write_text(_HELPER_PS1, encoding="ascii")
    return {"plan": plan, "plan_path": str(plan_path), "ps1": str(ps1), "ctrl": str(ctrl)}


def launch_helper(ps1: str, plan_path: str, main_pid: int | None = None) -> None:
    """分离启动 helper（调用方随后应立即退出，让 helper 接管文件替换）。"""
    pid = os.getpid() if main_pid is None else main_pid
    flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "DETACHED_PROCESS", 0))
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-File", ps1,
         "-MainPid", str(pid), "-Plan", plan_path],
        creationflags=flags, close_fds=True,
    )


def apply_update(staging: Path, dest: Path, info: UpdateInfo, relaunch: str,
                 main_pid: int | None = None, launch: bool = True) -> dict:
    """生成 helper 并（默认）分离启动。返回 write_helper 的结果（含 plan/路径）。"""
    try:
        result_path = update_result_path()
        result_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        result_path = None
    h = write_helper(staging, dest, info, relaunch, result_path=result_path)
    if launch:
        launch_helper(h["ps1"], h["plan_path"], main_pid)
    return h


# ---------- 运行态封装（frozen 才生效，dev 不打扰） ----------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """安装目录 = 可执行文件所在目录（frozen onedir 的 dist 根）。"""
    return Path(sys.executable).resolve().parent


def local_manifest() -> dict | None:
    p = install_dir() / MANIFEST_NAME
    if not p.exists():
        try:
            from . import __version__
            manifest = build_manifest(install_dir(), __version__, f"PPT Doctor v{__version__}")
            p.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
            return manifest
        except Exception:  # noqa: BLE001
            return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def check_sources(
    base_url: str,
    *,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> CheckReport:
    """各源同时查清单，记下每个源的结果，挑**版本最高**的那份。

    **挑版本最高的源，而不是第一个能连上的源。** 自建源的清单曾长期停在 v1.2.7；
    按「第一个连得上就用」，那台服务器一天不更新，所有用户就一天收不到更新，
    而且完全没有报错。现在只要任一源有更新的版本就能拿到；有同一版本的其它源
    按顺序排在后面，下载时逐个回落。
    """
    from concurrent.futures import ThreadPoolExecutor
    import time as _time

    from .netfetch import reason_of

    if not is_frozen():
        return CheckReport(best=None, checks=[], error="开发环境不检查更新")
    local = local_manifest()
    if not local:
        return CheckReport(best=None, checks=[], error="读不到本机安装清单，无法比对版本")
    from .editions import current_edition
    # The installer may switch editions while retaining an older local manifest.
    # Its marker is authoritative; the manifest only supplies hashes/version.
    edition = current_edition()
    sources = update_sources(base_url, edition=edition)

    def _one(source):
        started = _time.monotonic()
        try:
            remote = fetch_manifest_from(
                source, timeout=timeout, response_callback=response_callback)
            if remote.get('edition', 'full') != edition:
                raise UnsafeManifestError('更新包版本类型与当前安装不符')
            info = compare(local, remote)
            check = SourceCheck(source.label, source.name, True,
                                str(remote.get("version", "")),
                                ms=int((_time.monotonic() - started) * 1000))
            return source, info, check
        except Exception as exc:  # noqa: BLE001 单个源不可达不该拖垮整次检查
            reason = "更新包版本类型与当前安装不符" if isinstance(exc, UnsafeManifestError) \
                else reason_of(exc)
            return source, None, SourceCheck(source.label, source.name, False, reason=reason,
                                             ms=int((_time.monotonic() - started) * 1000))

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        results = list(pool.map(_one, sources))
    checks = [c for _s, _i, c in results]
    best: UpdateInfo | None = None
    for source, info, _check in results:
        if info is None:
            continue
        info.source = source
        if best is None or _ver_tuple(info.version) > _ver_tuple(best.version):
            best = info
    if best is not None:
        best.sources = [src for src, info, _c in results
                        if info is not None and info.version == best.version]
        best.source = best.sources[0]
        best.checks = checks
    report = CheckReport(best=best, checks=checks, current=str(local.get("version", "")))
    if report.all_failed:
        report.error = "所有更新源都连不上：" + "；".join(f"{c.name}：{c.reason}" for c in checks)
    return report


def check_for_update(
    base_url: str,
    *,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> UpdateInfo | None:
    """兼容入口：只要最佳更新。非 frozen / 无本地清单 / 全部源都拿不到 → None。"""
    return check_sources(base_url, timeout=timeout, response_callback=response_callback).best


# ---------- 上一次更新的结果（helper 写，主程序下次启动读一次） ----------
def update_result_path() -> Path:
    from .config import data_dir
    return data_dir() / "update_result.json"


def consume_update_result() -> dict | None:
    """读出并删除上一次更新的结果：{version, ok, rolled_back, reason}。只提示一次。"""
    p = update_result_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        data = None
    try:
        p.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) and data.get("version") else None


def describe_update_result(result: dict | None, current_version: str = "") -> str:
    """把 helper 的结果翻成一句提示。"""
    if not result:
        return ""
    version = str(result.get("version") or "")
    if result.get("ok"):
        if current_version and _ver_tuple(current_version) < _ver_tuple(version):
            return f"⚠ 更新到 v{version} 后启动的仍是旧版本，请重新打开或手动安装"
        return f"✅ 已更新到 v{version}"
    reason = str(result.get("reason") or "原因未知")
    tail = "，已恢复原来的版本" if result.get("rolled_back") else "，原来的版本没有改动"
    return f"⚠ 更新到 v{version} 没有完成{tail}：{reason}"


def run_update_check(argv: list[str]) -> int:
    """`PPT-Doctor.exe --update-check <base_url> <report.json>`：headless 检查 + 下载到 staging，
    写报告。用于打包态 E2E 验证 frozen 的 urllib/清单/增量下载/sha256 链路（不弹 GUI、不应用）。
    """
    i = argv.index("--update-check")
    base_url = argv[i + 1] if len(argv) > i + 1 else ""
    report = argv[i + 2] if len(argv) > i + 2 else "update_check_report.json"
    data: dict = {"frozen": is_frozen(), "base_url": base_url}
    try:
        checked = check_sources(base_url)
        data["checks"] = [c.as_dict() for c in checked.checks]
        info = checked.best
        if info is None:
            data["update"] = False
            if checked.error:
                data["check_error"] = checked.error
        else:
            staging = Path(tempfile.gettempdir()) / "pptutor_update_check"
            shutil.rmtree(staging, ignore_errors=True)
            try:
                download_update(base_url, info, staging)  # 逐源回落，逐块校验 sha256
            finally:
                data["attempts"] = info.attempts
            data.update(update=True, version=info.version, verified=True,
                        source=info.used_source.name if info.used_source else "",
                        kind=info.kind,
                        changed=[r[0] for r in info.changed], deleted=info.deleted,
                        bytes=info.total_bytes, staging=str(staging))
    except Exception as e:  # noqa: BLE001
        import traceback
        data.update(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
    Path(report).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if "error" in data else 0
