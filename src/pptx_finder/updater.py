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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
_CHECK_TIMEOUT = 8       # 拉清单超时（秒）
_DL_TIMEOUT = 30         # 单文件下载超时（秒）
_BUF = 1 << 16


@dataclass
class UpdateInfo:
    version: str
    notes: str
    changed: list          # [(relpath, sha256, size)] 需下载的文件
    deleted: list           # [relpath] 远端已移除、本地应删
    raw: dict = field(default_factory=dict)  # 远端完整清单（落地为新本地 manifest）

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


def build_manifest(
    dist_dir: Path,
    version: str,
    notes: str = "",
    *,
    commit: str = "",
    built_at: str = "",
) -> dict:
    """遍历 dist_dir 生成清单。relpath 用正斜杠；排除 manifest.json 自身。"""
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
def fetch_remote_manifest(
    base_url: str,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> dict:
    url = base_url.rstrip("/") + "/" + MANIFEST_NAME
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if response_callback is not None:
            response_callback(r)
        try:
            return json.loads(r.read().decode("utf-8"))
        finally:
            if response_callback is not None:
                response_callback(None)


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
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    total = info.total_bytes
    done = 0
    for rel, h, _size in info.changed:
        if cancel is not None and cancel():
            raise InterruptedError("下载已取消")
        h = _safe_hash(h, rel)  # 块名直接拼进 URL，落到这一刻才校验
        url = base_url.rstrip("/") + "/files/" + h
        dest = staging / safe_relpath(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:
            if response_callback is not None:
                response_callback(r)
            while True:
                chunk = r.read(_BUF)
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
                if cancel is not None and cancel():
                    raise InterruptedError("下载已取消")
        if response_callback is not None:
            response_callback(None)
        got = hasher.hexdigest()
        if got != h:
            raise ValueError(f"哈希校验失败 {rel}：期望 {h[:12]}… 实得 {got[:12]}…")
    if info.raw:
        (staging / MANIFEST_NAME).write_text(
            json.dumps(info.raw, ensure_ascii=False), encoding="utf-8")
    return staging


# ---------- 应用（Windows 原地替换） ----------
# helper 纯 ASCII（PS5.1 无 BOM 文件按 ANSI 读，纯 ASCII 不会乱码）。日志用英文。
_HELPER_PS1 = r'''param([int]$MainPid, [string]$Plan)
$script:LogPath = ($env:LOCALAPPDATA + '\pptx-finder\update.log')
function Log($m){ try { Add-Content -LiteralPath $script:LogPath -Value ((Get-Date -Format o) + '  ' + $m) } catch {} }
try {
  $p = Get-Content -Raw -LiteralPath $Plan | ConvertFrom-Json
  if ($p.log_path) { $script:LogPath = [string]$p.log_path }
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
    } else {
      $swapped = $true
      foreach ($rel in @($p.deletes)) {
        if (-not $rel) { continue }
        $dst = Join-Path $dest ($rel -replace '/','\')
        if (Test-Path -LiteralPath $dst) { try { Remove-Item -LiteralPath $dst -Force } catch { Log "del $rel $_" } }
      }
      Log "swap done -> v$($p.version)"
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
} catch { Log "FATAL $_" }
'''


def write_helper(
    staging: Path,
    dest: Path,
    info: UpdateInfo,
    relaunch: str,
    *,
    log_path: str | Path | None = None,
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
    h = write_helper(staging, dest, info, relaunch)
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


def check_for_update(
    base_url: str,
    *,
    timeout: float = _CHECK_TIMEOUT,
    response_callback=None,
) -> UpdateInfo | None:
    """供 UI 后台线程调用：非 frozen / 无本地清单 / 拉取失败 → None（不打扰）。"""
    if not is_frozen():
        return None
    local = local_manifest()
    if not local:
        return None
    remote = fetch_remote_manifest(
        base_url,
        timeout=timeout,
        response_callback=response_callback,
    )
    return compare(local, remote)


def run_update_check(argv: list[str]) -> int:
    """`PPT Doctor.exe --update-check <base_url> <report.json>`：headless 检查 + 下载到 staging，
    写报告。用于打包态 E2E 验证 frozen 的 urllib/清单/增量下载/sha256 链路（不弹 GUI、不应用）。
    """
    i = argv.index("--update-check")
    base_url = argv[i + 1] if len(argv) > i + 1 else ""
    report = argv[i + 2] if len(argv) > i + 2 else "update_check_report.json"
    data: dict = {"frozen": is_frozen(), "base_url": base_url}
    try:
        info = check_for_update(base_url)
        if info is None:
            data["update"] = False
        else:
            staging = Path(tempfile.gettempdir()) / "pptutor_update_check"
            shutil.rmtree(staging, ignore_errors=True)
            download_delta(base_url, info, staging)  # 逐块校验 sha256，能跑通即全部通过
            data.update(update=True, version=info.version, verified=True,
                        changed=[r[0] for r in info.changed], deleted=info.deleted,
                        bytes=info.total_bytes, staging=str(staging))
    except Exception as e:  # noqa: BLE001
        import traceback
        data.update(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
    Path(report).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if "error" in data else 0
