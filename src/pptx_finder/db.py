"""SQLite 索引库：schema + 读写原语。

并发模型：WAL 模式下允许多读 + 单写。索引线程持有写连接，搜索用各自读连接。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .text_tokenize import normalize, tokenize

log = logging.getLogger(__name__)

# 索引格式版本：分词器/切词规则改版即与旧库不兼容（如词级 jieba → 字级），
# 启动发现版本不符就清空内容、走全量重建——否则「原文里有、却怎么都搜不到」。
# 也兼作"强制重建"开关：v0.7.0 首启重扫会冻结 UI，多数人的库停在残缺态（部分文件 +
# 已盖 v2 标记 → 不会自动重扫）；2→3 让修复版（重扫已不冻结）自动重建这些残缺库。
# 5→6：内容搜索从只 pptx 扩到 docx/xlsx/txt/pdf，旧库需重建以纳入这些文档类型。
INDEX_VERSION = "6"
META_INDEX_REBUILD_REASON = "last_index_rebuild_reason"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  name_norm TEXT DEFAULT '',
  ext TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  content_hash TEXT,
  page_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ok',
  error TEXT DEFAULT '',
  indexed_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE VIRTUAL TABLE IF NOT EXISTS file_names_fts USING fts5(
  content, file_id UNINDEXED
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  content, file_id UNINDEXED, page_no UNINDEXED
);
CREATE TABLE IF NOT EXISTS pages_raw(
  file_id INTEGER NOT NULL,
  page_no INTEGER NOT NULL,
  raw_text TEXT,
  PRIMARY KEY(file_id, page_no)
);
CREATE TABLE IF NOT EXISTS minhash(
  file_id INTEGER PRIMARY KEY,
  sig BLOB,
  page_hashes TEXT,
  group_id INTEGER
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def sqlite_safe_text(text: str | None) -> str:
    """Return text SQLite can UTF-8 encode, dropping invalid UTF-16 surrogates."""
    if not text:
        return ""
    return "".join(ch for ch in str(text) if not 0xD800 <= ord(ch) <= 0xDFFF)


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")  # 遇锁等待而非立即失败（多连接/偶发并发）
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_filename_index(conn)
    _migrate_index_version(conn)
    conn.commit()


def _ensure_filename_index(conn: sqlite3.Connection) -> None:
    """Add/backfill normalized filename search data without forcing a full rebuild."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
    if "name_norm" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN name_norm TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name_norm ON files(name_norm)")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS file_names_fts USING fts5("
        "content, file_id UNINDEXED)"
    )
    rows = conn.execute(
        "SELECT id, name FROM files WHERE name_norm IS NULL OR name_norm=''"
    ).fetchall()
    for r in rows:
        _update_filename_index(conn, r["id"], r["name"])
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
    if file_count and fts_count < file_count:
        conn.execute("DELETE FROM file_names_fts")
        for r in conn.execute("SELECT id, name FROM files").fetchall():
            _update_filename_index(conn, r["id"], r["name"])


def _migrate_index_version(conn: sqlite3.Connection) -> None:
    """索引格式版本守门：版本不符且库里已有数据 → 清空内容，让启动走全量重建。

    老库（升级前）没有 index_version 标记（=None）但已有词级 token，命中本分支被清空；
    全新空库（stored=None 且无数据）只盖版本号、不清空，正常走首次全量索引。
    幂等：同版本直接返回，多连接重复调用安全。
    """
    row = conn.execute("SELECT value FROM meta WHERE key='index_version'").fetchone()
    stored = row["value"] if row else None
    if stored == INDEX_VERSION:
        return
    has_data = conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None
    if has_data:
        for t in ("files", "file_names_fts", "pages_fts", "pages_raw", "minhash"):
            conn.execute(f"DELETE FROM {t}")
        log.info("索引格式 %s→%s：已清空旧索引，将全量重建", stored, INDEX_VERSION)
        set_meta(conn, META_INDEX_REBUILD_REASON, f"index_version:{stored or 'none'}->{INDEX_VERSION}")
    else:
        delete_meta(conn, META_INDEX_REBUILD_REASON)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('index_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (INDEX_VERSION,),
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def meta_value(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row and row["value"] is not None else default


def delete_meta(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key=?", (key,))


def get_file_by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()


def all_indexed(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """path -> row，用于增量比对。"""
    return {r["path"]: r for r in conn.execute("SELECT * FROM files").fetchall()}


def upsert_file(
    conn: sqlite3.Connection,
    *,
    path: str,
    name: str,
    ext: str,
    size: int,
    mtime: float,
    content_hash: str,
    page_count: int,
    status: str,
    error: str,
    indexed_at: float,
) -> int:
    name = sqlite_safe_text(name)
    error = sqlite_safe_text(error)
    name_norm = normalize(name)
    cur = conn.execute(
        """
        INSERT INTO files(path,name,name_norm,ext,size,mtime,content_hash,page_count,status,error,indexed_at)
        VALUES(:path,:name,:name_norm,:ext,:size,:mtime,:content_hash,:page_count,:status,:error,:indexed_at)
        ON CONFLICT(path) DO UPDATE SET
          name=excluded.name, name_norm=excluded.name_norm, ext=excluded.ext, size=excluded.size, mtime=excluded.mtime,
          content_hash=excluded.content_hash, page_count=excluded.page_count,
          status=excluded.status, error=excluded.error, indexed_at=excluded.indexed_at
        RETURNING id
        """,
        dict(path=path, name=name, name_norm=name_norm, ext=ext, size=size, mtime=mtime,
             content_hash=content_hash, page_count=page_count, status=status,
             error=error, indexed_at=indexed_at),
    )
    file_id = cur.fetchone()[0]
    _update_filename_index(conn, file_id, name)
    return file_id


def _update_filename_index(conn: sqlite3.Connection, file_id: int, name: str) -> None:
    name = sqlite_safe_text(name)
    conn.execute("UPDATE files SET name_norm=? WHERE id=?", (normalize(name), file_id))
    conn.execute("DELETE FROM file_names_fts WHERE file_id=?", (file_id,))
    conn.execute(
        "INSERT INTO file_names_fts(content,file_id) VALUES(?,?)",
        (tokenize(name), file_id),
    )


def replace_pages(conn: sqlite3.Connection, file_id: int, pages: list[tuple[int, str, str]]) -> None:
    """pages: [(page_no, raw_text, tokenized_content)]。先清旧页再写。"""
    conn.execute("DELETE FROM pages_fts WHERE file_id=?", (file_id,))
    conn.execute("DELETE FROM pages_raw WHERE file_id=?", (file_id,))
    for page_no, raw, tok in pages:
        raw = sqlite_safe_text(raw)
        tok = sqlite_safe_text(tok)
        conn.execute(
            "INSERT INTO pages_fts(content,file_id,page_no) VALUES(?,?,?)",
            (tok, file_id, page_no),
        )
        conn.execute(
            "INSERT INTO pages_raw(file_id,page_no,raw_text) VALUES(?,?,?)",
            (file_id, page_no, raw),
        )


def touch_stat(conn: sqlite3.Connection, file_id: int, size: int, mtime: float, indexed_at: float) -> None:
    conn.execute(
        "UPDATE files SET size=?, mtime=?, indexed_at=? WHERE id=?",
        (size, mtime, indexed_at, file_id),
    )


def delete_file(conn: sqlite3.Connection, path: str) -> None:
    row = get_file_by_path(conn, path)
    if not row:
        return
    fid = row["id"]
    conn.execute("DELETE FROM pages_fts WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM pages_raw WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM file_names_fts WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM minhash WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM files WHERE id=?", (fid,))


def stats(conn: sqlite3.Connection, exts: tuple[str, ...] | None = None) -> dict:
    """库统计。exts 给定则只统计这些扩展名（如 config.PPT_EXTS）——胶片报告/仪表盘按 PPT 用；
    默认 None = 全类型（底部状态栏 / 搜索覆盖）。"""
    ex = tuple(e.lower() for e in exts) if exts else ()
    fw = (" WHERE lower(ext) IN (%s)" % ",".join("?" * len(ex))) if ex else ""
    fc = conn.execute(f"SELECT COUNT(*) FROM files{fw}", ex).fetchone()[0]
    pc_sql = (
        "SELECT COUNT(*) FROM pages_raw" if not ex
        else f"SELECT COUNT(*) FROM pages_raw WHERE file_id IN (SELECT id FROM files{fw})"
    )
    pc = conn.execute(pc_sql, ex).fetchone()[0]
    status_counts = {
        (r["status"] or ""): int(r["count"])
        for r in conn.execute(
            f"SELECT status, COUNT(*) AS count FROM files{fw} GROUP BY status", ex
        ).fetchall()
    }
    return {
        "file_count": fc,
        "page_count": pc,
        "status_counts": status_counts,
        "pending_count": status_counts.get("pending", 0),
        "error_count": status_counts.get("error", 0),
        "scanned_count": status_counts.get("scanned", 0),
    }


def type_counts(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """每个扩展名的 (已建内容, 已发现总数)。已建 = status 非 'pending'
    （pending = 已登记文件名、内容还没建）。供底部状态栏「分类型索引进度」x/y 用。"""
    out: dict[str, tuple[int, int]] = {}
    for r in conn.execute(
        "SELECT lower(ext) AS e, "
        "SUM(CASE WHEN status='pending' THEN 0 ELSE 1 END) AS built, "
        "COUNT(*) AS total FROM files GROUP BY lower(ext)"
    ).fetchall():
        out[r["e"] or ""] = (int(r["built"] or 0), int(r["total"] or 0))
    return out


def maintain(conn: sqlite3.Connection) -> dict:
    """Run cheap SQLite maintenance after indexing.

    FTS optimize merges segment b-trees and keeps long-running local indexes from
    slowly degrading. The WAL checkpoint is non-destructive and bounded by the
    busy timeout on the connection.
    """
    result = {"fts_optimized": 0, "checkpointed": False, "error": ""}
    try:
        for table in ("file_names_fts", "pages_fts"):
            try:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                result["fts_optimized"] += 1
            except sqlite3.DatabaseError as exc:
                log.debug("fts optimize skipped for %s: %s", table, exc)
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            result["checkpointed"] = True
        except sqlite3.DatabaseError as exc:
            log.debug("wal checkpoint skipped: %s", exc)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def recent_files(conn: sqlite3.Connection, limit: int = 20, exts: tuple[str, ...] | None = None) -> list:
    """最近修改的文件（空查询默认视图 / 仪表盘最近活跃）。返回无命中片段的 FileResult，按 mtime 降序。
    exts 给定则只取这些扩展名（仪表盘按 PPT 用）；默认 None = 全类型。"""
    from .models import FileResult
    ex = tuple(e.lower() for e in exts) if exts else ()
    fw = (" WHERE lower(ext) IN (%s)" % ",".join("?" * len(ex))) if ex else ""
    rows = conn.execute(
        f"SELECT * FROM files{fw} ORDER BY mtime DESC LIMIT ?", (*ex, limit)
    ).fetchall()
    return [
        FileResult(
            file_id=r["id"], path=r["path"], name=r["name"], ext=r["ext"],
            mtime=r["mtime"], size=r["size"], page_count=r["page_count"],
            status=r["status"], score=0.0, name_hit=False, hits=[],
        )
        for r in rows
    ]


def get_page_text(conn: sqlite3.Connection, file_id: int, page_no: int) -> str:
    """取某文件某页的原文（pages_raw.raw_text）。无则空串。供「复制本页文字」用，
    直接读已索引文本，不依赖 PowerPoint COM。"""
    row = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? AND page_no=?",
        (file_id, page_no),
    ).fetchone()
    return (row["raw_text"] if row and row["raw_text"] else "") or ""


def page_titles(conn: sqlite3.Connection, file_id: int, limit: int = 40) -> list:
    """每页首行作大纲标题（近似，用已索引的 raw_text）。返回 [(page_no, title)]。"""
    rows = conn.execute(
        "SELECT page_no, raw_text FROM pages_raw WHERE file_id=? ORDER BY page_no LIMIT ?",
        (file_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        first = ((r["raw_text"] or "").strip().split("\n", 1)[0]).strip()[:38]
        out.append((r["page_no"], first or f"第 {r['page_no']} 页"))
    return out
