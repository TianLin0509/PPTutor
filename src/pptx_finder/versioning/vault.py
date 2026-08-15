"""版本库：快照 / 列版本 / 重组恢复 / 导出。

按页（part）去重存储：pptx 是 zip，逐 part 内容寻址存进全局对象池，跨文档重复也只存一份；
每版只记一份 manifest（name→hash 列表）。改几个字只新增变化的 part，大幅省空间。
重组后做保真自检；万一失败，该版回退为完整拷贝（mode=full），保证一定能恢复。
"""
from __future__ import annotations

import datetime
import threading
from collections import OrderedDict
from contextlib import contextmanager
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

import xxhash

from ..config import data_dir, ext_path
from ..parser import parse_pptx
from ..text_tokenize import tokenize
from . import store

log = logging.getLogger(__name__)

_OBJECT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_GLOBAL_OBJECTS_DIRNAME = "_objects"
# 「这个对象路径已按内容哈希验过」的缓存：省掉重复读盘重算哈希。
# 必须有上限——它是模块级全局，托盘常驻数周只增不减；一次深度体检 / GC 会把
# 对象池里每一个对象都塞进来，生产库实测对象池 48,775 个文件，等于白扛几 MB
# 常驻内存且永不释放。用 OrderedDict 当 LRU：超上限就淘汰最久未用的，
# 淘汰只损失一次重新哈希，不影响正确性。
_VERIFIED_OBJECT_CAP = 4096
_VERIFIED_OBJECT_PATHS: OrderedDict[str, None] = OrderedDict()
_VERIFIED_LOCK = threading.Lock()
_STABLE_COPY_RETRY_DELAYS_SEC = (0.15, 0.4, 0.9)


def _verified_mark(key: str) -> None:
    with _VERIFIED_LOCK:
        _VERIFIED_OBJECT_PATHS.pop(key, None)
        _VERIFIED_OBJECT_PATHS[key] = None
        while len(_VERIFIED_OBJECT_PATHS) > _VERIFIED_OBJECT_CAP:
            _VERIFIED_OBJECT_PATHS.popitem(last=False)


def _verified_hit(key: str) -> bool:
    with _VERIFIED_LOCK:
        if key not in _VERIFIED_OBJECT_PATHS:
            return False
        _VERIFIED_OBJECT_PATHS.move_to_end(key)
        return True


def _verified_forget(key: str) -> None:
    with _VERIFIED_LOCK:
        _VERIFIED_OBJECT_PATHS.pop(key, None)


class SnapshotSourceError(OSError):
    """The source could not be captured as one coherent point-in-time file."""


class SnapshotSourceChangedError(SnapshotSourceError):
    """The source changed while it was being copied."""


class InvalidSnapshotError(SnapshotSourceError):
    """The stable source is not a parseable PPTX recovery point."""


def vault_dir() -> Path:
    from ..config import get_version_vault_dir

    override = get_version_vault_dir()
    p = Path(override) if override else data_dir() / "vault"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return vault_dir() / "versions.db"


def doc_id_for(path: str) -> str:
    norm = os.path.normcase(os.path.abspath(path))
    return xxhash.xxh64(norm.encode("utf-8")).hexdigest()


def _doc_dir(doc_id: str) -> Path:
    d = vault_dir() / doc_id
    (d / "versions").mkdir(parents=True, exist_ok=True)
    (d / "objects").mkdir(parents=True, exist_ok=True)
    return d


def _objects_dir(doc_id: str) -> Path:
    """Legacy per-document object directory (kept for read compatibility)."""
    return _doc_dir(doc_id) / "objects"


def _global_objects_dir() -> Path:
    """Shared content-addressed pool used by all documents."""
    p = vault_dir() / _GLOBAL_OBJECTS_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _hash_path(path: Path) -> str:
    h = xxhash.xxh64()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _object_path(doc_id: str, object_hash: str) -> Path:
    """Resolve new global objects first, then the legacy per-document pool."""
    shared = _global_objects_dir() / object_hash
    if shared.exists():
        return shared
    return vault_dir() / doc_id / "objects" / object_hash


def _object_is_valid(path: Path, object_hash: str) -> bool:
    key = str(path)
    if not path.exists():
        _verified_forget(key)
        return False
    if _verified_hit(key):
        return True
    if _hash_path(path) != object_hash:
        return False
    _verified_mark(key)
    return True


def _install_object_bytes(data: bytes, object_hash: str) -> Path:
    """Crash-safe idempotent write into the shared object pool."""
    objd = _global_objects_dir()
    dest = objd / object_hash
    if _object_is_valid(dest, object_hash):
        return dest
    fd, tmp = tempfile.mkstemp(prefix=".object-", dir=objd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        _verified_mark(str(dest))
        return dest
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _install_object_file(src: Path, object_hash: str) -> tuple[Path, bool]:
    """Install a verified legacy object; return (destination, already_existed)."""
    dest = _global_objects_dir() / object_hash
    if _object_is_valid(dest, object_hash):
        return dest, True
    try:
        os.link(src, dest)
    except FileExistsError:
        if not _object_is_valid(dest, object_hash):
            _install_object_bytes(src.read_bytes(), object_hash)
        return dest, True
    except OSError:
        _install_object_bytes(src.read_bytes(), object_hash)
    return dest, False


def _manifest_path(doc_id: str, version_id: str) -> Path:
    return _doc_dir(doc_id) / "versions" / f"{version_id}.json"


def version_file(doc_id: str, version_id: str) -> Path:
    """mode=full 回退时的完整 pptx 路径。"""
    return _doc_dir(doc_id) / "versions" / f"{version_id}.pptx"


def manifest_for(doc_id: str, version_id: str | None) -> dict:
    if not version_id:
        return {}
    mf = _manifest_path(doc_id, version_id)
    if not mf.exists():
        return {}
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _part_bucket(name: str) -> str:
    n = name.lower()
    if n.startswith("ppt/slides/slide") and n.endswith(".xml"):
        return "slides"
    if n.startswith("ppt/notesslides/"):
        return "notes"
    if n.startswith("ppt/media/"):
        return "media"
    if n.startswith("ppt/charts/"):
        return "charts"
    if n.startswith("ppt/diagrams/"):
        return "diagrams"
    if n.startswith("ppt/theme/"):
        return "theme"
    if n.startswith("ppt/slidelayouts/") or n.startswith("ppt/slidemasters/"):
        return "layout"
    return "other"


def manifest_diff(doc_id: str, old_version_id: str | None, new_version_id: str) -> dict:
    new_parts = dict(manifest_for(doc_id, new_version_id).get("parts") or {})
    old_parts = dict(manifest_for(doc_id, old_version_id).get("parts") or {}) if old_version_id else {}
    old_names = set(old_parts)
    new_names = set(new_parts)
    added = new_names - old_names
    removed = old_names - new_names
    changed = {name for name in (old_names & new_names) if old_parts.get(name) != new_parts.get(name)}
    buckets: dict[str, dict[str, int]] = {}
    for kind, names in (("added", added), ("removed", removed), ("changed", changed)):
        for name in names:
            row = buckets.setdefault(_part_bucket(name), {"added": 0, "removed": 0, "changed": 0})
            row[kind] += 1
    return {
        "added_parts": len(added),
        "removed_parts": len(removed),
        "changed_parts": len(changed),
        "buckets": buckets,
    }


def _raw_file_hash(path: str) -> str:
    h = xxhash.xxh64()
    with open(ext_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _package_content_hash_from_parts(parts: dict[str, str]) -> str:
    h = xxhash.xxh64()
    for name in sorted(parts):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(str(parts[name]).encode("ascii", errors="ignore"))
        h.update(b"\0")
    return f"pkg:{h.hexdigest()}"


def manifest_content_hash(doc_id: str, version_id: str | None) -> str:
    """Canonical content hash for a stored version manifest.

    The hash ignores ZIP metadata/compression and only depends on package part
    names plus part bytes, so rebuilding/exporting a version still matches the
    original logical PPTX content.
    """
    if not version_id:
        return ""
    mf = manifest_for(doc_id, version_id)
    parts = dict(mf.get("parts") or {})
    if parts:
        return _package_content_hash_from_parts(parts)
    full = version_file(doc_id, version_id)
    if full.exists():
        return file_hash(str(full))
    return ""


def file_hash(path: str) -> str:
    """Canonical PPTX content hash.

    ZIP containers can differ after export/rebuild even when every OpenXML part
    is identical. Hash the sorted package part map instead of raw bytes so copy
    branch detection survives harmless repackaging.
    """
    try:
        with zipfile.ZipFile(ext_path(path)) as zf:
            parts: dict[str, str] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                part_hash = xxhash.xxh64()
                with zf.open(info) as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        part_hash.update(chunk)
                parts[info.filename] = part_hash.hexdigest()
            return _package_content_hash_from_parts(parts)
    except (OSError, zipfile.BadZipFile):
        return f"file:{_raw_file_hash(path)}"


def _file_hash(path: str) -> str:
    return file_hash(path)


def _new_vid() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


@contextmanager
def stable_snapshot_source(path: str):
    """Yield one immutable temporary copy of ``path``.

    PowerPoint and sync clients can replace a package while a watcher callback
    is already running.  Hashing the live path and then opening it again can
    mix two saves.  We copy once, require source stat stability across that
    copy, and make every later snapshot stage read only the temporary file.
    """
    source = os.path.abspath(path)
    tmp_root = vault_dir() / "_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    prepared = ""
    for attempt, delay in enumerate((0.0, *_STABLE_COPY_RETRY_DELAYS_SEC)):
        if delay:
            time.sleep(delay)
        fd, tmp = tempfile.mkstemp(prefix=".pptdoctor-snapshot-", suffix=".pptx", dir=str(tmp_root))
        os.close(fd)
        try:
            before = os.stat(ext_path(source))
            shutil.copyfile(ext_path(source), ext_path(tmp))
            with open(ext_path(tmp), "rb+") as copied:
                os.fsync(copied.fileno())
            after = os.stat(ext_path(source))
            copied_stat = os.stat(ext_path(tmp))
            stable = (
                before.st_size == after.st_size == copied_stat.st_size
                and before.st_mtime_ns == after.st_mtime_ns
            )
            if not stable:
                raise SnapshotSourceChangedError(source)
            prepared = tmp
            break
        except (OSError, SnapshotSourceChangedError) as exc:
            last_error = exc
            try:
                os.unlink(ext_path(tmp))
            except OSError:
                pass
            if attempt >= len(_STABLE_COPY_RETRY_DELAYS_SEC):
                break
    if not prepared:
        if isinstance(last_error, SnapshotSourceChangedError):
            raise last_error
        raise SnapshotSourceError(source) from last_error
    try:
        yield prepared
    finally:
        _unlink_snapshot_tmp(prepared)


# 所有受管暂存前缀：留底副本 / 保真自检 / 版本预览 / 重组恢复（崩溃残留靠启动清扫回收）
_STALE_TMP_PREFIXES = (
    ".pptdoctor-snapshot-",
    ".pptdoctor-verify-",
    ".pptdoctor-preview-",
    ".pptdoctor-restore-",
)


def _unlink_snapshot_tmp(tmp: str) -> None:
    """删除留底暂存副本：杀软/索引器可能短暂锁定新写入的大文件，
    静默吞 OSError 会让 180MB 级暂存永久残留（实测同事 C 盘被此类文件堆爆）。"""
    for attempt in range(4):
        try:
            os.unlink(ext_path(tmp))
            return
        except OSError:
            if attempt == 3:
                log.warning("snapshot temp cleanup failed after retries: %s", tmp, exc_info=True)
            else:
                time.sleep(0.3 * (attempt + 1))


def sweep_stale_snapshot_temps(*, max_age_sec: float = 0.0, tempdir: str | None = None) -> int:
    """清扫残留的 .pptdoctor-* 暂存（%TEMP% 与版本库 _tmp），返回删除数。

    暂存文件只应存活于一次留底操作期间；进程被杀或删除失败都会永久残留，
    且此前没有任何清扫机制。启动时调用（max_age_sec=0）：此刻不可能有在途留底操作。
    """
    if tempdir is not None:
        roots = [Path(tempdir)]
    else:
        roots = [Path(tempfile.gettempdir())]
        try:
            roots.append(vault_dir() / "_tmp")  # 暂存已改为随版本库位置（可迁出 C 盘）
        except OSError:
            pass
    now = time.time()
    deleted = 0
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.name.startswith(_STALE_TMP_PREFIXES):
                    continue
                if max_age_sec > 0 and now - entry.stat().st_mtime < max_age_sec:
                    continue
                entry.unlink()
                deleted += 1
            except OSError:
                continue
    if deleted:
        log.info("swept %d stale snapshot temp files", deleted)
    return deleted


def _write_zip(
    dest: str, doc_id: str, names: list[str], parts: dict[str, str],
    *, compression: int = zipfile.ZIP_DEFLATED,
) -> None:
    """把对象池里的 part 重组成一个 pptx 包。

    compression 只影响产物体积与写入耗时，不影响「能否重组、能否解析」。
    恢复 / 导出走默认的 DEFLATE（产物要交回用户磁盘）；留底时的保真自检走
    ZIP_STORED——那个临时文件写完就删，为它把几十 MB 图片再压一遍纯属白烧 CPU。
    """
    with zipfile.ZipFile(dest, "w", compression) as z:
        for name in names:
            z.writestr(name, _object_path(doc_id, parts[name]).read_bytes())


def _dedup_store(doc_id: str, path: str) -> tuple[list[str], dict[str, str]]:
    """解压 pptx，逐 part 内容寻址写入全局对象池。"""
    _doc_dir(doc_id)  # 保持每文档 manifest / full 目录结构与旧版本兼容
    names: list[str] = []
    parts: dict[str, str] = {}
    with zipfile.ZipFile(ext_path(path)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            data = zf.read(name)
            h = xxhash.xxh64(data).hexdigest()
            _install_object_bytes(data, h)
            names.append(name)
            parts[name] = h
    return names, parts


def migrate_legacy_objects() -> dict[str, int]:
    """Move per-document objects into the shared pool, safely and resumably.

    Each source is hash-verified before installation and removed only after the
    global copy verifies. Re-running after a crash is therefore idempotent.
    """
    result = {
        "scanned": 0,
        "migrated": 0,
        "duplicates": 0,
        "bytes_reclaimed": 0,
        "errors": 0,
    }
    root = vault_dir()
    for doc_dir in list(root.iterdir()):
        if not doc_dir.is_dir() or doc_dir.name == _GLOBAL_OBJECTS_DIRNAME:
            continue
        legacy = doc_dir / "objects"
        if not legacy.is_dir():
            continue
        for src in list(legacy.iterdir()):
            if not src.is_file() or not _OBJECT_HASH_RE.fullmatch(src.name):
                continue
            result["scanned"] += 1
            try:
                size = src.stat().st_size
                if _hash_path(src) != src.name:
                    result["errors"] += 1
                    continue
                dest, existed = _install_object_file(src, src.name)
                if not _object_is_valid(dest, src.name):
                    result["errors"] += 1
                    continue
                if existed:
                    result["duplicates"] += 1
                    result["bytes_reclaimed"] += size
                else:
                    result["migrated"] += 1
                src.unlink()
            except OSError:
                result["errors"] += 1
    return result


def backfill_content_hashes(conn) -> dict[str, int]:
    """Upgrade legacy raw ZIP hashes to canonical package hashes in place."""
    result = {"checked": 0, "updated": 0, "errors": 0}
    rows = conn.execute(
        "SELECT version_id, doc_id, content_hash FROM versions"
    ).fetchall()
    for row in rows:
        current = str(row["content_hash"] or "")
        if current.startswith(("pkg:", "file:")):
            continue
        result["checked"] += 1
        try:
            canonical = manifest_content_hash(
                str(row["doc_id"]),
                str(row["version_id"]),
            )
            if not canonical:
                result["errors"] += 1
                continue
            conn.execute(
                "UPDATE versions SET content_hash=? WHERE version_id=?",
                (canonical, row["version_id"]),
            )
            result["updated"] += 1
        except (OSError, ValueError, TypeError):
            result["errors"] += 1
    conn.commit()
    return result


def delete_version_artifacts(doc_id: str, version_id: str) -> None:
    """Remove non-shared files owned exclusively by a version DB row."""
    for path in (_manifest_path(doc_id, version_id), version_file(doc_id, version_id)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def collect_garbage(conn, *, dry_run: bool = True) -> dict[str, int | bool]:
    """Delete only artifacts proven unreachable from every live DB version.

    Safety gate: one missing/invalid live manifest or referenced object aborts
    the entire mutation pass. An inconsistent vault is reported, never cleaned.
    """
    result: dict[str, int | bool] = {
        "aborted": False,
        "errors": 0,
        "manifests_removed": 0,
        "full_files_removed": 0,
        "objects_removed": 0,
        "temp_objects_removed": 0,
        "bytes_reclaimed": 0,
    }
    # 崩溃残留的 .object-* 暂存永远不会被任何 manifest 引用：超过 1 小时直接清扫。
    # 独立于下面的结构安全门——恢复图不一致时中止 GC，但清残留总是安全的。
    now = time.time()
    for path in _global_objects_dir().iterdir():
        try:
            if not path.is_file() or not path.name.startswith(".object-"):
                continue
            if now - path.stat().st_mtime < 3600:
                continue  # 可能是在途写入（mkstemp 创建后随即 os.replace）
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            result["temp_objects_removed"] = int(result["temp_objects_removed"]) + 1
            result["bytes_reclaimed"] = int(result["bytes_reclaimed"]) + size
        except OSError:
            continue
    rows = conn.execute("SELECT version_id, doc_id FROM versions").fetchall()
    live_manifests = {
        (str(row["doc_id"]), str(row["version_id"])) for row in rows
    }
    referenced: set[str] = set()
    root = vault_dir()
    shared_by_hash = {
        path.name: path
        for path in _global_objects_dir().iterdir()
        if path.is_file() and _OBJECT_HASH_RE.fullmatch(path.name)
    }

    missing_branch_bases = conn.execute(
        """SELECT COUNT(*)
           FROM doc_branches AS b
           LEFT JOIN versions AS v ON v.version_id=b.branched_from_version_id
           WHERE v.version_id IS NULL"""
    ).fetchone()[0]
    if missing_branch_bases:
        result["errors"] = int(result["errors"]) + int(missing_branch_bases)

    # First pass is read-only and validates the complete recovery graph.
    for row in rows:
        doc_id = str(row["doc_id"])
        version_id = str(row["version_id"])
        mf = vault_dir() / doc_id / "versions" / f"{version_id}.json"
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            mode = manifest.get("mode")
            if mode == "dedup":
                parts = manifest.get("parts")
                names = manifest.get("names")
                if not isinstance(parts, dict) or not isinstance(names, list):
                    raise ValueError("dedup manifest has no parts map")
                if any(str(name) not in parts for name in names):
                    raise ValueError("manifest order references a missing part")
                for object_hash in parts.values():
                    object_hash = str(object_hash)
                    if not _OBJECT_HASH_RE.fullmatch(object_hash):
                        raise ValueError("invalid object hash")
                    referenced.add(object_hash)
                    if (
                        object_hash not in shared_by_hash
                        and not (root / doc_id / "objects" / object_hash).is_file()
                    ):
                        raise FileNotFoundError(object_hash)
            elif mode == "full":
                full = vault_dir() / doc_id / "versions" / f"{version_id}.pptx"
                if not full.is_file():
                    raise FileNotFoundError(full)
            else:
                raise ValueError("invalid manifest mode")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            result["errors"] = int(result["errors"]) + 1

    if result["errors"]:
        result["aborted"] = True
        return result

    orphan_manifests: list[Path] = []
    orphan_full: list[Path] = []
    legacy_objects: list[Path] = []
    for doc_dir in list(root.iterdir()):
        if not doc_dir.is_dir() or doc_dir.name == _GLOBAL_OBJECTS_DIRNAME:
            continue
        versions_dir = doc_dir / "versions"
        if versions_dir.is_dir():
            orphan_manifests.extend(
                p for p in versions_dir.glob("*.json")
                if (doc_dir.name, p.stem) not in live_manifests
            )
            orphan_full.extend(
                p for p in versions_dir.glob("*.pptx")
                if (doc_dir.name, p.stem) not in live_manifests
            )
        legacy = doc_dir / "objects"
        if legacy.is_dir():
            legacy_objects.extend(
                p for p in legacy.iterdir()
                if p.is_file() and p.name not in referenced
            )
    shared_objects = [
        path for object_hash, path in shared_by_hash.items()
        if object_hash not in referenced
    ]

    def remove(paths: list[Path], counter: str) -> None:
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if not dry_run:
                try:
                    path.unlink()
                    _verified_forget(str(path))
                except OSError:
                    result["errors"] = int(result["errors"]) + 1
                    continue
            result[counter] = int(result[counter]) + 1
            result["bytes_reclaimed"] = int(result["bytes_reclaimed"]) + size

    remove(orphan_manifests, "manifests_removed")
    remove(orphan_full, "full_files_removed")
    remove(shared_objects + legacy_objects, "objects_removed")
    return result


def audit_repository(conn, *, deep: bool = False) -> dict:
    """Validate the recovery graph without mutating repository files.

    The quick pass validates every DB row, manifest and referenced artifact.
    The deep pass additionally re-hashes every shared object, providing a
    user-invokable equivalent of ``git fsck`` for the local PPT vault.
    """
    rows = conn.execute(
        "SELECT version_id, doc_id, content_hash FROM versions ORDER BY version_id"
    ).fetchall()
    root = vault_dir()
    shared = {
        p.name: p
        for p in _global_objects_dir().iterdir()
        if p.is_file() and _OBJECT_HASH_RE.fullmatch(p.name)
    }
    object_paths = dict(shared)
    referenced: set[str] = set()
    object_versions: dict[str, set[str]] = {}
    invalid: dict[str, str] = {}
    missing_objects: set[str] = set()
    full_versions = 0

    for row in rows:
        version_id = str(row["version_id"])
        doc_id = str(row["doc_id"])
        mf = vault_dir() / doc_id / "versions" / f"{version_id}.json"
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            mode = manifest.get("mode")
            if mode == "dedup":
                names = manifest.get("names")
                parts = manifest.get("parts")
                if not isinstance(names, list) or not isinstance(parts, dict):
                    raise ValueError("invalid dedup manifest")
                if not all(isinstance(name, str) for name in names):
                    raise ValueError("manifest contains a non-string part name")
                if len(names) != len(set(names)) or set(names) != set(parts):
                    raise ValueError("manifest names/parts do not match")
                for raw_hash in parts.values():
                    if not _OBJECT_HASH_RE.fullmatch(str(raw_hash)):
                        raise ValueError("invalid object hash")
                stored_hash = str(row["content_hash"] or "")
                manifest_hash = _package_content_hash_from_parts(parts)
                if stored_hash.startswith(("pkg:", "file:")) and stored_hash != manifest_hash:
                    raise ValueError("content hash mismatch between database and manifest")
                version_missing: list[str] = []
                for raw_hash in parts.values():
                    object_hash = str(raw_hash)
                    referenced.add(object_hash)
                    object_versions.setdefault(object_hash, set()).add(version_id)
                    if object_hash not in shared:
                        legacy = root / doc_id / "objects" / object_hash
                        if legacy.is_file():
                            object_paths.setdefault(object_hash, legacy)
                        else:
                            missing_objects.add(object_hash)
                            version_missing.append(object_hash)
                if version_missing:
                    raise FileNotFoundError(
                        f"{len(version_missing)} referenced objects are missing"
                    )
            elif mode == "full":
                full_versions += 1
                full = vault_dir() / doc_id / "versions" / f"{version_id}.pptx"
                if not full.is_file():
                    raise FileNotFoundError("missing full snapshot")
                deck = parse_pptx(str(full))
                if deck.status != "ok":
                    raise ValueError(
                        f"full snapshot is not parseable: {deck.status} "
                        f"({getattr(deck, 'error', '') or 'invalid PPTX'})"
                    )
                stored_hash = str(row["content_hash"] or "")
                full_hash = file_hash(str(full))
                if stored_hash.startswith(("pkg:", "file:")) and stored_hash != full_hash:
                    raise ValueError("content hash mismatch between database and full snapshot")
            else:
                raise ValueError("unknown manifest mode")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            invalid[version_id] = f"{type(exc).__name__}: {exc}"

    hash_errors: list[str] = []
    read_errors: list[str] = []
    bytes_checked = 0
    if deep:
        for object_hash, path in object_paths.items():
            try:
                bytes_checked += path.stat().st_size
                if _hash_path(path) != object_hash:
                    hash_errors.append(object_hash)
                    _verified_forget(str(path))
            except OSError:
                read_errors.append(object_hash)
                _verified_forget(str(path))

        # A corrupt shared object can invalidate many restore points.  Map the
        # pool-level failure back to every version that references it so the
        # UI can quarantine those exact recovery points until a later deep
        # check proves the bytes healthy again.
        for object_hash in hash_errors:
            for version_id in object_versions.get(object_hash, ()):
                invalid.setdefault(
                    version_id,
                    f"deep: object hash mismatch ({object_hash})",
                )
        for object_hash in read_errors:
            for version_id in object_versions.get(object_hash, ()):
                invalid.setdefault(
                    version_id,
                    f"deep: object read failed ({object_hash})",
                )

    unreferenced = set(shared) - referenced
    ok = not (
        invalid
        or missing_objects
        or hash_errors
        or read_errors
    )
    return {
        "ok": ok,
        "deep": bool(deep),
        "versions_checked": len(rows),
        "full_versions": full_versions,
        "invalid_versions": invalid,
        "invalid_count": len(invalid),
        "referenced_objects": len(referenced),
        "missing_objects": len(missing_objects),
        "shared_objects": len(shared),
        "unreferenced_objects": len(unreferenced),
        "objects_hashed": len(object_paths) if deep else 0,
        "bytes_hashed": bytes_checked,
        "hash_errors": len(hash_errors),
        "read_errors": len(read_errors),
    }


def _verify(doc_id: str, names: list[str], parts: dict[str, str]) -> bool:
    """重组到临时文件并验证能正常解析（保真自检）。

    用 ZIP_STORED 重组：这个临时包写完就删，唯一用途是证明「对象齐全、能重组回
    可解析的 OpenXML 包」——压缩方式不影响这个结论。而 file_hash 哈希的是
    「part 名 → part 内容哈希」的排序映射（刻意与 ZIP 重打包无关），所以哈希比对
    照样成立。50 MB 稿实测：这一步 1436 ms → 145 ms，整次留底 2.0 s → 0.7 s，
    直接决定用户按下 Ctrl+S 之后我们要抢走多少 CPU。
    """
    # 自检暂存改放版本库 _tmp（启动清扫覆盖，可随库迁出 C 盘），不再落裸 %TEMP%。
    tmp_root = vault_dir() / "_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".pptdoctor-verify-", suffix=".pptx", dir=str(tmp_root)
    )
    os.close(fd)
    try:
        _write_zip(tmp, doc_id, names, parts, compression=zipfile.ZIP_STORED)
        return (
            parse_pptx(tmp).status == "ok"
            and file_hash(tmp) == _package_content_hash_from_parts(parts)
        )
    except Exception:  # noqa: BLE001
        return False
    finally:
        _unlink_snapshot_tmp(tmp)


def _change_summary(conn, latest_vid: str, new_pages: list, new_pc: int, old_pc: int) -> str:
    """对比上一版逐页文本，给一句大致改动简述（改了几页 + 页数增减）。"""
    try:
        old = {r["page_no"]: r["content"] for r in conn.execute(
            "SELECT page_no, content FROM version_pages_fts WHERE version_id=?", (latest_vid,))}
    except Exception:  # noqa: BLE001
        old = {}
    new = {pno: txt for pno, txt in new_pages}
    changed_pages = sum(1 for p in (set(old) & set(new)) if (old.get(p) or "") != (new.get(p) or ""))
    parts = []
    if changed_pages:
        parts.append(f"改 {changed_pages} 页")
    d = new_pc - old_pc
    if d > 0:
        parts.append(f"+{d} 页")
    elif d < 0:
        parts.append(f"{d} 页")
    return " · ".join(parts) if parts else "内容微调"


def snapshot(
    conn,
    path: str,
    session_id: str = "",
    doc_id: str | None = None,
    base_version=None,
    content_hash: str | None = None,
    source_path: str | None = None,
) -> str | None:
    """对 path 当前内容拍快照（按页去重）；内容相对最新版未变则跳过（返回 None）。"""
    path = os.path.abspath(path)
    if source_path is None:
        if not os.path.exists(path):
            return None
        with stable_snapshot_source(path) as stable:
            return snapshot(
                conn,
                path,
                session_id,
                doc_id=doc_id,
                base_version=base_version,
                content_hash=content_hash,
                source_path=stable,
            )

    source_path = os.path.abspath(source_path)
    if not os.path.exists(source_path):
        raise SnapshotSourceError(source_path)
    deck = parse_pptx(source_path)
    if deck.status != "ok":
        error = getattr(deck, "error", "") or "invalid PPTX"
        raise InvalidSnapshotError(f"{path}: {deck.status} ({error})")

    chash = content_hash or _file_hash(source_path)
    did = doc_id or doc_id_for(path)
    latest = base_version if base_version is not None else store.latest_version(conn, did)
    if (
        latest is not None
        and "health" in latest.keys()
        and str(latest["health"] or "ok") != "ok"
    ):
        # An identical live file is valuable repair material when the stored
        # recovery point was quarantined. Never let hash dedupe suppress the
        # creation of a fresh healthy baseline in that case.
        latest = None
    latest_doc_id = (latest["doc_id"] if latest is not None and "doc_id" in latest.keys() else did)
    if latest is not None and (
        latest["content_hash"] == chash
        or manifest_content_hash(latest_doc_id, latest["version_id"]) == chash
    ):
        store.upsert_doc(conn, did, path, datetime.datetime.now().timestamp())
        conn.commit()
        return None

    vid = _new_vid()
    _doc_dir(did)
    mode = "dedup"
    names: list[str] = []
    parts: dict[str, str] = {}
    try:
        names, parts = _dedup_store(did, source_path)
        if not _verify(did, names, parts):
            mode = "full"
    except Exception:  # noqa: BLE001 解压失败 → 完整拷贝兜底
        mode = "full"
    if mode == "full":
        shutil.copy2(ext_path(source_path), version_file(did, vid))
        names, parts = [], {}

    manifest_path = _manifest_path(did, vid)
    fd, manifest_tmp = tempfile.mkstemp(
        prefix=".manifest-",
        suffix=".json",
        dir=manifest_path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as manifest_file:
            json.dump({"mode": mode, "names": names, "parts": parts}, manifest_file)
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        os.replace(manifest_tmp, manifest_path)
    finally:
        try:
            os.unlink(manifest_tmp)
        except OSError:
            pass

    # 解析逐页文本（供跨版本搜 + 页数）
    pages = []
    pages = [(pg.page_no, tokenize(pg.raw_text)) for pg in deck.pages]
    now = datetime.datetime.now().timestamp()
    try:
        size = os.path.getsize(ext_path(source_path))
    except OSError:
        size = 0
    changed = (_change_summary(conn, latest["version_id"], pages, deck.page_count, latest["page_count"] or 0)
               if latest is not None else "")
    store.upsert_doc(conn, did, path, now)
    store.add_version(conn, vid, did, now, session_id, deck.page_count, size, chash, changed=changed)
    store.index_pages(conn, did, vid, pages)
    store.set_latest(conn, did, vid)
    conn.commit()
    return vid


#: rebuild_to 的失败原因（供 UI 给出可执行的下一步，而不是干巴巴一句「恢复失败」）
REBUILD_ERR_LOCKED = "target_locked"      # 目标文件被占用（十有八九是 PowerPoint 正开着它）
REBUILD_ERR_MISSING = "recovery_missing"  # 恢复点的 manifest / 全量文件不见了
REBUILD_ERR_CORRUPT = "recovery_corrupt"  # 重组出来的包解析不过或内容哈希对不上
REBUILD_ERR_IO = "io_error"


def rebuild_to(doc_id: str, version_id: str, dest: str, *, on_error=None) -> bool:
    """把某版本原子重组/恢复到 dest。

    始终先在目标同目录生成并验证临时文件，最后用 ``os.replace`` 一次切换。
    任一对象缺失、manifest 损坏或校验失败时，现有目标文件保持逐字节不变。

    on_error(reason) 可选：拿到上面四个 REBUILD_ERR_* 之一。这里区分「文件被占用」
    是有意义的——用户一边开着 PowerPoint 一边点恢复是最常见的情形，只报「恢复失败」
    等于让人去猜。注意：占用发生在最后一步 os.replace，此时原文件仍然完好无损。
    """
    def _fail(reason: str) -> bool:
        if on_error is not None:
            try:
                on_error(reason)
            except Exception:  # noqa: BLE001 上报失败绝不能盖住恢复本身的结果
                pass
        return False

    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    mf = _manifest_path(doc_id, version_id)
    if not mf.exists():
        return _fail(REBUILD_ERR_MISSING)
    fd, tmp = tempfile.mkstemp(
        prefix=".pptdoctor-restore-",
        suffix=".pptx",
        dir=os.path.dirname(dest),
    )
    os.close(fd)
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
        mode = m.get("mode")
        if mode == "full":
            src = version_file(doc_id, version_id)
            if not src.exists():
                return _fail(REBUILD_ERR_MISSING)
            shutil.copy2(src, ext_path(tmp))
            if parse_pptx(tmp).status != "ok":
                return _fail(REBUILD_ERR_CORRUPT)
        elif mode == "dedup":
            _write_zip(ext_path(tmp), doc_id, m["names"], m["parts"])
            if parse_pptx(tmp).status != "ok":
                return _fail(REBUILD_ERR_CORRUPT)
        else:
            return _fail(REBUILD_ERR_CORRUPT)

        expected = manifest_content_hash(doc_id, version_id)
        if not expected or file_hash(tmp) != expected:
            return _fail(REBUILD_ERR_CORRUPT)
        try:
            os.replace(ext_path(tmp), ext_path(dest))
        except PermissionError:
            # Windows 上目标被别的进程以拒绝共享方式打开时就是这个错误。
            return _fail(REBUILD_ERR_LOCKED)
        return True
    except Exception:  # noqa: BLE001
        return _fail(REBUILD_ERR_IO)
    finally:
        try:
            os.unlink(ext_path(tmp))
        except OSError:
            pass


# ---- 幽灵文档收割与版本库迁移（2026-07-30） ----
def _norm_root(root: str) -> str:
    r = os.path.normcase(os.path.abspath(str(root)))
    return r if r.endswith(os.sep) else r + os.sep


def _fixed_drive_roots() -> list[str]:
    """本地固定磁盘根（normcase、带尾分隔符）。非 Windows 无盘符概念，返回空 = 不筛。"""
    if os.name != "nt":
        return []
    from ..scanner import fixed_drives

    try:
        return [_norm_root(r) for r in fixed_drives()]
    except Exception:  # noqa: BLE001 探测失败时不筛，等价于全部路径可判定
        return []


def _checkable_disappearance_paths(paths, fixed_roots: list[str]) -> list[str]:
    """可用来判定「消失」的路径子集：只有固定盘上的路径算数。

    移动硬盘/网络盘未挂载时 os.path.exists 同样是 False，但那不是消失；
    fixed_roots 为空（非 Windows 或探测失败）时不做盘筛选，全部路径可判定。
    """
    if not fixed_roots:
        return list(paths)
    out = []
    for p in paths:
        try:
            norm = os.path.normcase(os.path.abspath(p))
        except (OSError, ValueError):
            continue
        if any(norm.startswith(root) for root in fixed_roots):
            out.append(p)
    return out


def list_ghost_docs(
    conn,
    *,
    min_missing_sec: float = 0.0,
    fixed_roots: list[str] | None = None,
) -> list[dict]:
    """所有登记路径均已不存在的受管文档（幽灵文档）。

    存活判定覆盖 doc_paths 全部历史路径（含 alias）与 managed_docs.path 主路径：
    任一仍存在即算活文档。可判定性只基于「当前位置」——主路径 + doc_paths 中
    status='current' 的路径；历史 alias 可能是文件搬到网络盘之前的固定盘旧路径，
    拿它做可判定依据会把「当前在未挂载网络盘」的文档误判成可收割。
    移动硬盘/网络盘未挂载不算消失——当前位置全在可移动/网络盘上的文档无法判定，
    永不列入（也因此跳过了对离线 UNC 的秒级 exists 慢探测）。

    min_missing_sec > 0 时启用宽限期：仅当首次确认缺失（managed_docs.deleted_at，
    由对账/扫描/mark_ghost_docs_seen 写入）至今已满该时长才列入；deleted_at=0
    （从未被确认过缺失）在宽限模式下不列入——先补记，下轮维护再收。
    """
    roots = (
        [_norm_root(r) for r in fixed_roots]
        if fixed_roots is not None
        else _fixed_drive_roots()
    )
    now = time.time()
    ghosts: list[dict] = []
    rows = conn.execute(
        """SELECT d.doc_id AS doc_id, d.path AS path, d.deleted_at AS deleted_at,
                  (SELECT COUNT(*) FROM versions v WHERE v.doc_id=d.doc_id) AS n_versions
           FROM managed_docs d"""
    ).fetchall()
    paths_by_doc: dict[str, list[str]] = {}
    current_by_doc: dict[str, list[str]] = {}
    for pr in conn.execute("SELECT doc_id, path, status FROM doc_paths").fetchall():
        paths_by_doc.setdefault(str(pr["doc_id"]), []).append(str(pr["path"]))
        if str(pr["status"] or "") == "current":
            current_by_doc.setdefault(str(pr["doc_id"]), []).append(str(pr["path"]))
    for row in rows:
        doc_id = str(row["doc_id"])
        candidates = set(paths_by_doc.get(doc_id, [])) | {str(row["path"])}
        # 先固定盘过滤再 exists：无法判定的文档直接 skip，不付离线 UNC 的慢探测。
        checkable = _checkable_disappearance_paths(
            set(current_by_doc.get(doc_id, [])) | {str(row["path"])}, roots
        )
        if not checkable:
            continue
        if any(os.path.exists(p) for p in candidates):
            continue
        missing_since = float(row["deleted_at"] or 0)
        if min_missing_sec > 0 and (
            missing_since <= 0 or now - missing_since < min_missing_sec
        ):
            continue
        ghosts.append({
            "doc_id": doc_id,
            "path": str(row["path"]),
            "versions": int(row["n_versions"]),
            "missing_since": missing_since,
        })
    return ghosts


def _doc_has_live_path(conn, doc_id: str, main_path: str) -> bool:
    """删除前的最后一道复核：该文档还有没有任何一条登记路径真实存在。"""
    candidates = {str(main_path)} | set(store.list_doc_paths(conn, doc_id))
    return any(os.path.exists(p) for p in candidates)


def mark_ghost_docs_seen(
    conn, *, fixed_roots: list[str] | None = None, ghosts=None
) -> int:
    """给当前观察到的幽灵候选补记 deleted_at=now（宽限期从首次确认缺失起算）。

    只补 deleted_at=0 的文档；已确认过缺失的保留原时刻。返回新标记数。
    ghosts 可传入已经探测好的 list_ghost_docs 结果——探测要对上千条路径做
    os.path.exists，重维护里本来是 mark/reap 各跑一遍；调用方在锁外探测一次
    再分给两边，能把持锁时间直接砍掉一半（见 manager 的重维护流程）。
    """
    marked = 0
    now = time.time()
    candidates = (
        list_ghost_docs(conn, fixed_roots=fixed_roots) if ghosts is None else ghosts
    )
    for g in candidates:
        if g["missing_since"] > 0:
            continue
        conn.execute(
            "UPDATE managed_docs SET deleted_at=? WHERE doc_id=? AND deleted_at=0",
            (now, g["doc_id"]),
        )
        marked += 1
    if marked:
        conn.commit()
    return marked


def reap_ghost_docs(
    conn,
    *,
    dry_run: bool = True,
    min_missing_sec: float = 0.0,
    fixed_roots: list[str] | None = None,
    ghosts=None,
) -> dict:
    """清理幽灵文档：删除其版本、磁盘目录与 DB 记录，再做对象级 GC（collect_garbage）。

    min_missing_sec/fixed_roots 透传 list_ghost_docs：自动维护传 30 天宽限；
    设置页手动清理由用户逐次确认，保持默认立即判定。

    ghosts 可传入已探测好的候选（省掉重复的上千次 os.path.exists，见
    mark_ghost_docs_seen 的说明）。此时仍会在真正删除前逐个复核「所有登记路径
    确实都不存在」——探测与删除之间文件可能被恢复，宁可白探一次也不能误删。
    """
    if ghosts is None:
        ghosts = list_ghost_docs(
            conn, min_missing_sec=min_missing_sec, fixed_roots=fixed_roots
        )
    else:
        now = time.time()
        ghosts = [
            g for g in ghosts
            if (
                min_missing_sec <= 0
                or (
                    float(g.get("missing_since") or 0) > 0
                    and now - float(g["missing_since"]) >= min_missing_sec
                )
            )
            and not _doc_has_live_path(conn, g["doc_id"], g["path"])
        ]
    result = {
        "ghost_docs": len(ghosts),
        "ghost_versions": sum(g["versions"] for g in ghosts),
        "removed_dirs": 0,
        "gc": None,
        "dry_run": dry_run,
    }
    if dry_run or not ghosts:
        return result
    for g in ghosts:
        store.delete_doc(conn, g["doc_id"])
        d = vault_dir() / g["doc_id"]
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            result["removed_dirs"] += 1
    conn.commit()
    result["gc"] = collect_garbage(conn, dry_run=False)
    return result


# ---- 容量上限 / 库卫生 / 迁移备份清扫（版本库减负） ----
DEFAULT_VACUUM_MIN_FREE_BYTES = 64 * 1024 * 1024
DEFAULT_VACUUM_MIN_FREE_RATIO = 0.20
_SIZE_BUDGET_MAX_ROUNDS = 8
# 无进展即停：一轮（估计或实测）回收不足缺口 5%、或连最小字节数都收不回时，
# 继续驱逐只是拿健康历史换不到达标的纯损失（缺口被豁免地板占住）。
_SIZE_BUDGET_MIN_PROGRESS_RATIO = 0.05
_SIZE_BUDGET_MIN_PROGRESS_BYTES = 1
# 每个 active 文档在容量驱逐下的保底版本数（最新 N 个永不驱逐）。
# 生产库实测 3800 份文档里 3700 份只有 1 个版本——没有保底线时，一次超限驱逐
# 就等于按「最久没动」的顺序逐份抹掉整个文档的全部回滚历史。
_SIZE_BUDGET_KEEP_PER_ACTIVE_DOC = 1
_MIGRATION_BACKUP_MAX_AGE_SEC = 30 * 24 * 60 * 60


def _vault_dir_no_create() -> Path:
    """解析版本库目录但不创建（只读统计/清扫用，不给未开启版本管理的用户造目录）。"""
    from ..config import get_version_vault_dir

    override = get_version_vault_dir()
    return Path(override) if override else data_dir() / "vault"


def vault_size_bytes() -> int:
    """版本库目录当前总占用（字节）；目录不存在返回 0。

    用 os.scandir 递归而不是 Path.rglob：rglob 对每个条目还要再发 is_file() 与
    stat() 两次系统调用，而 scandir 在 Windows 上直接复用目录枚举里带回来的
    元数据。真实 3.4 GB 版本库实测 7.1 s → 1.4 s。这条路径在每周重维护里被
    enforce_size_budget 全程持锁反复调用，省下的就是 watcher 留底被阻塞的时间。
    """
    root = _vault_dir_no_create()
    if not root.is_dir():
        return 0
    total = 0
    stack = [str(root)]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    # DirEntry.stat() 在 Windows 上读的是目录项缓存，对「当前有打开
                    # 写句柄」的文件会给出过期大小——versions.db-wal 正是这种文件，
                    # 实测能少算 100 KB+。而 _budget_relevant_bytes 事后按真实
                    # stat 减掉这三个文件，少算就会减成负数、被 max(0,…) 夹成 0，
                    # 于是容量上限静默永不触发。库文件本来就只有三个，单独真 stat。
                    if entry.name.startswith("versions.db"):
                        total += os.stat(entry.path).st_size
                    else:
                        total += entry.stat().st_size
            except OSError:
                continue
    return total


def _branch_base_ids(conn) -> set[str]:
    """所有作为复制分支基线的 version_id（永不驱逐：它们是副本的恢复根）。"""
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT branched_from_version_id FROM doc_branches"
        ).fetchall()
        if row[0]
    }


def vault_health_snapshot() -> dict:
    """版本库体检快照：总占用 + 失效留底（源文件已不存在）的份数与可回收估计。

    此前这些数字只藏在设置页的版本管理区，用户得先点「清理失效版本」才知道有多少。
    真实库实测 3999 份受管文档里 1924 份的源文件已经没了、占掉版本库大头——
    这正是「C 盘暴涨」反馈的直接来源，应该在库体检里主动摆出来。

    只读；版本库不存在或不可读时返回 available=False，调用方按「未知」处理。
    """
    result = {
        "available": False,
        "vault_bytes": 0,
        "docs": 0,
        "ghost_docs": 0,
        "ghost_versions": 0,
        "ghost_bytes_estimate": 0,
    }
    root = _vault_dir_no_create()
    db_file = root / "versions.db"
    if not db_file.is_file():
        return result
    result["vault_bytes"] = vault_size_bytes()
    conn = None
    try:
        conn = store.connect(db_file)
        store.init_db(conn)  # 老库补列（deleted_at 等），保证收割查询可用
        result["docs"] = int(
            conn.execute("SELECT COUNT(*) FROM managed_docs").fetchone()[0]
        )
        ghosts = list_ghost_docs(conn)
        result["ghost_docs"] = len(ghosts)
        result["ghost_versions"] = sum(int(g["versions"]) for g in ghosts)
        if ghosts:
            ids = [g["doc_id"] for g in ghosts]
            total = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                total += int(
                    conn.execute(
                        f"SELECT COALESCE(SUM(size),0) FROM versions WHERE doc_id IN ({marks})",
                        chunk,
                    ).fetchone()[0]
                )
            # size 是快照时的源文件大小；对象池去重让实际回收小于它，标注为「估计」
            result["ghost_bytes_estimate"] = total
        result["available"] = True
    except (OSError, sqlite3.Error):
        return result
    finally:
        if conn is not None:
            conn.close()
    return result


def budget_relevant_bytes() -> int:
    """容量上限计量口径的公开入口——供调用方在持锁之前先量好（见 enforce_size_budget）。"""
    return _budget_relevant_bytes()


def _budget_relevant_bytes() -> int:
    """容量上限计量口径：vault 总占用减去 versions.db 三件套。

    版本删除本身会写 WAL——把库文件算进预算会让驱逐对着一个越删越大的目标空转；
    库文件体积由 maintain_db 的条件 VACUUM + WAL TRUNCATE 单独约束。
    """
    total = vault_size_bytes()
    root = _vault_dir_no_create()
    for name in ("versions.db", "versions.db-wal", "versions.db-shm"):
        try:
            total -= (root / name).stat().st_size
        except OSError:
            pass
    return max(0, total)


def survivor_version_ids(conn, keep_per_active_doc: int = _SIZE_BUDGET_KEEP_PER_ACTIVE_DOC):
    """每个 active 文档最新 N 个版本的 version_id 集合——容量驱逐的保底线。

    「源文件还在磁盘上、版本库里却一个可回滚版本都没有」是产品承诺的反面
    （README：改崩了一键回到任意健康历史版本）。全局 ts ASC 驱逐天然先吃最久
    没动过的文档，而那恰恰是最需要旧版的一类：近期文件还能靠 OneDrive/回收站
    找回，两年前的稿子不能。这里按 doc 保留最新 N 个，不受全局驱逐顺序影响。

    已 deleted 的文档不在保底范围：其留底本就是幽灵收割的目标。
    """
    if keep_per_active_doc <= 0:
        return set()
    by_doc: dict[str, list[tuple[float, str]]] = {}
    for row in conn.execute(
        """SELECT v.doc_id AS doc_id, v.version_id AS version_id, v.ts AS ts
           FROM versions AS v
           JOIN managed_docs AS d ON d.doc_id=v.doc_id
           WHERE d.status='active'"""
    ).fetchall():
        by_doc.setdefault(str(row["doc_id"]), []).append(
            (float(row["ts"] or 0), str(row["version_id"]))
        )
    survivors: set[str] = set()
    for versions in by_doc.values():
        versions.sort(reverse=True)  # ts 新→旧，同 ts 用 version_id 兜底定序
        survivors.update(vid for _ts, vid in versions[:keep_per_active_doc])
    return survivors


def enforce_size_budget(
    conn, *, max_bytes: int,
    keep_per_active_doc: int = _SIZE_BUDGET_KEEP_PER_ACTIVE_DOC,
    measured_bytes: int | None = None,
) -> dict:
    """容量上限：超出时按 ts 从老到新驱逐健康版本，随后对象级 GC。

    只驱逐 health='ok'、非分支基、且不在每文档保底线内的版本：隔离版本可能可
    修复、分支基是副本的恢复根、每个 active 文档最新 N 个版本是「源文件还在就
    至少留得住一次回滚」的底线（见 survivor_version_ids），三者永不进驱逐候选。
    versions.size 是快照时源文件大小，只是占用估计；去重共享对象可能让实际回收
    小于估计，因此按轮循环直到达标或候选耗尽。
    GC 安全门一旦中止立即停止——不在结构存疑的库上继续删。
    无进展即停：一轮的估计回收（驱逐前）或实测回收（GC 后）不足缺口 5%
    （且不足最小字节数）时立即 break——预算低于豁免地板（隔离/分支基/保底线
    全量）时继续驱逐只是净损失健康历史。诊断字段：converged（最终是否达标）、
    floor_bytes（豁免地板估计下限，驱逐全部候选也降不到它以下；去重共享使
    真实地板更高）、protected_versions（本轮保底线规模）。
    计量口径见 _budget_relevant_bytes（不含 versions.db 本体）。
    """
    result = {
        "max_bytes": int(max_bytes),
        "skipped": False,
        "vault_bytes_before": 0,
        "evicted_versions": 0,
        "gc": None,
        "vault_bytes_after": 0,
        "converged": True,
        "floor_bytes": 0,
        "protected_versions": 0,
    }
    budget = int(max_bytes)
    if budget <= 0:
        result["skipped"] = True  # 0 = 不限
        return result
    # measured_bytes：调用方在锁外量好的口径。首次测量是整目录遍历（真实 3.4GB
    # 库约 2 秒），没有理由让 watcher 的留底排在它后面——重维护在进锁前先量。
    total = _budget_relevant_bytes() if measured_bytes is None else int(measured_bytes)
    result["vault_bytes_before"] = total
    if total > budget:
        # 豁免地板估计：全部可驱逐候选的 size 总和之外的部分永远降不下去。
        branch_bases = _branch_base_ids(conn)
        protected = branch_bases | survivor_version_ids(conn, keep_per_active_doc)
        result["protected_versions"] = len(protected)
        claimable = 0
        for row in conn.execute(
            "SELECT version_id, size FROM versions WHERE COALESCE(health,'ok')='ok'"
        ).fetchall():
            if str(row["version_id"]) not in protected:
                claimable += max(0, int(row["size"] or 0))
        result["floor_bytes"] = max(0, total - claimable)
    gc_result: dict | None = None
    for _ in range(_SIZE_BUDGET_MAX_ROUNDS):
        if total <= budget:
            break
        # 保底线每轮重算：上一轮驱逐后某文档的「最新 N 个」会变（少于 N 个时全保）
        protected = _branch_base_ids(conn) | survivor_version_ids(
            conn, keep_per_active_doc
        )
        result["protected_versions"] = len(protected)
        over_by = total - budget
        min_progress = max(
            _SIZE_BUDGET_MIN_PROGRESS_BYTES,
            int(over_by * _SIZE_BUDGET_MIN_PROGRESS_RATIO),
        )
        candidates: list[tuple[str, str, str]] = []
        claimed = 0
        for row in conn.execute(
            """SELECT version_id, doc_id, size, thumb_path FROM versions
               WHERE COALESCE(health,'ok')='ok'
               ORDER BY ts ASC, version_id ASC"""
        ).fetchall():
            vid = str(row["version_id"])
            if vid in protected:
                continue
            candidates.append((vid, str(row["doc_id"]), str(row["thumb_path"] or "")))
            claimed += max(0, int(row["size"] or 0))
            if claimed >= over_by:
                break
        if not candidates:
            break  # 只剩豁免版本：驱逐也降不下去，停
        if claimed < min_progress:
            break  # 估计口径无进展：整轮驱逐也收不到缺口的 5%，纯属损失健康历史
        for vid, doc_id, thumb_path in candidates:
            store.delete_version(conn, vid)
            delete_version_artifacts(doc_id, vid)
            if thumb_path:
                try:
                    Path(thumb_path).unlink(missing_ok=True)
                except OSError:
                    pass
            result["evicted_versions"] += 1
        conn.commit()
        gc_result = collect_garbage(conn, dry_run=False)
        new_total = _budget_relevant_bytes()
        progressed = total - new_total
        total = new_total
        if bool(gc_result.get("aborted", False)) or int(gc_result.get("errors", 0) or 0):
            break
        if progressed < min_progress:
            break  # 实测口径无进展：去重共享让实际回收远低于估计，同样停
    result["gc"] = gc_result
    result["vault_bytes_after"] = total
    result["converged"] = total <= budget
    return result


def maintain_db(
    conn,
    *,
    min_free_bytes: int = DEFAULT_VACUUM_MIN_FREE_BYTES,
    min_free_ratio: float = DEFAULT_VACUUM_MIN_FREE_RATIO,
) -> dict:
    """versions.db 卫生：FTS optimize + 条件 VACUUM + wal_checkpoint(TRUNCATE)。

    与索引侧（db.maintain）同一思路：删版本留下的 FTS 行先合并段树防退化；
    空闲页同时超过绝对量与比率阈值才做全量 VACUUM，重维护不为小碎片白扛大库重写。
    """
    result = {
        "fts_optimized": 0,
        "checkpointed": False,
        "vacuumed": False,
        "free_bytes_before": 0,
        "free_ratio_before": 0.0,
        # error = 意料之外的失败（会挡住重维护的 7 天节流标记）；
        # vacuum_error = VACUUM 这一步失败，属可选优化，不挡节流标记。
        # 二者分开是因为 VACUUM 需要约两倍库体积的临时空间，磁盘紧张时会稳定失败：
        # 混在一起会让 vault_heavy_maintenance_last_success 永远写不进去，
        # 于是每次启动都重跑一整套幽灵扫描 + 驱逐 + GC（全程持锁）。
        "error": "",
        "vacuum_error": "",
    }
    try:
        try:
            conn.execute(
                "INSERT INTO version_pages_fts(version_pages_fts) VALUES('optimize')"
            )
            result["fts_optimized"] += 1
        except sqlite3.DatabaseError as exc:
            log.debug("fts optimize skipped: %s", exc)
        conn.commit()

        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        free_bytes = page_size * free_pages
        free_ratio = (free_pages / page_count) if page_count else 0.0
        result["free_bytes_before"] = free_bytes
        result["free_ratio_before"] = free_ratio

        if (
            free_pages > 0
            and free_bytes >= max(0, int(min_free_bytes))
            and free_ratio >= max(0.0, float(min_free_ratio))
        ):
            try:
                conn.execute("VACUUM")
                result["vacuumed"] = True
                log.info(
                    "versions.db vacuum reclaimed candidate space: %.1f MiB (%.1f%% freelist)",
                    free_bytes / (1024 * 1024),
                    free_ratio * 100,
                )
            except sqlite3.DatabaseError as exc:
                result["vacuum_error"] = f"{type(exc).__name__}: {exc}"
                log.warning("versions.db vacuum skipped: %s", exc)

        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result["checkpointed"] = row is None or int(row[0]) == 0
        except sqlite3.DatabaseError as exc:
            log.debug("wal checkpoint skipped: %s", exc)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def sweep_stale_migration_backups(
    *,
    max_age_sec: float = _MIGRATION_BACKUP_MAX_AGE_SEC,
    data_root: str | None = None,
    vault_root: str | None = None,
) -> int:
    """清理一次性迁移备份：data_dir 下 index.db*.bak、vault/backups/versions-pre-*.db。

    只删匹配明确迁移备份命名模式且 mtime 超过 max_age_sec 的普通文件；
    同目录其它文件（包括现行 versions.db / index.db）一律不动。
    """
    data_dir_path = Path(data_root) if data_root is not None else data_dir()
    backups_dir = (
        Path(vault_root) / "backups"
        if vault_root is not None
        else _vault_dir_no_create() / "backups"
    )
    candidates: list[Path] = []
    try:
        candidates.extend(p for p in data_dir_path.glob("index.db*.bak") if p.is_file())
    except OSError:
        pass
    try:
        if backups_dir.is_dir():
            candidates.extend(
                p for p in backups_dir.glob("versions-pre-*.db") if p.is_file()
            )
    except OSError:
        pass
    now = time.time()
    deleted = 0
    for path in candidates:
        try:
            if max_age_sec > 0 and now - path.stat().st_mtime < max_age_sec:
                continue
            path.unlink()
            deleted += 1
        except OSError:
            continue
    if deleted:
        log.info("swept %d stale migration backups", deleted)
    return deleted


def _copy_db_live(src_db: Path, dst_db: Path) -> None:
    """用 sqlite backup API 复制可能处于打开状态的库（普通 copy 对 WAL 活跃库不安全）。"""
    import sqlite3 as _sq

    src = _sq.connect(str(src_db))
    dst = _sq.connect(str(dst_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def migrate_vault_dir(src: Path, dst: Path, progress_cb=None) -> dict:
    """把版本库整体迁到新目录：复制 → 校验（文件数+字节）→ 删除源。失败回滚目标、保留源。

    versions.db 走 sqlite backup（活跃连接下也一致）；WAL/SHM 随 checkpoint 并入新库不单独复制。
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_dir():
        raise ValueError(f"源目录不存在: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    if any(dst.iterdir()):
        raise ValueError(f"目标目录非空: {dst}")

    files = [f for f in src.rglob("*") if f.is_file()]
    total = len(files)
    copied = 0
    for f in files:
        rel = f.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if f.name == "versions.db":
            _copy_db_live(f, out)
        elif f.name.startswith("versions.db-"):
            continue  # WAL/SHM 不复制
        else:
            shutil.copy2(f, out)
        copied += 1
        if progress_cb and (copied % 50 == 0 or copied == total):
            progress_cb(copied, total)

    src_count = total - sum(1 for f in files if f.name.startswith("versions.db-"))
    dst_files = [f for f in dst.rglob("*") if f.is_file()]
    dst_bytes = sum(f.stat().st_size for f in dst_files)
    if src_count != len(dst_files):
        shutil.rmtree(dst, ignore_errors=True)
        raise RuntimeError(f"迁移校验失败（文件数 {src_count}/{len(dst_files)}），已回滚目标目录")

    # 逐文件字节校验：下一句就是 rmtree(src)，删掉的是用户全部 PPT 历史版本。
    # 只比文件数不够——目标盘写满、网络/移动盘中途截断都可能留下「个数对、内容短」
    # 的文件，而对象池是内容寻址的，短文件要等到用户真正回滚时才暴露成「恢复点损坏」。
    # versions.db 走的是 sqlite backup（重建页面，体积本就与源不同），改为结构自检。
    for f in files:
        if f.name.startswith("versions.db-"):
            continue
        out = dst / f.relative_to(src)
        if f.name == "versions.db":
            if not _sqlite_readable(out):
                shutil.rmtree(dst, ignore_errors=True)
                raise RuntimeError("迁移校验失败（新版本库无法打开或缺表），已回滚目标目录")
            continue
        try:
            if out.stat().st_size != f.stat().st_size:
                raise OSError("size mismatch")
        except OSError:
            shutil.rmtree(dst, ignore_errors=True)
            raise RuntimeError(
                f"迁移校验失败（文件内容不完整：{f.relative_to(src)}），已回滚目标目录"
            ) from None
    shutil.rmtree(src)
    return {"files": src_count, "bytes": dst_bytes}


def _sqlite_readable(db_file: Path) -> bool:
    """迁移后的 versions.db 结构自检。

    它走的是 sqlite backup API（按页重建），体积本就与源不同，比字节没有意义；
    要验的是「这是一个完整可读的库」。quick_check 会校验页结构与完整性，
    足以逮住截断/损坏，又比 integrity_check 快得多（不做逐索引交叉校验）。
    不检查具体表名：本函数只负责目录搬迁的完整性，不替业务层判断库内容。
    """
    conn = None
    try:
        conn = sqlite3.connect(str(db_file))
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()
