"""版本库元数据存储：独立 SQLite（vault/versions.db），不碰主索引库。

表：managed_docs（受管文档，即被监听到改过的文件）/ versions（版本记录）
   / version_pages_fts（历史版本逐页文本，供跨版本内容搜索）。
无「受管目录」表——全盘监听、谁变管谁，不需要预先登记目录。
"""
from __future__ import annotations

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
CREATE VIRTUAL TABLE IF NOT EXISTS version_pages_fts USING fts5(
  content, doc_id UNINDEXED, version_id UNINDEXED, page_no UNINDEXED
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
    conn.commit()


# ---- 受管文档 ----
def upsert_doc(conn, doc_id: str, path: str, ts: float) -> None:
    conn.execute(
        """INSERT INTO managed_docs(doc_id, path, status, created_at, updated_at)
           VALUES(?,?,'active',?,?)
           ON CONFLICT(doc_id) DO UPDATE SET path=excluded.path, status='active', updated_at=excluded.updated_at""",
        (doc_id, path, ts, ts),
    )


def get_doc(conn, doc_id: str):
    return conn.execute("SELECT * FROM managed_docs WHERE doc_id=?", (doc_id,)).fetchone()


def list_docs(conn):
    return conn.execute("SELECT * FROM managed_docs ORDER BY updated_at DESC").fetchall()


def set_status(conn, doc_id: str, status: str) -> None:
    conn.execute("UPDATE managed_docs SET status=? WHERE doc_id=?", (status, doc_id))
    conn.commit()


def set_latest(conn, doc_id: str, version_id: str) -> None:
    conn.execute("UPDATE managed_docs SET latest_version_id=? WHERE doc_id=?", (version_id, doc_id))


# ---- 版本 ----
def add_version(conn, version_id, doc_id, ts, session_id, page_count, size, content_hash, changed="") -> None:
    conn.execute(
        """INSERT INTO versions(version_id, doc_id, ts, session_id, page_count, size, content_hash, changed)
           VALUES(?,?,?,?,?,?,?,?)""",
        (version_id, doc_id, ts, session_id, page_count, size, content_hash, changed),
    )


def list_versions(conn, doc_id: str):
    return conn.execute("SELECT * FROM versions WHERE doc_id=? ORDER BY ts DESC", (doc_id,)).fetchall()


def get_version(conn, version_id: str):
    return conn.execute("SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()


def latest_version(conn, doc_id: str):
    return conn.execute(
        "SELECT * FROM versions WHERE doc_id=? ORDER BY ts DESC LIMIT 1", (doc_id,)
    ).fetchone()


def delete_version(conn, version_id: str) -> None:
    conn.execute("DELETE FROM versions WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM version_pages_fts WHERE version_id=?", (version_id,))


# ---- 跨版本内容搜索（FTS） ----
def index_pages(conn, doc_id: str, version_id: str, pages: list[tuple[int, str]]) -> None:
    """pages: [(page_no, tokenized_text)]。"""
    for pno, toks in pages:
        if toks:
            conn.execute(
                "INSERT INTO version_pages_fts(content, doc_id, version_id, page_no) VALUES(?,?,?,?)",
                (toks, doc_id, version_id, pno),
            )


def search_versions(conn, match: str):
    """返回命中历史版本页：[(doc_id, version_id, page_no)]。match 为 FTS5 表达式。"""
    if not match:
        return []
    try:
        return conn.execute(
            "SELECT doc_id, version_id, page_no FROM version_pages_fts WHERE version_pages_fts MATCH ?",
            (match,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
