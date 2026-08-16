"""Version-management orchestration for PPT Doctor."""
from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from .. import actions, renderer
from ..config import (
    PPTX_EXT,
    get_version_keep_per_doc,
    get_version_vault_dir,
    get_vault_max_mb,
    set_version_vault_dir,
)
from ..path_policy import explicit_project_output_roots, is_project_output_path
from ..scanner import iter_ppt_files
from ..text_tokenize import build_fts_match_exact
from . import store, vault

SESSION_GAP_SEC = 30 * 60
KEEP_PER_DOC = 100
_DIFF_SAMPLE_LIMIT = 6
_DEFAULT_RECONCILE_INTERVAL_SEC = 300.0
_DEFAULT_RECONCILE_BATCH_DOCS = 500
_DEFAULT_RECONCILE_BATCH_NEW_FILES = 120
_DEFAULT_VAULT_HEAVY_MAINTENANCE_INTERVAL_SEC = 7 * 24 * 60 * 60
_DEFAULT_GHOST_GRACE_SEC = 30 * 24 * 60 * 60
_DEFAULT_QUARANTINE_KEEP_PER_DOC = 10
_FALSE_ENV = {"0", "false", "no", "off"}


def _now() -> float:
    return datetime.datetime.now().timestamp()


def _sid(ts: float) -> str:
    return "s" + datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M")


def _is_pptx(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == PPTX_EXT


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _token_set(text: str) -> set[str]:
    return {tok for tok in str(text or "").split() if tok.strip()}


def _page_text_diff(old_pages, new_pages) -> dict:
    old = {int(r["page_no"]): str(r["content"] or "") for r in old_pages}
    new = {int(r["page_no"]): str(r["content"] or "") for r in new_pages}
    old_keys = set(old)
    new_keys = set(new)
    added_pages = sorted(new_keys - old_keys)
    removed_pages = sorted(old_keys - new_keys)
    changed_pages = []
    added_terms: list[str] = []
    removed_terms: list[str] = []
    for page in sorted(old_keys & new_keys):
        if old.get(page) == new.get(page):
            continue
        changed_pages.append(page)
        old_terms = _token_set(old.get(page, ""))
        new_terms = _token_set(new.get(page, ""))
        for tok in sorted(new_terms - old_terms):
            if tok not in added_terms:
                added_terms.append(tok)
        for tok in sorted(old_terms - new_terms):
            if tok not in removed_terms:
                removed_terms.append(tok)
    return {
        "added_pages": added_pages,
        "removed_pages": removed_pages,
        "changed_pages": changed_pages,
        "added_terms": added_terms[:_DIFF_SAMPLE_LIMIT],
        "removed_terms": removed_terms[:_DIFF_SAMPLE_LIMIT],
    }


def _diff_summary(version, previous, text_diff: dict, package_diff: dict) -> list[str]:
    lines: list[str] = []
    if previous is None:
        lines.append("首个版本，可作为恢复基线。")
    page_delta = int(version["page_count"] or 0) - (int(previous["page_count"] or 0) if previous else 0)
    if page_delta > 0:
        lines.append(f"新增 {page_delta} 页。")
    elif page_delta < 0:
        lines.append(f"减少 {abs(page_delta)} 页。")
    changed_pages = text_diff.get("changed_pages") or []
    if changed_pages:
        sample = ", ".join(f"P{p}" for p in changed_pages[:8])
        lines.append(f"文本改动 {len(changed_pages)} 页：{sample}。")
    if text_diff.get("added_pages"):
        lines.append("新增页面：" + ", ".join(f"P{p}" for p in text_diff["added_pages"][:8]) + "。")
    if text_diff.get("removed_pages"):
        lines.append("删除页面：" + ", ".join(f"P{p}" for p in text_diff["removed_pages"][:8]) + "。")
    buckets = package_diff.get("buckets") or {}
    media = buckets.get("media") or {}
    if any(media.get(k, 0) for k in ("added", "removed", "changed")):
        lines.append(
            "图片/媒体变化："
            f"+{media.get('added', 0)} -{media.get('removed', 0)} 改{media.get('changed', 0)}。"
        )
    charts = buckets.get("charts") or {}
    if any(charts.get(k, 0) for k in ("added", "removed", "changed")):
        lines.append(
            "图表变化："
            f"+{charts.get('added', 0)} -{charts.get('removed', 0)} 改{charts.get('changed', 0)}。"
        )
    if not lines:
        changed_parts = int(package_diff.get("changed_parts") or 0)
        if changed_parts:
            lines.append(f"结构/样式微调：{changed_parts} 个内部部件变化。")
        else:
            lines.append("未检测到明显文本或页面变化。")
    return lines[:6]


class VersionManager:
    def __init__(
        self,
        conn=None,
        on_snapshot=None,
        on_content_saved=None,
        *,
        index_roots: list[str] | tuple[str, ...] | None = None,
    ):
        self._db_path = vault.db_path() if conn is None else None
        self._conn = conn or store.connect(self._db_path)
        store.init_db(self._conn)
        if conn is None:
            self._read_conn = store.connect(self._db_path)
            self._read_conn.isolation_level = None
        else:
            self._read_conn = conn
        self._lock = threading.RLock()
        # Covers the complete stable-copy -> metadata/artifact commit window.
        # A vault location move must not relocate ``_tmp`` between those two
        # phases or one ordinary PowerPoint save can be lost mid-migration.
        self._vault_location_lock = threading.RLock()
        self._watcher = None
        self._index_roots = tuple(index_roots or ())
        self._explicit_output_roots = explicit_project_output_roots(self._index_roots)
        self._on_snapshot = on_snapshot
        self._on_content_saved = on_content_saved
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_interval_sec = max(
            0.0,
            _env_float("PPTUTOR_VERSION_RECONCILE_SEC", _DEFAULT_RECONCILE_INTERVAL_SEC),
        )
        self._reconcile_batch_docs = max(
            1,
            _env_int("PPTUTOR_VERSION_RECONCILE_BATCH_DOCS", _DEFAULT_RECONCILE_BATCH_DOCS),
        )
        self._reconcile_batch_new_files = max(
            0,
            _env_int("PPTUTOR_VERSION_RECONCILE_BATCH_NEW_FILES", _DEFAULT_RECONCILE_BATCH_NEW_FILES),
        )
        self._reconcile_common_dirs = (
            os.environ.get("PPTUTOR_VERSION_RECONCILE_COMMON_DIRS", "1").strip().lower()
            not in _FALSE_ENV
        )
        self._reconcile_cycles = 0
        self._reconcile_snapshots = 0
        self._reconcile_last_checked = 0
        self._reconcile_last_new_checked = 0
        self._reconcile_last_ms = 0.0
        self._reconcile_last_error = ""
        self._reconcile_last_cursor = ""
        self._snapshot_failures = 0
        self._snapshot_last_error = ""
        self._keep_per_doc = max(
            0,
            _env_int(
                "PPTUTOR_VERSION_KEEP_PER_DOC",
                get_version_keep_per_doc(KEEP_PER_DOC),
            ),
        )
        self._vault_maintenance_thread: threading.Thread | None = None
        # Serialize filesystem-moving maintenance with fsck. This lock is
        # deliberately separate from ``_lock`` so long read-only deep audits
        # never block ordinary snapshot commits.
        self._vault_maintenance_lock = threading.RLock()
        self._vault_maintenance_enabled = (
            self._db_path is not None
            and os.environ.get("PPTUTOR_VAULT_MAINTENANCE", "1").strip().lower()
            not in _FALSE_ENV
        )
        self._vault_heavy_maintenance_interval_sec = max(
            0.0,
            _env_float(
                "PPTUTOR_VAULT_HEAVY_MAINTENANCE_SEC",
                _DEFAULT_VAULT_HEAVY_MAINTENANCE_INTERVAL_SEC,
            ),
        )
        self._vault_maintenance_result: dict = {}
        self._vault_maintenance_error = ""
        # 幽灵收割宽限期：所有路径首次确认缺失满该时长才自动收割
        self._ghost_grace_sec = max(
            0.0,
            _env_float("PPTUTOR_GHOST_GRACE_SEC", _DEFAULT_GHOST_GRACE_SEC),
        )
        # 隔离（quarantined）版本每 doc 封顶：豁免不能无界堆积
        self._quarantine_keep_per_doc = max(
            0,
            _env_int("PPTUTOR_VERSION_QUARANTINE_KEEP", _DEFAULT_QUARANTINE_KEEP_PER_DOC),
        )
        self._vault_maintenance_stop = threading.Event()
        self._restore_last_error = ""

    # ---------- Snapshot identity ----------
    def snapshot_now(
        self,
        path: str,
        notify: bool = True,
        *,
        preserve_version_ids: set[str] | None = None,
        explicit_output_roots: tuple[str, ...] | list[str] | None = None,
    ) -> str | None:
        if not _is_pptx(path):
            return None
        path = os.path.abspath(path)
        allowed_outputs = (
            self._explicit_output_roots
            if explicit_output_roots is None else tuple(explicit_output_roots)
        )
        if is_project_output_path(path, explicit_output_roots=allowed_outputs):
            return None
        if not os.path.exists(path):
            return None
        try:
            with self._vault_location_lock:
                with vault.stable_snapshot_source(path) as snapshot_source:
                    content_hash = vault.file_hash(snapshot_source)
                    with self._lock:
                        doc_id, base_version, content_hash = self._snapshot_identity(
                            path,
                            content_hash=content_hash,
                        )
                        sid = self._session_id_for_doc(doc_id)
                        vid = vault.snapshot(
                            self._conn,
                            path,
                            sid,
                            doc_id=doc_id,
                            base_version=base_version,
                            content_hash=content_hash,
                            source_path=snapshot_source,
                        )
                        if vid:
                            self._enforce_quota(
                                doc_id,
                                preserve_version_ids=preserve_version_ids,
                            )
            self._snapshot_last_error = ""
        except vault.SnapshotSourceError as exc:
            self._snapshot_failures += 1
            self._snapshot_last_error = f"{type(exc).__name__}: {exc}"
            raise
        if vid and notify and self._on_snapshot is not None:
            try:
                self._on_snapshot(path, vid)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("on_snapshot callback raised", exc_info=True)
        return vid

    def move_path(self, src_path: str, dest_path: str) -> bool:
        """Bind a filesystem move/rename to the existing doc id when possible."""
        if not _is_pptx(dest_path):
            return False
        src_path = os.path.abspath(src_path)
        dest_path = os.path.abspath(dest_path)
        if is_project_output_path(
            dest_path,
            explicit_output_roots=self._explicit_output_roots,
        ):
            return False
        if os.path.exists(src_path):
            return False
        with self._lock:
            doc = store.get_doc_by_path(self._conn, src_path)
            if not doc:
                return False
            store.upsert_doc(self._conn, doc["doc_id"], dest_path, _now())
            self._conn.commit()
            return True

    def _snapshot_identity(self, path: str, content_hash: str | None = None):
        doc = store.get_doc_by_path(self._conn, path)
        if doc:
            doc_id = doc["doc_id"]
            return (
                doc_id,
                self._effective_latest_version_on_conn(self._conn, doc_id),
                content_hash,
            )

        content_hash = content_hash or vault.file_hash(path)
        candidates = self._find_versions_by_content_hash(content_hash)
        now = _now()

        for version in candidates:
            source_doc = store.get_doc(self._conn, version["doc_id"])
            if source_doc and source_doc["path"] and not os.path.exists(source_doc["path"]):
                store.upsert_doc(self._conn, version["doc_id"], path, now)
                return (
                    version["doc_id"],
                    self._effective_latest_version_on_conn(self._conn, version["doc_id"]),
                    content_hash,
                )

        for version in candidates:
            source_doc = store.get_doc(self._conn, version["doc_id"])
            if not source_doc:
                continue
            child_doc_id = vault.doc_id_for(path)
            store.upsert_doc(self._conn, child_doc_id, path, now)
            if not store.get_branch(self._conn, child_doc_id):
                store.record_branch(
                    self._conn,
                    child_doc_id,
                    version["doc_id"],
                    version["version_id"],
                    now,
                    "copy/hash_match",
                )
            return (
                child_doc_id,
                self._effective_latest_version_on_conn(self._conn, child_doc_id),
                content_hash,
            )

        doc_id = vault.doc_id_for(path)
        return doc_id, store.latest_version(self._conn, doc_id), content_hash

    def _find_versions_by_content_hash(self, content_hash: str):
        candidates = list(store.find_versions_by_content_hash(self._conn, content_hash))
        if candidates or not str(content_hash or "").startswith("pkg:"):
            return candidates
        rows = self._conn.execute("SELECT * FROM versions ORDER BY ts DESC").fetchall()
        matched = []
        for row in rows:
            if "health" in row.keys() and str(row["health"] or "ok") != "ok":
                continue
            try:
                if vault.manifest_content_hash(row["doc_id"], row["version_id"]) == content_hash:
                    matched.append(row)
            except Exception:  # noqa: BLE001
                continue
        return matched

    def _doc_id_for_path_on_conn(self, conn, path: str) -> str:
        doc = store.get_doc_by_path(conn, path)
        return doc["doc_id"] if doc else vault.doc_id_for(path)

    def _session_id_for_doc(self, doc_id: str) -> str:
        latest = store.latest_version(self._conn, doc_id)
        now = _now()
        if latest is not None and (now - latest["ts"]) < SESSION_GAP_SEC:
            return latest["session_id"] or _sid(latest["ts"])
        return _sid(now)

    @staticmethod
    def _effective_versions_on_conn(conn, doc_id: str):
        rows = list(store.list_versions(conn, doc_id))
        branch = store.get_branch(conn, doc_id)
        if branch:
            rows.extend(
                store.list_versions_through(
                    conn,
                    branch["parent_doc_id"],
                    branch["branched_from_version_id"],
                )
            )
            rows.sort(key=lambda r: (float(r["ts"] or 0), str(r["version_id"])), reverse=True)
        return rows

    @staticmethod
    def _effective_latest_version_on_conn(conn, doc_id: str):
        latest = store.latest_version(conn, doc_id)
        if latest is not None:
            return latest
        branch = store.get_branch(conn, doc_id)
        if branch:
            return store.get_version(conn, branch["branched_from_version_id"])
        return None

    # ---------- Catch-up ----------
    def catch_up_root(self, root: str) -> int:
        n = 0
        selected_output_roots = explicit_project_output_roots([root])
        for p in iter_ppt_files([root]):
            if p.suffix.lower() == PPTX_EXT and self.snapshot_now(
                str(p),
                explicit_output_roots=selected_output_roots,
            ):
                n += 1
        return n

    def reconcile_known_docs(
        self,
        *,
        limit: int | None = None,
        notify: bool = True,
        scan_new_files: bool = True,
    ) -> int:
        """Catch up when filesystem watcher misses save/create events.

        Managed docs use an mtime guard before hashing. New-file catch-up is a
        shallow, bounded scan of managed/common user directories, not a full
        disk walk.
        """
        max_docs = self._reconcile_batch_docs if limit is None else max(1, int(limit))
        with self._lock:
            cursor = store.get_meta(self._conn, "reconcile_cursor", "")
            docs = list(store.list_active_docs_after(self._conn, cursor, max_docs))
            known_paths = store.current_path_keys(self._conn)
        checked = 0
        new_checked = 0
        created = 0
        cycle_error = ""
        start = datetime.datetime.now().timestamp()
        try:
            for doc in docs:
                path = str(doc["path"] or "")
                if not path or not _is_pptx(path):
                    continue
                if is_project_output_path(
                    path,
                    explicit_output_roots=self._explicit_output_roots,
                ):
                    continue
                if not os.path.exists(path):
                    with self._lock:
                        store.set_status(
                            self._conn,
                            doc["doc_id"],
                            "deleted",
                            commit=False,
                        )
                    continue
                checked += 1
                with self._lock:
                    latest = self._effective_latest_version_on_conn(self._conn, doc["doc_id"])
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if latest is not None and mtime <= float(latest["ts"] or 0) + 0.5:
                    continue
                try:
                    if self.snapshot_now(path, notify=notify):
                        created += 1
                except vault.SnapshotSourceError as exc:
                    cycle_error = f"{type(exc).__name__}: {exc}"
                    continue
            if scan_new_files and self._reconcile_batch_new_files > 0:
                for path in self._iter_reconcile_new_file_candidates(docs, known_paths):
                    new_checked += 1
                    try:
                        if self.snapshot_now(path, notify=notify):
                            created += 1
                    except vault.SnapshotSourceError as exc:
                        cycle_error = f"{type(exc).__name__}: {exc}"
                    if new_checked >= self._reconcile_batch_new_files:
                        break
            return created
        except Exception as exc:  # noqa: BLE001
            cycle_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).warning("version reconcile failed", exc_info=True)
            return created
        finally:
            if docs:
                with self._lock:
                    self._reconcile_last_cursor = str(docs[-1]["doc_id"])
                    store.set_meta(
                        self._conn,
                        "reconcile_cursor",
                        self._reconcile_last_cursor,
                    )
                    self._conn.commit()
            elapsed = (datetime.datetime.now().timestamp() - start) * 1000.0
            self._reconcile_cycles += 1
            self._reconcile_snapshots += created
            self._reconcile_last_checked = checked
            self._reconcile_last_new_checked = new_checked
            self._reconcile_last_ms = elapsed
            self._reconcile_last_error = cycle_error

    def _reconcile_candidate_dirs(self, docs) -> list[str]:
        dirs: dict[str, None] = {}
        env_dirs = os.environ.get("PPTUTOR_VERSION_RECONCILE_DIRS", "")
        for raw in env_dirs.split(os.pathsep):
            raw = raw.strip()
            if raw:
                dirs[os.path.abspath(os.path.expanduser(raw))] = None
        if self._reconcile_common_dirs:
            home = Path.home()
            for name in ("Desktop", "Documents", "Downloads"):
                p = home / name
                if p.is_dir():
                    dirs[str(p)] = None
        for doc in docs:
            path = str(doc["path"] or "")
            if path:
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    dirs[parent] = None
        return list(dirs)

    def _iter_reconcile_new_file_candidates(self, docs, known_paths: set[str]):
        candidates: list[tuple[float, str]] = []
        for directory in self._reconcile_candidate_dirs(docs):
            try:
                with os.scandir(directory) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        if not _is_pptx(entry.path):
                            continue
                        if is_project_output_path(
                            entry.path,
                            explicit_output_roots=self._explicit_output_roots,
                        ):
                            continue
                        if os.path.basename(entry.path).startswith("~$"):
                            continue
                        key = store.path_key(entry.path)
                        if key in known_paths:
                            continue
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            continue
                        candidates.append((mtime, os.path.abspath(entry.path)))
            except OSError:
                continue
        seen: set[str] = set()
        for _mtime, path in sorted(candidates, reverse=True):
            key = store.path_key(path)
            if key in seen:
                continue
            seen.add(key)
            yield path

    # ---------- Queries ----------
    @staticmethod
    def _open_vault_connections(db_file: Path):
        """Open and validate the writer/UI-reader pair for one vault."""
        writer = store.connect(db_file)
        reader = None
        try:
            store.init_db(writer)
            reader = store.connect(db_file)
            reader.isolation_level = None
            # Force a real read before an old vault can be released.  Opening a
            # SQLite handle alone does not prove that its schema is usable.
            store.summary_stats(reader)
            return writer, reader
        except Exception:
            VersionManager._close_vault_connections(writer, reader)
            raise

    @staticmethod
    def _close_vault_connections(writer, reader) -> None:
        seen: set[int] = set()
        for conn in (reader, writer):
            if conn is None or id(conn) in seen:
                continue
            seen.add(id(conn))
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "failed to close vault connection during relocation",
                    exc_info=True,
                )

    @staticmethod
    def _vault_setting_for_destination(destination: Path, config_value: str | None) -> str:
        # Empty is the intentional config spelling for data_dir()/vault.  Any
        # user-entered non-empty/relative spelling is persisted as the resolved
        # absolute path so a future launcher cwd cannot silently redirect it.
        if config_value is not None and not str(config_value).strip():
            return ""
        return str(destination)

    def switch_vault_dir(
        self,
        destination: str | Path,
        *,
        config_value: str | None = None,
    ) -> dict:
        """Immediately switch to another vault without moving the old one.

        Both connection pairs are live-tested before the persisted setting and
        active manager are switched.  This avoids the old settings-dialog bug
        where metadata continued going to the old DB while snapshot artifacts
        followed the newly saved config directory.
        """
        if self._db_path is None:
            raise RuntimeError("注入数据库连接的测试管理器不支持切换版本库")
        destination = Path(os.path.abspath(str(destination)))
        source = Path(self._db_path).parent
        same = os.path.normcase(str(source)) == os.path.normcase(str(destination))
        desired_setting = self._vault_setting_for_destination(destination, config_value)
        if same:
            set_version_vault_dir(desired_setting)
            return {"files": 0, "bytes": 0, "source_backup": "", "switched": True}
        if vault._paths_overlap(source, destination):
            raise ValueError("当前版本库与新位置不能互相嵌套")

        destination.mkdir(parents=True, exist_ok=True)
        entries = list(destination.iterdir())
        if entries and not (destination / "versions.db").is_file():
            raise ValueError("目标目录非空且不是可识别的 PPT Doctor 版本库")

        with self._vault_maintenance_lock:
            with self._vault_location_lock:
                with self._lock:
                    new_db = destination / "versions.db"
                    new_writer, new_reader = self._open_vault_connections(new_db)
                    try:
                        set_version_vault_dir(desired_setting)
                    except Exception:
                        self._close_vault_connections(new_writer, new_reader)
                        raise
                    old_writer, old_reader = self._conn, self._read_conn
                    self._conn, self._read_conn = new_writer, new_reader
                    self._db_path = new_db
                    self._close_vault_connections(old_writer, old_reader)
        return {"files": 0, "bytes": 0, "source_backup": "", "switched": True}

    def migrate_vault_dir(
        self,
        destination: str | Path,
        progress_cb=None,
        *,
        config_value: str | None = None,
    ) -> dict:
        """Move the live vault, verify it, reconnect, then release rollback data."""
        if self._db_path is None:
            raise RuntimeError("注入数据库连接的测试管理器不支持迁移版本库")
        destination = Path(os.path.abspath(str(destination)))
        source_db = Path(self._db_path)
        source = source_db.parent
        desired_setting = self._vault_setting_for_destination(destination, config_value)
        old_setting = get_version_vault_dir()

        with self._vault_maintenance_lock:
            with self._vault_location_lock:
                with self._lock:
                    connections_closed = False

                    def _close_before_source_move() -> None:
                        nonlocal connections_closed
                        self._close_vault_connections(self._conn, self._read_conn)
                        connections_closed = True

                    try:
                        result = vault.migrate_vault_dir(
                            source,
                            destination,
                            progress_cb,
                            keep_source_backup=True,
                            before_source_move=_close_before_source_move,
                        )
                    except Exception:
                        if connections_closed:
                            self._conn, self._read_conn = self._open_vault_connections(source_db)
                        raise

                    backup_raw = str(result.get("source_backup") or "")
                    if not backup_raw:
                        # keep_source_backup=True is a hard hand-off contract:
                        # reconnect/config validation must finish before the
                        # only rollback copy can be released.
                        raise RuntimeError("迁移未返回源版本库回滚副本，拒绝切换")
                    backup = Path(backup_raw)
                    new_writer = new_reader = None
                    try:
                        new_db = destination / "versions.db"
                        new_writer, new_reader = self._open_vault_connections(new_db)
                        set_version_vault_dir(desired_setting)
                    except Exception as exc:
                        self._close_vault_connections(new_writer, new_reader)
                        rollback_errors: list[str] = []
                        try:
                            set_version_vault_dir(old_setting)
                        except Exception as config_exc:  # noqa: BLE001
                            rollback_errors.append(f"设置回滚失败：{config_exc}")
                        try:
                            if backup.is_dir() and not source.exists():
                                os.replace(backup, source)
                        except OSError as move_exc:
                            rollback_errors.append(f"源目录回滚失败：{move_exc}")
                        if source.is_dir():
                            try:
                                self._conn, self._read_conn = self._open_vault_connections(
                                    source_db
                                )
                                shutil.rmtree(destination, ignore_errors=True)
                            except Exception as reopen_exc:  # noqa: BLE001
                                rollback_errors.append(f"旧版本库重连失败：{reopen_exc}")
                        detail = "；".join(rollback_errors)
                        raise RuntimeError(
                            f"新版本库重连失败，已尝试回滚：{exc}"
                            + (f"（{detail}）" if detail else "")
                        ) from exc

                    self._conn, self._read_conn = new_writer, new_reader
                    self._db_path = destination / "versions.db"
                    retained = str(backup)
                    try:
                        shutil.rmtree(backup)
                        retained = ""
                    except OSError:
                        logging.getLogger(__name__).warning(
                            "vault migration succeeded; rollback backup retained: %s",
                            backup,
                        )
                    result["source_backup"] = retained
                    return result

    def list_docs(self):
        with self._lock:
            return store.list_docs(self._read_conn)

    def summary_stats(self) -> dict[str, int]:
        """Thread-safe KPI snapshot for dashboards and status surfaces."""
        with self._lock:
            if self._db_path is None:
                return store.summary_stats(self._conn)
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                return store.summary_stats(conn)
            finally:
                conn.close()

    def preview_ghost_cleanup(self) -> dict:
        """Return the current manual-cleanup impact without racing a snapshot."""
        with self._vault_maintenance_lock:
            with self._vault_location_lock:
                with self._lock:
                    return vault.reap_ghost_docs(self._conn, dry_run=True)

    def reap_ghost_docs_now(self) -> dict:
        """Manually remove expired/missing documents under all vault locks.

        GC must not inspect the object pool between a snapshot writing its
        objects and committing the referencing DB row.  The old settings-page
        implementation used an independent SQLite connection and could delete
        those in-flight objects as apparent orphans.
        """
        with self._vault_maintenance_lock:
            with self._vault_location_lock:
                with self._lock:
                    return vault.reap_ghost_docs(self._conn, dry_run=False)

    def list_docs_details(self) -> list[dict]:
        with self._lock:
            if self._db_path is None:
                return list(store.list_docs(self._conn))
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                return list(store.list_docs(conn))
            finally:
                conn.close()

    def get_doc(self, doc_id: str):
        with self._lock:
            return store.get_doc(self._read_conn, doc_id)

    def get_version(self, version_id: str):
        with self._lock:
            return store.get_version(self._read_conn, version_id)

    def list_versions(self, path: str):
        with self._lock:
            doc_id = self._doc_id_for_path_on_conn(self._read_conn, path)
            return self._effective_versions_on_conn(self._read_conn, doc_id)

    def list_versions_details(self, path: str, limit: int | None = None) -> list[dict]:
        with self._lock:
            if self._db_path is None:
                doc_id = self._doc_id_for_path_on_conn(self._conn, path)
                return self._list_versions_by_doc_details_on_conn(self._conn, doc_id, limit)
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                doc_id = self._doc_id_for_path_on_conn(conn, path)
                return self._list_versions_by_doc_details_on_conn(conn, doc_id, limit)
            finally:
                conn.close()

    def list_versions_by_doc(self, doc_id: str):
        with self._lock:
            return self._effective_versions_on_conn(self._read_conn, doc_id)

    def list_versions_by_doc_details(self, doc_id: str, limit: int | None = None) -> list[dict]:
        with self._lock:
            if self._db_path is None:
                return self._list_versions_by_doc_details_on_conn(self._conn, doc_id, limit)
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                return self._list_versions_by_doc_details_on_conn(conn, doc_id, limit)
            finally:
                conn.close()

    @classmethod
    def _list_versions_by_doc_details_on_conn(cls, conn, doc_id: str, limit: int | None) -> list[dict]:
        rows = cls._effective_versions_on_conn(conn, doc_id)
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        return [
            {
                "version_id": r["version_id"],
                "doc_id": r["doc_id"],
                "ts": r["ts"],
                "page_count": r["page_count"],
                "changed": r["changed"],
                "thumb_path": r["thumb_path"],
                "session_id": (r["session_id"] if "session_id" in r.keys() else ""),
                "health": (r["health"] if "health" in r.keys() else "ok"),
                "health_error": (
                    r["health_error"] if "health_error" in r.keys() else ""
                ),
                "inherited": r["doc_id"] != doc_id,
            }
            for r in rows
        ]

    def ensure_version_preview(self, version_id: str, page_no: int = 1, long_edge: int = 360) -> str | None:
        """Render and cache a small PNG preview for one historical version."""
        with self._vault_location_lock:
            return self._ensure_version_preview_current_vault(version_id, page_no, long_edge)

    def _ensure_version_preview_current_vault(
        self,
        version_id: str,
        page_no: int,
        long_edge: int,
    ) -> str | None:
        with self._lock:
            version = store.get_version(self._conn, version_id)
            if not version:
                return None
            cached = str(version["thumb_path"] or "")
            if cached and os.path.exists(cached):
                return cached
            doc_id = version["doc_id"]

        # 预览重组暂存随版本库 _tmp（启动清扫覆盖），不再落裸 %TEMP%。
        tmp_root = vault.vault_dir() / "_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".pptdoctor-preview-", suffix=".pptx", dir=str(tmp_root)
        )
        os.close(fd)
        try:
            if not vault.rebuild_to(
                doc_id,
                version_id,
                tmp,
                expected_content_hash=str(version["content_hash"] or ""),
            ):
                return None
            page = max(1, int(page_no))
            png = renderer.render_page_once(
                tmp,
                page,
                cache_key=f"version-{version_id}-p{page}",
                long_edge=long_edge,
                hi_priority=False,
            )
            if not png or not os.path.exists(png):
                return None
            out = str(png)
            with self._lock:
                store.set_version_thumb_path(self._conn, version_id, out)
                self._conn.commit()
            return out
        finally:
            vault._unlink_snapshot_tmp(tmp)

    def describe_version_diff(self, version_id: str) -> dict:
        with self._vault_location_lock:
            if self._db_path is None:
                with self._lock:
                    return self._describe_version_diff_on_conn(self._conn, version_id)
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                return self._describe_version_diff_on_conn(conn, version_id)
            finally:
                conn.close()

    @staticmethod
    def _describe_version_diff_on_conn(conn, version_id: str) -> dict:
        version = store.get_version(conn, version_id)
        if not version:
            return {"version_id": version_id, "ok": False, "lines": ["版本不存在或已被清理。"]}
        previous = store.previous_version(
            conn,
            version["doc_id"],
            float(version["ts"] or 0),
            version["version_id"],
        )
        old_pages = store.version_pages(conn, previous["version_id"]) if previous is not None else []
        new_pages = store.version_pages(conn, version_id)
        text_diff = _page_text_diff(old_pages, new_pages)
        package_diff = vault.manifest_diff(
            version["doc_id"],
            previous["version_id"] if previous is not None else None,
            version_id,
        )
        lines = _diff_summary(version, previous, text_diff, package_diff)
        return {
            "version_id": version_id,
            "ok": True,
            "previous_version_id": previous["version_id"] if previous is not None else "",
            "page_count": int(version["page_count"] or 0),
            "previous_page_count": int(previous["page_count"] or 0) if previous is not None else 0,
            "changed": str(version["changed"] or ""),
            "text": text_diff,
            "package": package_diff,
            "lines": lines,
        }

    # ---------- Restore / export ----------
    def last_restore_error(self) -> str:
        """上一次恢复/导出失败的原因（vault.REBUILD_ERR_*），成功时为空串。

        UI 拿它把「恢复失败」换成可执行的下一步——最常见的失败是用户正开着
        PowerPoint 编辑这份稿，此时目标文件被独占，而原文件其实毫发无损。
        """
        return self._restore_last_error

    def restore_to(self, path: str, version_id: str, dest: str | None = None) -> bool:
        self._restore_last_error = ""
        with self._vault_location_lock:
            return self._restore_to_current_vault(path, version_id, dest)

    def _restore_to_current_vault(
        self,
        path: str,
        version_id: str,
        dest: str | None,
    ) -> bool:
        with self._lock:
            version = store.get_version(self._conn, version_id)
            if not version:
                self._restore_last_error = vault.REBUILD_ERR_MISSING
                return False
            if "health" in version.keys() and str(version["health"] or "ok") != "ok":
                self._restore_last_error = vault.REBUILD_ERR_CORRUPT
                return False
            owner_doc_id = str(version["doc_id"])
            expected_hash = str(version["content_hash"] or "")
        target = dest or path
        same_target = os.path.normcase(os.path.abspath(target)) == os.path.normcase(
            os.path.abspath(path)
        )
        if same_target and os.path.exists(path):
            if actions.presentation_open_state(path) is not False:
                self._restore_last_error = vault.REBUILD_ERR_LOCKED
                return False
        if same_target and os.path.exists(path):
            # Do not hold _lock while entering snapshot_now: snapshot's global
            # order is location-lock -> DB-lock so live vault migration cannot
            # deadlock against restore's pre-overwrite safety copy.
            try:
                self.snapshot_now(
                    path,
                    notify=False,
                    preserve_version_ids={version_id},
                )
            except vault.InvalidSnapshotError:
                # The main reason to restore can be that the current PPTX is
                # already structurally broken.  Requiring that broken file to
                # become a healthy recovery point makes recovery impossible.
                logging.getLogger(__name__).warning(
                    "current file is invalid; restoring healthy version without pre-snapshot: %s",
                    path,
                )
        return vault.rebuild_to(
            owner_doc_id,
            version_id,
            target,
            on_error=self._note_restore_error,
            expected_content_hash=expected_hash,
            before_replace=lambda: actions.presentation_open_state(target) is False,
        )

    def _note_restore_error(self, reason: str) -> None:
        self._restore_last_error = str(reason or "")

    def export(self, path: str, version_id: str, dest: str) -> bool:
        self._restore_last_error = ""
        with self._vault_location_lock:
            return self._export_from_current_vault(path, version_id, dest)

    def _export_from_current_vault(self, path: str, version_id: str, dest: str) -> bool:
        with self._lock:
            version = store.get_version(self._conn, version_id)
            if not version:
                self._restore_last_error = vault.REBUILD_ERR_MISSING
                return False
            if "health" in version.keys() and str(version["health"] or "ok") != "ok":
                self._restore_last_error = vault.REBUILD_ERR_CORRUPT
                return False
            owner_doc_id = version["doc_id"]
            expected_hash = str(version["content_hash"] or "")
        return vault.rebuild_to(
            owner_doc_id,
            version_id,
            dest,
            on_error=self._note_restore_error,
            expected_content_hash=expected_hash,
            before_replace=lambda: (
                not os.path.exists(dest)
                or actions.presentation_open_state(dest) is False
            ),
        )

    # ---------- Cross-version search ----------
    def search_history(self, query: str):
        with self._lock:
            return store.search_versions(self._conn, build_fts_match_exact(query))

    def search_history_details(self, query: str, limit: int = 200) -> dict:
        match = build_fts_match_exact(query)
        if not match:
            return {"query": query, "total": 0, "rows": []}
        with self._lock:
            if self._db_path is None:
                return self._search_history_details_on_conn(self._conn, query, match, limit)
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                return self._search_history_details_on_conn(conn, query, match, limit)
            finally:
                conn.close()

    @staticmethod
    def _search_history_details_on_conn(conn, query: str, match: str, limit: int) -> dict:
        try:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM version_pages_fts WHERE version_pages_fts MATCH ?",
                (match,),
            ).fetchone()[0])
            rows = conn.execute(
                """
                SELECT f.doc_id, f.version_id, f.page_no, d.path AS doc_path,
                       v.ts AS ts, v.health AS health, v.health_error AS health_error
                FROM version_pages_fts AS f
                LEFT JOIN managed_docs AS d ON d.doc_id = f.doc_id
                LEFT JOIN versions AS v ON v.version_id = f.version_id
                WHERE version_pages_fts MATCH ?
                ORDER BY v.ts DESC, f.rowid DESC
                LIMIT ?
                """,
                (match, int(limit)),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return {"query": query, "total": 0, "rows": []}
        return {
            "query": query,
            "total": total,
            "rows": [
                {
                    "doc_id": r["doc_id"],
                    "version_id": r["version_id"],
                    "page_no": r["page_no"],
                    "doc_path": r["doc_path"],
                    "ts": r["ts"] or 0,
                    "health": r["health"] or "ok",
                    "health_error": r["health_error"] or "",
                }
                for r in rows
                if r["doc_path"]
            ],
        }

    # ---------- Deleted-file recovery ----------
    def scan_deleted(self) -> int:
        """双向对账：active→deleted 标记缺失；deleted→active 复活未观测到的恢复。

        反向 pass 的必要性：恢复若发生在应用关闭期间（或目录不在对账候选），
        deleted_at 宽限锚点不会被 upsert/set_status('active') 清零；不复活的话
        再次删除会继承旧锚点，幽灵收割的 30 天宽限被直接跳过（宁可保守也不许
        误删）。任一登记路径（含历史 alias）仍存在即视为恢复，复活顺带清零锚点。
        返回状态发生翻转的文档数（两个方向合计）。
        """
        with self._lock:
            n = 0
            for doc in store.list_docs(self._conn):
                if doc["status"] == "active":
                    if not os.path.exists(doc["path"]):
                        store.set_status(self._conn, doc["doc_id"], "deleted")
                        n += 1
                elif doc["status"] == "deleted":
                    candidates = [doc["path"], *store.list_doc_paths(self._conn, doc["doc_id"])]
                    if any(os.path.exists(p) for p in candidates):
                        store.set_status(self._conn, doc["doc_id"], "active")
                        n += 1
            return n

    def mark_deleted(self, path: str) -> bool:
        """Mark a watched PPTX as deleted immediately after its delete event."""
        with self._lock:
            doc = store.get_doc_by_path(self._conn, os.path.abspath(path))
            if not doc:
                return False
            store.set_status(self._conn, doc["doc_id"], "deleted")
            return True

    def recover(self, doc_id: str, dest: str | None = None) -> bool:
        self._restore_last_error = ""
        with self._lock:
            doc = store.get_doc(self._conn, doc_id)
            latest = next(
                (
                    version
                    for version in self._effective_versions_on_conn(self._conn, doc_id)
                    if "health" not in version.keys()
                    or str(version["health"] or "ok") == "ok"
                ),
                None,
            )
            if not doc or not latest:
                self._restore_last_error = vault.REBUILD_ERR_MISSING
                return False
            ok = vault.rebuild_to(
                latest["doc_id"],
                latest["version_id"],
                dest or doc["path"],
                on_error=self._note_restore_error,
                expected_content_hash=str(latest["content_hash"] or ""),
                before_replace=lambda: (
                    not os.path.exists(dest or str(doc["path"]))
                    or actions.presentation_open_state(dest or str(doc["path"])) is False
                ),
            )
            same_target = dest is None or os.path.normcase(os.path.abspath(dest)) == os.path.normcase(
                os.path.abspath(str(doc["path"]))
            )
            if ok and same_target:
                store.set_status(self._conn, doc_id, "active")
            return ok

    # ---------- Quota ----------
    def set_retention_limit(self, limit: int) -> None:
        with self._lock:
            self._keep_per_doc = max(0, int(limit))

    def _enforce_quota(
        self,
        doc_id: str,
        *,
        preserve_version_ids: set[str] | None = None,
    ) -> None:
        vers = store.list_versions(self._conn, doc_id)
        branch_bases = {
            str(row["branched_from_version_id"])
            for row in self._conn.execute(
                "SELECT branched_from_version_id FROM doc_branches"
            ).fetchall()
            if row["branched_from_version_id"]
        }
        if self._purge_quarantined_overflow(doc_id, vers, branch_bases):
            vers = store.list_versions(self._conn, doc_id)
        quarantined = {
            str(version["version_id"])
            for version in vers
            if "health" in version.keys()
            and str(version["health"] or "ok") != "ok"
        }
        keep_limit = self._keep_per_doc
        if keep_limit <= 0 or len(vers) <= keep_limit:
            return
        healthy_versions = [
            version
            for version in vers
            if str(version["version_id"]) not in quarantined
        ]
        session_rows: dict[str, list] = {}
        session_order: list[str] = []
        for version in healthy_versions:
            key = str(version["session_id"] or version["version_id"])
            if key not in session_rows:
                session_rows[key] = []
                session_order.append(key)
            session_rows[key].append(version)

        # First preserve one milestone per editing session, then spend the
        # remaining budget on dense detail from the five newest sessions.
        # This prevents one save-heavy afternoon from erasing months of useful
        # rollback history. Branch bases and quarantined snapshots stay outside
        # the healthy quota: the former are recovery roots; the latter may be
        # repairable after a storage issue, and are capped separately per doc
        # (see _purge_quarantined_overflow) so the exemption cannot grow unbounded.
        candidates: list = [session_rows[key][0] for key in session_order]
        candidates.extend(
            version
            for key in session_order[:5]
            for version in session_rows[key]
        )
        seen_candidates = {str(v["version_id"]) for v in candidates}
        candidates.extend(
            v
            for v in healthy_versions
            if str(v["version_id"]) not in seen_candidates
        )
        explicit_preserves = {
            str(version_id) for version_id in (preserve_version_ids or ())
        }
        exempt_ids = set(branch_bases) | quarantined | explicit_preserves
        keep_ids = set(exempt_ids)
        for version in candidates:
            if len(keep_ids - exempt_ids) >= keep_limit:
                break
            keep_ids.add(str(version["version_id"]))

        evictions: list[tuple[str, str]] = []
        for v in vers:
            # A copied document may inherit history through this exact parent
            # version. It is a live recovery root, not quota garbage.
            if str(v["version_id"]) in keep_ids:
                continue
            thumb_path = str(v["thumb_path"] or "")
            store.delete_version(self._conn, v["version_id"])
            evictions.append((str(v["version_id"]), thumb_path))
        if not evictions:
            return
        # Metadata-first deletion is crash-safe: after this commit an abrupt
        # exit can leave only unreachable artifacts, never a live row pointing
        # at a manifest that was already removed.
        self._conn.commit()
        for version_id, thumb_path in evictions:
            vault.delete_version_artifacts(doc_id, version_id)
            if thumb_path:
                try:
                    Path(thumb_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _purge_quarantined_overflow(self, doc_id: str, vers, branch_bases: set[str]) -> int:
        """隔离版本每 doc 封顶：只留最新若干个，超出的 purge（分支基继续豁免）。

        豁免是为了「可能可修复」，但无界豁免会让坏恢复点随时间无限堆积。
        vers 为 store.list_versions 结果（ts DESC，前 N 个即最新）。返回清除数。
        """
        limit = self._quarantine_keep_per_doc
        if limit <= 0:
            return 0
        quarantined_versions = [
            version
            for version in vers
            if "health" in version.keys()
            and str(version["health"] or "ok") != "ok"
            and str(version["version_id"]) not in branch_bases
        ]
        overflow = quarantined_versions[limit:]
        evictions: list[tuple[str, str]] = []
        for v in overflow:
            thumb_path = str(v["thumb_path"] or "")
            store.delete_version(self._conn, v["version_id"])
            evictions.append((str(v["version_id"]), thumb_path))
        if evictions:
            self._conn.commit()
        for version_id, thumb_path in evictions:
            vault.delete_version_artifacts(doc_id, version_id)
            if thumb_path:
                try:
                    Path(thumb_path).unlink(missing_ok=True)
                except OSError:
                    pass
        return len(overflow)

    # ---------- Watcher lifecycle ----------
    def start(self, *, watch: bool = True) -> None:
        self.scan_deleted()
        if watch:
            self._start_watcher()
        self._start_reconcile_loop()
        self._start_vault_maintenance()

    def _start_vault_maintenance(self) -> None:
        if not self._vault_maintenance_enabled:
            return
        thread = self._vault_maintenance_thread
        if thread is not None and thread.is_alive():
            return
        stop_event = threading.Event()
        self._vault_maintenance_stop = stop_event
        self._vault_maintenance_thread = threading.Thread(
            target=self._vault_maintenance_loop,
            args=(stop_event,),
            name="PPTDoctorVaultMaintenance",
            daemon=True,
        )
        self._vault_maintenance_thread.start()

    def _vault_maintenance_loop(self, stop_event=None) -> None:
        # 托盘常驻数周重维护也得能跑到：启动即跑一次，之后按重维护节流间隔
        # 周期触发；间隔内的重活仍由 _run_vault_maintenance_serialized 的账本节流。
        event = stop_event or self._vault_maintenance_stop
        self.run_vault_maintenance()
        interval = self._vault_heavy_maintenance_interval_sec
        while interval > 0 and not event.wait(interval):
            self.run_vault_maintenance()

    def _stop_vault_maintenance(self) -> None:
        event = self._vault_maintenance_stop
        event.set()
        thread = self._vault_maintenance_thread
        self._vault_maintenance_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def run_vault_maintenance(self) -> dict:
        with self._vault_maintenance_lock:
            return self._run_vault_maintenance_serialized()

    def _run_vault_maintenance_serialized(self) -> dict:
        """Run cheap integrity checks every start and throttle heavy GC weekly."""
        try:
            with self._lock:
                last_success_raw = store.get_meta(
                    self._conn,
                    "vault_heavy_maintenance_last_success",
                    "0",
                )
            try:
                last_success = float(last_success_raw or 0)
            except (TypeError, ValueError):
                last_success = 0.0
            now = _now()
            heavy_due = (
                self._vault_heavy_maintenance_interval_sec <= 0
                or now - last_success >= self._vault_heavy_maintenance_interval_sec
            )
            migration = (
                vault.migrate_legacy_objects()
                if heavy_due
                else {
                    "skipped": True,
                    "reason": "interval",
                    "last_success": last_success,
                }
            )
            with self._lock:
                hash_backfill = vault.backfill_content_hashes(self._conn)

            # The integrity walk is read-only and can take noticeable time on
            # large vaults. Run it on a separate snapshot connection so normal
            # save events are never blocked behind diagnostics.
            audit = self.audit_repository(deep=False)

            # 锁外先把两件慢的只读活干完（原本它们都在锁内，生产库上合计约 10 秒，
            # 期间 watcher 的留底快照全部排队）：
            #   1) 幽灵探测要对上千条登记路径做 os.path.exists；而且此前 mark 与
            #      reap 各自跑了一遍，等于白探两次。这里只探一次，分给两边用。
            #   2) 版本库体积是整目录遍历（真实 3.4GB 库约 2 秒）。
            # 探测结果可能在进锁前变旧：reap 在真正删除前会逐个复核路径是否复活。
            ghost_probe = None
            measured_bytes = None
            if heavy_due:
                probe_conn = None
                try:
                    probe_conn = store.connect(self._db_path)
                    ghost_probe = vault.list_ghost_docs(probe_conn)
                except Exception:  # noqa: BLE001 探测失败就退回锁内自己探
                    ghost_probe = None
                finally:
                    if probe_conn is not None:
                        probe_conn.close()
                try:
                    measured_bytes = vault.budget_relevant_bytes()
                except OSError:
                    measured_bytes = None

            with self._lock:
                if heavy_due:
                    # 幽灵收割带宽限期：先补记本轮新观察到的缺失，再收割已到期的。
                    ghosts_marked = vault.mark_ghost_docs_seen(
                        self._conn, ghosts=ghost_probe
                    )
                    ghosts = vault.reap_ghost_docs(
                        self._conn,
                        dry_run=False,
                        min_missing_sec=self._ghost_grace_sec,
                        # 复用同一份探测：其中 missing_since 是「本轮 mark 之前」的
                        # 值，本轮刚补记的文档在这里仍是 0 → 被宽限过滤挡下，
                        # 与旧实现（mark 之后再探一次）的结论一致，且更保守。
                        ghosts=ghost_probe,
                    )
                    # 容量上限：超了才按从老到新驱逐健康版本（分支基/隔离豁免）。
                    budget = vault.enforce_size_budget(
                        self._conn,
                        max_bytes=int(get_vault_max_mb()) * 1024 * 1024,
                        measured_bytes=measured_bytes,
                    )
                    # GC performs its own structural safety gate under the
                    # manager lock. Quarantined legacy full snapshots do not
                    # make unrelated live objects unsafe to collect.
                    garbage = vault.collect_garbage(self._conn, dry_run=False)
                    # 删行之后回收库文件本身：FTS 合并 + 条件 VACUUM + WAL 截断。
                    db_hygiene = vault.maintain_db(self._conn)
                else:
                    ghosts_marked = 0
                    ghosts = {
                        "skipped": True,
                        "reason": "interval",
                        "last_success": last_success,
                    }
                    budget = dict(ghosts)
                    garbage = dict(ghosts)
                    db_hygiene = dict(ghosts)

                def _step_clean(step: dict) -> bool:
                    return (
                        not bool(step.get("aborted", False))
                        and int(step.get("errors", 0) or 0) == 0
                    )

                heavy_ok = (
                    heavy_due
                    and _step_clean(migration)
                    and _step_clean(hash_backfill)
                    and _step_clean(garbage)
                    and _step_clean(ghosts.get("gc") or {})
                    and _step_clean(budget.get("gc") or {})
                    and not str(db_hygiene.get("error") or "")
                )
                if heavy_ok:
                    store.set_meta(
                        self._conn,
                        "vault_heavy_maintenance_last_success",
                        str(now),
                    )
                    self._conn.commit()
            result = {
                "migration": migration,
                "hash_backfill": hash_backfill,
                "audit": audit,
                "ghosts_marked": ghosts_marked,
                "ghosts": ghosts,
                "budget": budget,
                "garbage": garbage,
                "db_hygiene": db_hygiene,
                "heavy_due": heavy_due,
            }
            self._vault_maintenance_result = result
            self._vault_maintenance_error = ""
            return result
        except Exception as exc:  # noqa: BLE001
            self._vault_maintenance_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).warning("vault maintenance failed", exc_info=True)
            return {"migration": {}, "garbage": {"aborted": True}}

    def _audit_repository_locked(self, *, deep: bool) -> dict:
        result = vault.audit_repository(self._conn, deep=deep)
        self._persist_audit_health_locked(result)
        return result

    def _persist_audit_health_locked(self, result: dict) -> None:
        if result.get("deep", False):
            self._conn.execute(
                "UPDATE versions SET health='ok', health_error='' WHERE health<>'ok'"
            )
        else:
            # A quick manifest check cannot disprove byte corruption found by
            # a prior deep hash pass. Preserve those quarantines until another
            # deep pass verifies the object pool.
            self._conn.execute(
                """UPDATE versions SET health='ok', health_error=''
                   WHERE health<>'ok' AND health_error NOT LIKE 'deep:%'"""
            )
        for version_id, error in (result.get("invalid_versions") or {}).items():
            store.set_version_health(
                self._conn,
                str(version_id),
                "invalid",
                str(error),
            )
        self._conn.commit()
        quarantined = int(self._conn.execute(
            "SELECT COUNT(*) FROM versions WHERE health<>'ok'"
        ).fetchone()[0])
        result["quarantined_versions"] = quarantined
        result["ok"] = bool(result.get("ok", False)) and quarantined == 0

    def audit_repository(self, *, deep: bool = False) -> dict:
        with self._vault_maintenance_lock:
            return self._audit_repository_serialized(deep=deep)

    def _audit_repository_serialized(self, *, deep: bool = False) -> dict:
        """Run a user-requested quick or deep vault integrity check."""
        if self._db_path is None:
            with self._lock:
                result = self._audit_repository_locked(deep=deep)
        else:
            conn = store.connect(self._db_path)
            try:
                conn.isolation_level = None
                result = vault.audit_repository(conn, deep=deep)
            finally:
                conn.close()
            with self._lock:
                self._persist_audit_health_locked(result)
        with self._lock:
            current = dict(self._vault_maintenance_result)
            current["audit"] = result
            self._vault_maintenance_result = current
        return result

    def _start_watcher(self) -> None:
        self._stop_watcher()
        from .watcher import VaultWatcher, default_watch_paths
        self._watcher = VaultWatcher(
            list(self._index_roots) or default_watch_paths(),
            self.snapshot_now,
            self.move_path,
            self._on_content_saved,
            self.mark_deleted,
        )
        self._watcher.start()

    def _start_reconcile_loop(self) -> None:
        self._stop_reconcile_loop()
        if self._reconcile_interval_sec <= 0:
            return
        stop_event = threading.Event()
        self._reconcile_stop = stop_event
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            args=(stop_event,),
            name="PPTDoctorVersionReconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def _reconcile_loop(self, stop_event=None) -> None:
        # 启动即补一次离线期间漏拍；旧实现先睡 5 分钟，首屏看似受保护但
        # 刚开机的保存记录仍处于盲区。
        event = stop_event or self._reconcile_stop
        self.reconcile_known_docs()
        while not event.wait(self._reconcile_interval_sec):
            self.reconcile_known_docs()

    def _stop_reconcile_loop(self) -> None:
        self._reconcile_stop.set()
        thread = self._reconcile_thread
        self._reconcile_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def stop(self) -> None:
        self._stop_reconcile_loop()
        self._stop_watcher()
        self._stop_vault_maintenance()

    def diagnostic_lines(self) -> list[str]:
        alive = self._reconcile_thread is not None and self._reconcile_thread.is_alive()
        maintenance_alive = (
            self._vault_maintenance_thread is not None
            and self._vault_maintenance_thread.is_alive()
        )
        migration = self._vault_maintenance_result.get("migration") or {}
        hash_backfill = self._vault_maintenance_result.get("hash_backfill") or {}
        audit = self._vault_maintenance_result.get("audit") or {}
        garbage = self._vault_maintenance_result.get("garbage") or {}
        return [
            "version_reconcile: "
            f"enabled={self._reconcile_interval_sec > 0} "
            f"alive={alive} interval={self._reconcile_interval_sec:.0f}s "
            f"batch={self._reconcile_batch_docs} new_batch={self._reconcile_batch_new_files} "
            f"common_dirs={self._reconcile_common_dirs} "
            f"cycles={self._reconcile_cycles} "
            f"snapshots={self._reconcile_snapshots} "
            f"last_checked={self._reconcile_last_checked} "
            f"last_new_checked={self._reconcile_last_new_checked} "
            f"last_ms={self._reconcile_last_ms:.0f} "
            f"cursor={self._reconcile_last_cursor or '-'} "
            f"error={self._reconcile_last_error or '-'}",
            "version_snapshots: "
            f"failures={self._snapshot_failures} "
            f"last_error={self._snapshot_last_error or '-'}",
            f"version_retention: keep_per_doc={self._keep_per_doc or 'unlimited'}",
            "vault_fsck: "
            f"ok={bool(audit.get('ok', False))} "
            f"deep={bool(audit.get('deep', False))} "
            f"versions={int(audit.get('versions_checked', 0) or 0)} "
            f"invalid={int(audit.get('quarantined_versions', audit.get('invalid_count', 0)) or 0)} "
            f"missing={int(audit.get('missing_objects', 0) or 0)} "
            f"hash_errors={int(audit.get('hash_errors', 0) or 0)}",
            "vault_hashes: "
            f"updated={int(hash_backfill.get('updated', 0) or 0)} "
            f"errors={int(hash_backfill.get('errors', 0) or 0)}",
            "vault_maintenance: "
            f"enabled={self._vault_maintenance_enabled} alive={maintenance_alive} "
            f"heavy_due={bool(self._vault_maintenance_result.get('heavy_due', False))} "
            f"migrated={int(migration.get('migrated', 0) or 0)} "
            f"duplicates={int(migration.get('duplicates', 0) or 0)} "
            f"migration_errors={int(migration.get('errors', 0) or 0)} "
            f"gc_aborted={bool(garbage.get('aborted', False))} "
            f"gc_skipped={bool(garbage.get('skipped', False))} "
            f"gc_objects={int(garbage.get('objects_removed', 0) or 0)} "
            f"gc_temp_objects={int(garbage.get('temp_objects_removed', 0) or 0)} "
            f"ghosts_marked={int(self._vault_maintenance_result.get('ghosts_marked', 0) or 0)} "
            f"ghosts_reaped={int((self._vault_maintenance_result.get('ghosts') or {}).get('ghost_docs', 0) or 0)} "
            f"budget_evicted={int((self._vault_maintenance_result.get('budget') or {}).get('evicted_versions', 0) or 0)} "
            f"db_vacuumed={bool((self._vault_maintenance_result.get('db_hygiene') or {}).get('vacuumed', False))} "
            f"error={self._vault_maintenance_error or '-'}",
        ]
