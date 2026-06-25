"""Version-management orchestration for PPTutor."""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
import threading

from .. import renderer
from ..config import PPTX_EXT
from ..scanner import iter_ppt_files
from ..text_tokenize import build_fts_match_exact
from . import store, vault

SESSION_GAP_SEC = 30 * 60
KEEP_PER_DOC = 50


def _now() -> float:
    return datetime.datetime.now().timestamp()


def _sid(ts: float) -> str:
    return "s" + datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M")


def _is_pptx(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == PPTX_EXT


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
        candidates = store.find_versions_by_content_hash(self._conn, content_hash)
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

    def _start_watcher(self) -> None:
        self._stop_watcher()
        from .watcher import VaultWatcher, default_watch_paths
        self._watcher = VaultWatcher(default_watch_paths(), self.snapshot_now, self.move_path)
        self._watcher.start()

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def stop(self) -> None:
        self._stop_watcher()
