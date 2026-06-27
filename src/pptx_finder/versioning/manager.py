"""Version-management orchestration for PPT Doctor."""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
import threading
from pathlib import Path

from .. import renderer
from ..config import PPTX_EXT
from ..scanner import iter_ppt_files
from ..text_tokenize import build_fts_match_exact
from . import store, vault

SESSION_GAP_SEC = 30 * 60
KEEP_PER_DOC = 50
_DIFF_SAMPLE_LIMIT = 6
_DEFAULT_RECONCILE_INTERVAL_SEC = 300.0
_DEFAULT_RECONCILE_BATCH_DOCS = 200
_DEFAULT_RECONCILE_BATCH_NEW_FILES = 120
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
    def __init__(self, conn=None, on_snapshot=None):
        self._db_path = vault.db_path() if conn is None else None
        self._conn = conn or store.connect(self._db_path)
        store.init_db(self._conn)
        if conn is None:
            self._read_conn = store.connect(self._db_path)
            self._read_conn.isolation_level = None
        else:
            self._read_conn = conn
        self._lock = threading.RLock()
        self._watcher = None
        self._on_snapshot = on_snapshot
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

    # ---------- Snapshot identity ----------
    def snapshot_now(self, path: str, notify: bool = True) -> str | None:
        if not _is_pptx(path):
            return None
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return None
        with self._lock:
            doc_id, base_version, content_hash = self._snapshot_identity(path)
            sid = self._session_id_for_doc(doc_id)
            vid = vault.snapshot(
                self._conn,
                path,
                sid,
                doc_id=doc_id,
                base_version=base_version,
                content_hash=content_hash,
            )
            if vid:
                self._enforce_quota(doc_id)
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
        if os.path.exists(src_path):
            return False
        with self._lock:
            doc = store.get_doc_by_path(self._conn, src_path)
            if not doc:
                return False
            store.upsert_doc(self._conn, doc["doc_id"], dest_path, _now())
            self._conn.commit()
            return True

    def _snapshot_identity(self, path: str):
        doc = store.get_doc_by_path(self._conn, path)
        if doc:
            doc_id = doc["doc_id"]
            return doc_id, self._effective_latest_version_on_conn(self._conn, doc_id), None

        content_hash = vault.file_hash(path)
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
        for p in iter_ppt_files([root]):
            if p.suffix.lower() == PPTX_EXT and self.snapshot_now(str(p)):
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
            docs = list(store.list_docs(self._conn))[:max_docs]
            known_paths = {
                store.path_key(str(doc["path"] or ""))
                for doc in docs
                if str(doc["path"] or "")
            }
        checked = 0
        new_checked = 0
        created = 0
        start = datetime.datetime.now().timestamp()
        try:
            for doc in docs:
                path = str(doc["path"] or "")
                if not path or not _is_pptx(path):
                    continue
                if not os.path.exists(path):
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
                if self.snapshot_now(path, notify=notify):
                    created += 1
            if scan_new_files and self._reconcile_batch_new_files > 0:
                for path in self._iter_reconcile_new_file_candidates(docs, known_paths):
                    new_checked += 1
                    if self.snapshot_now(path, notify=notify):
                        created += 1
                    if new_checked >= self._reconcile_batch_new_files:
                        break
            return created
        except Exception as exc:  # noqa: BLE001
            self._reconcile_last_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).warning("version reconcile failed", exc_info=True)
            return created
        finally:
            elapsed = (datetime.datetime.now().timestamp() - start) * 1000.0
            self._reconcile_cycles += 1
            self._reconcile_snapshots += created
            self._reconcile_last_checked = checked
            self._reconcile_last_new_checked = new_checked
            self._reconcile_last_ms = elapsed

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
    def list_docs(self):
        return store.list_docs(self._read_conn)

    def list_docs_details(self) -> list[dict]:
        if self._db_path is None:
            with self._lock:
                return list(store.list_docs(self._conn))
        conn = store.connect(self._db_path)
        try:
            conn.isolation_level = None
            return list(store.list_docs(conn))
        finally:
            conn.close()

    def get_doc(self, doc_id: str):
        return store.get_doc(self._read_conn, doc_id)

    def get_version(self, version_id: str):
        return store.get_version(self._read_conn, version_id)

    def list_versions(self, path: str):
        doc_id = self._doc_id_for_path_on_conn(self._read_conn, path)
        return self._effective_versions_on_conn(self._read_conn, doc_id)

    def list_versions_details(self, path: str, limit: int | None = None) -> list[dict]:
        if self._db_path is None:
            with self._lock:
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
        return self._effective_versions_on_conn(self._read_conn, doc_id)

    def list_versions_by_doc_details(self, doc_id: str, limit: int | None = None) -> list[dict]:
        if self._db_path is None:
            with self._lock:
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
                "inherited": r["doc_id"] != doc_id,
            }
            for r in rows
        ]

    def ensure_version_preview(self, version_id: str, page_no: int = 1, long_edge: int = 360) -> str | None:
        """Render and cache a small PNG preview for one historical version."""
        with self._lock:
            version = store.get_version(self._conn, version_id)
            if not version:
                return None
            cached = str(version["thumb_path"] or "")
            if cached and os.path.exists(cached):
                return cached
            doc_id = version["doc_id"]

        fd, tmp = tempfile.mkstemp(suffix=".pptx")
        os.close(fd)
        try:
            if not vault.rebuild_to(doc_id, version_id, tmp):
                return None
            page = max(1, int(page_no))
            try:
                png = renderer.render_page(
                    tmp,
                    page,
                    cache_key=f"version-{version_id}-p{page}",
                    long_edge=long_edge,
                    hi_priority=False,
                )
            finally:
                renderer.close_current_presentation()
            if not png or not os.path.exists(png):
                return None
            out = str(png)
            with self._lock:
                store.set_version_thumb_path(self._conn, version_id, out)
                self._conn.commit()
            return out
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def describe_version_diff(self, version_id: str) -> dict:
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
    def restore_to(self, path: str, version_id: str, dest: str | None = None) -> bool:
        with self._lock:
            target = dest or path
            if target == path and os.path.exists(path):
                self.snapshot_now(path, notify=False)
            version = store.get_version(self._conn, version_id)
            if not version:
                return False
            return vault.rebuild_to(version["doc_id"], version_id, target)

    def export(self, path: str, version_id: str, dest: str) -> bool:
        with self._lock:
            version = store.get_version(self._conn, version_id)
            owner_doc_id = version["doc_id"] if version else self._doc_id_for_path_on_conn(self._conn, path)
        return vault.rebuild_to(owner_doc_id, version_id, dest)

    # ---------- Cross-version search ----------
    def search_history(self, query: str):
        with self._lock:
            return store.search_versions(self._conn, build_fts_match_exact(query))

    def search_history_details(self, query: str, limit: int = 200) -> dict:
        match = build_fts_match_exact(query)
        if not match:
            return {"query": query, "total": 0, "rows": []}
        if self._db_path is None:
            with self._lock:
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
                SELECT f.doc_id, f.version_id, f.page_no, d.path AS doc_path, v.ts AS ts
                FROM version_pages_fts AS f
                LEFT JOIN managed_docs AS d ON d.doc_id = f.doc_id
                LEFT JOIN versions AS v ON v.version_id = f.version_id
                WHERE version_pages_fts MATCH ?
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
                }
                for r in rows
                if r["doc_path"]
            ],
        }

    # ---------- Deleted-file recovery ----------
    def scan_deleted(self) -> int:
        with self._lock:
            n = 0
            for doc in store.list_docs(self._conn):
                if doc["status"] == "active" and not os.path.exists(doc["path"]):
                    store.set_status(self._conn, doc["doc_id"], "deleted")
                    n += 1
            return n

    def recover(self, doc_id: str, dest: str | None = None) -> bool:
        with self._lock:
            doc = store.get_doc(self._conn, doc_id)
            latest = self._effective_latest_version_on_conn(self._conn, doc_id)
            if not doc or not latest:
                return False
            ok = vault.rebuild_to(latest["doc_id"], latest["version_id"], dest or doc["path"])
            if ok and (dest is None or dest == doc["path"]):
                store.set_status(self._conn, doc_id, "active")
            return ok

    # ---------- Quota ----------
    def _enforce_quota(self, doc_id: str) -> None:
        vers = store.list_versions(self._conn, doc_id)
        if len(vers) <= KEEP_PER_DOC:
            return
        for v in vers[KEEP_PER_DOC:]:
            store.delete_version(self._conn, v["version_id"])
            try:
                vault.version_file(doc_id, v["version_id"]).unlink(missing_ok=True)
            except OSError:
                pass
        self._conn.commit()

    # ---------- Watcher lifecycle ----------
    def start(self) -> None:
        self.scan_deleted()
        self._start_watcher()
        self._start_reconcile_loop()

    def _start_watcher(self) -> None:
        self._stop_watcher()
        from .watcher import VaultWatcher, default_watch_paths
        self._watcher = VaultWatcher(default_watch_paths(), self.snapshot_now, self.move_path)
        self._watcher.start()

    def _start_reconcile_loop(self) -> None:
        self._stop_reconcile_loop()
        if self._reconcile_interval_sec <= 0:
            return
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name="PPTDoctorVersionReconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def _reconcile_loop(self) -> None:
        while not self._reconcile_stop.wait(self._reconcile_interval_sec):
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

    def diagnostic_lines(self) -> list[str]:
        alive = self._reconcile_thread is not None and self._reconcile_thread.is_alive()
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
            f"error={self._reconcile_last_error or '-'}"
        ]
