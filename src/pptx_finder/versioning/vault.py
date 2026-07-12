"""版本库：快照 / 列版本 / 重组恢复 / 导出。

按页（part）去重存储：pptx 是 zip，逐 part 内容寻址存进全局对象池，跨文档重复也只存一份；
每版只记一份 manifest（name→hash 列表）。改几个字只新增变化的 part，大幅省空间。
重组后做保真自检；万一失败，该版回退为完整拷贝（mode=full），保证一定能恢复。
"""
from __future__ import annotations

import datetime
from contextlib import contextmanager
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import xxhash

from ..config import data_dir, ext_path
from ..parser import parse_pptx
from ..text_tokenize import tokenize
from . import store

_OBJECT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_GLOBAL_OBJECTS_DIRNAME = "_objects"
_VERIFIED_OBJECT_PATHS: set[str] = set()
_STABLE_COPY_RETRY_DELAYS_SEC = (0.15, 0.4, 0.9)


class SnapshotSourceError(OSError):
    """The source could not be captured as one coherent point-in-time file."""


class SnapshotSourceChangedError(SnapshotSourceError):
    """The source changed while it was being copied."""


class InvalidSnapshotError(SnapshotSourceError):
    """The stable source is not a parseable PPTX recovery point."""


def vault_dir() -> Path:
    p = data_dir() / "vault"
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
        _VERIFIED_OBJECT_PATHS.discard(key)
        return False
    if key in _VERIFIED_OBJECT_PATHS:
        return True
    if _hash_path(path) != object_hash:
        return False
    _VERIFIED_OBJECT_PATHS.add(key)
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
        _VERIFIED_OBJECT_PATHS.add(str(dest))
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
    last_error: Exception | None = None
    prepared = ""
    for attempt, delay in enumerate((0.0, *_STABLE_COPY_RETRY_DELAYS_SEC)):
        if delay:
            time.sleep(delay)
        fd, tmp = tempfile.mkstemp(prefix=".pptdoctor-snapshot-", suffix=".pptx")
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
        try:
            os.unlink(ext_path(prepared))
        except OSError:
            pass


def _write_zip(dest: str, doc_id: str, names: list[str], parts: dict[str, str]) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
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
        "bytes_reclaimed": 0,
    }
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
                    _VERIFIED_OBJECT_PATHS.discard(str(path))
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
                    _VERIFIED_OBJECT_PATHS.discard(str(path))
            except OSError:
                read_errors.append(object_hash)
                _VERIFIED_OBJECT_PATHS.discard(str(path))

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
    """重组到临时文件并验证能正常解析（保真自检）。"""
    fd, tmp = tempfile.mkstemp(suffix=".pptx")
    os.close(fd)
    try:
        _write_zip(tmp, doc_id, names, parts)
        return (
            parse_pptx(tmp).status == "ok"
            and file_hash(tmp) == _package_content_hash_from_parts(parts)
        )
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


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


def rebuild_to(doc_id: str, version_id: str, dest: str) -> bool:
    """把某版本原子重组/恢复到 dest。

    始终先在目标同目录生成并验证临时文件，最后用 ``os.replace`` 一次切换。
    任一对象缺失、manifest 损坏或校验失败时，现有目标文件保持逐字节不变。
    """
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    mf = _manifest_path(doc_id, version_id)
    if not mf.exists():
        return False
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
                return False
            shutil.copy2(src, ext_path(tmp))
            if parse_pptx(tmp).status != "ok":
                return False
        elif mode == "dedup":
            _write_zip(ext_path(tmp), doc_id, m["names"], m["parts"])
            if parse_pptx(tmp).status != "ok":
                return False
        else:
            return False

        expected = manifest_content_hash(doc_id, version_id)
        if not expected or file_hash(tmp) != expected:
            return False
        os.replace(ext_path(tmp), ext_path(dest))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.unlink(ext_path(tmp))
        except OSError:
            pass
