"""SQLite metadata store for the PPT Doctor vault."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_docs(
  doc_id TEXT PRIMARY KEY, path TEXT NOT NULL, status TEXT DEFAULT 'active',
  latest_version_id TEXT DEFAULT '', created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS versions(
  version_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ts REAL DEFAULT 0,
  session_id TEXT DEFAULT '', page_count INTEGER DEFAULT 0, size INTEGER DEFAULT 0,
  changed TEXT DEFAULT '', thumb_path TEXT DEFAULT '', content_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_versions_doc ON versions(doc_id, ts);
CREATE INDEX IF NOT EXISTS idx_versions_hash ON versions(content_hash, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS version_pages_fts USING fts5(
  content, doc_id UNINDEXED, version_id UNINDEXED, page_no UNINDEXED
);
CREATE TABLE IF NOT EXISTS doc_paths(
  doc_id TEXT NOT NULL, path TEXT NOT NULL, path_key TEXT NOT NULL,
  status TEXT DEFAULT 'current', first_seen REAL DEFAULT 0, last_seen REAL DEFAULT 0,
  PRIMARY KEY(doc_id, path_key)
);
CREATE INDEX IF NOT EXISTS idx_doc_paths_key ON doc_paths(path_key, status, last_seen);
CREATE TABLE IF NOT EXISTS doc_branches(
  doc_id TEXT PRIMARY KEY, parent_doc_id TEXT NOT NULL,
  branched_from_version_id TEXT NOT NULL, branched_at REAL DEFAULT 0,
  reason TEXT DEFAULT ''
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "versions", "changed", "TEXT DEFAULT ''")
    _ensure_column(conn, "versions", "thumb_path", "TEXT DEFAULT ''")
    # Backfill path aliases for existing vaults created before doc_paths existed.
    for row in conn.execute("SELECT doc_id, path, created_at, updated_at FROM managed_docs").fetchall():
        ts = row["updated_at"] or row["created_at"] or 0
        record_path(conn, row["doc_id"], row["path"], ts, "current")
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


# ---- Managed documents ----
def upsert_doc(conn, doc_id: str, path: str, ts: float) -> None:
    conn.execute(
        """INSERT INTO managed_docs(doc_id, path, status, created_at, updated_at)
           VALUES(?,?,'active',?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
             path=excluded.path, status='active', updated_at=excluded.updated_at""",
        (doc_id, path, ts, ts),
    )
    record_path(conn, doc_id, path, ts, "current")


def record_path(conn, doc_id: str, path: str, ts: float, status: str = "current") -> None:
    key = path_key(path)
    if status == "current":
        conn.execute(
            """UPDATE doc_paths
               SET status='alias', last_seen=?
               WHERE path_key=? AND doc_id<>? AND status='current'""",
            (ts, key, doc_id),
        )
        conn.execute(
            """UPDATE doc_paths
               SET status='alias', last_seen=?
               WHERE doc_id=? AND path_key<>? AND status='current'""",
            (ts, doc_id, key),
        )
    conn.execute(
        """INSERT INTO doc_paths(doc_id, path, path_key, status, first_seen, last_seen)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(doc_id, path_key) DO UPDATE SET
             path=excluded.path, status=excluded.status, last_seen=excluded.last_seen""",
        (doc_id, path, key, status, ts, ts),
    )


def get_doc(conn, doc_id: str):
    return conn.execute("SELECT * FROM managed_docs WHERE doc_id=?", (doc_id,)).fetchone()


def get_doc_by_path(conn, path: str):
    return conn.execute(
        """SELECT d.*
           FROM doc_paths AS p
           JOIN managed_docs AS d ON d.doc_id=p.doc_id
           WHERE p.path_key=? AND p.status='current'
           ORDER BY p.last_seen DESC
           LIMIT 1""",
        (path_key(path),),
    ).fetchone()


def list_docs(conn):
    return conn.execute("SELECT * FROM managed_docs ORDER BY updated_at DESC").fetchall()


def summary_stats(conn) -> dict[str, int]:
    """Return non-overloaded vault KPIs.

    ``protected_docs`` counts documents that actually own at least one stored
    version; ``rollback_docs`` requires two or more versions. This deliberately
    avoids presenting the managed-document row count as a snapshot count.
    """
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM managed_docs) AS managed_docs,
             (SELECT COUNT(*) FROM managed_docs WHERE status='active') AS active_docs,
             (SELECT COUNT(*) FROM managed_docs WHERE status='deleted') AS deleted_docs,
             (SELECT COUNT(*) FROM versions) AS total_versions,
             (SELECT COUNT(*) FROM (
                SELECT doc_id FROM versions GROUP BY doc_id HAVING COUNT(*) >= 1
              )) AS protected_docs,
             (SELECT COUNT(*) FROM (
                SELECT doc_id FROM versions GROUP BY doc_id HAVING COUNT(*) >= 2
              )) AS rollback_docs,
             (SELECT COUNT(*) FROM (
                SELECT doc_id FROM versions GROUP BY doc_id HAVING COUNT(*) = 1
              )) AS single_version_docs
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def set_status(conn, doc_id: str, status: str) -> None:
    conn.execute("UPDATE managed_docs SET status=? WHERE doc_id=?", (status, doc_id))
    conn.commit()


def set_latest(conn, doc_id: str, version_id: str) -> None:
    conn.execute("UPDATE managed_docs SET latest_version_id=? WHERE doc_id=?", (version_id, doc_id))


# ---- Versions ----
def add_version(conn, version_id, doc_id, ts, session_id, page_count, size, content_hash, changed="") -> None:
    conn.execute(
        """INSERT INTO versions(version_id, doc_id, ts, session_id, page_count, size, content_hash, changed)
           VALUES(?,?,?,?,?,?,?,?)""",
        (version_id, doc_id, ts, session_id, page_count, size, content_hash, changed),
    )


def set_version_thumb_path(conn, version_id: str, thumb_path: str) -> None:
    conn.execute("UPDATE versions SET thumb_path=? WHERE version_id=?", (thumb_path, version_id))


def list_versions(conn, doc_id: str):
    return conn.execute("SELECT * FROM versions WHERE doc_id=? ORDER BY ts DESC", (doc_id,)).fetchall()


def list_versions_through(conn, doc_id: str, version_id: str):
    return conn.execute(
        """SELECT *
           FROM versions
           WHERE doc_id=?
             AND ts <= COALESCE((SELECT ts FROM versions WHERE version_id=?), -1)
           ORDER BY ts DESC""",
        (doc_id, version_id),
    ).fetchall()


def get_version(conn, version_id: str):
    return conn.execute("SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()


def latest_version(conn, doc_id: str):
    return conn.execute(
        "SELECT * FROM versions WHERE doc_id=? ORDER BY ts DESC LIMIT 1", (doc_id,)
    ).fetchone()


def previous_version(conn, doc_id: str, ts: float, version_id: str):
    return conn.execute(
        """SELECT * FROM versions
           WHERE doc_id=?
             AND (ts < ? OR (ts = ? AND version_id < ?))
           ORDER BY ts DESC, version_id DESC
           LIMIT 1""",
        (doc_id, ts, ts, version_id),
    ).fetchone()


def version_pages(conn, version_id: str):
    return conn.execute(
        "SELECT page_no, content FROM version_pages_fts WHERE version_id=? ORDER BY page_no",
        (version_id,),
    ).fetchall()


def find_versions_by_content_hash(conn, content_hash: str):
    if not content_hash:
        return []
    return conn.execute(
        "SELECT * FROM versions WHERE content_hash=? ORDER BY ts DESC",
        (content_hash,),
    ).fetchall()


def delete_version(conn, version_id: str) -> None:
    conn.execute("DELETE FROM versions WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM version_pages_fts WHERE version_id=?", (version_id,))


# ---- Copy branches ----
def record_branch(
    conn,
    doc_id: str,
    parent_doc_id: str,
    branched_from_version_id: str,
    ts: float,
    reason: str,
) -> None:
    conn.execute(
        """INSERT INTO doc_branches(doc_id, parent_doc_id, branched_from_version_id, branched_at, reason)
           VALUES(?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
             parent_doc_id=excluded.parent_doc_id,
             branched_from_version_id=excluded.branched_from_version_id,
             branched_at=excluded.branched_at,
             reason=excluded.reason""",
        (doc_id, parent_doc_id, branched_from_version_id, ts, reason),
    )


def get_branch(conn, doc_id: str):
    return conn.execute("SELECT * FROM doc_branches WHERE doc_id=?", (doc_id,)).fetchone()


# ---- Full-text history search ----
def index_pages(conn, doc_id: str, version_id: str, pages: list[tuple[int, str]]) -> None:
    for pno, toks in pages:
        if toks:
            conn.execute(
                "INSERT INTO version_pages_fts(content, doc_id, version_id, page_no) VALUES(?,?,?,?)",
                (toks, doc_id, version_id, pno),
            )


def search_versions(conn, match: str):
    if not match:
        return []
    try:
        return conn.execute(
            "SELECT doc_id, version_id, page_no FROM version_pages_fts WHERE version_pages_fts MATCH ?",
            (match,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
