"""SQLite 索引库：schema + 读写原语。

并发模型：WAL 模式下允许多读 + 单写。索引线程持有写连接，搜索用各自读连接。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from .text_tokenize import normalize, tokenize

log = logging.getLogger(__name__)

#: 空闲空间达到这个绝对量就整理（不看占比）——大库上「25% 空闲」可能已经是好几个 G。
DEFAULT_VACUUM_MIN_FREE_BYTES = 256 * 1024 * 1024
#: 或者空闲占比达到这个比例也整理，但至少要有 DEFAULT_VACUUM_FLOOR_BYTES 那么多，
#: 免得为了几百 KB 去 VACUUM 一个刚建好的小库。
DEFAULT_VACUUM_MIN_FREE_RATIO = 0.25
#: 走「占比」这条路时的绝对下限。
#: 为什么要有这条：原来两个门槛是**与**的关系，于是一个 132 MB 的库无论怎么膨胀
#: 都到不了「空闲 256 MB」，VACUUM 一次都不会跑。真机实测就是这样——索引格式
#: 迁移把旧数据清空之后，132 MB 里 86%（114 MB）是空闲页，白占着磁盘。
#: 改成「或」之后这台机器上实测：VACUUM 132 MB -> 18 MB，耗时 0.2 秒。
DEFAULT_VACUUM_FLOOR_BYTES = 32 * 1024 * 1024
#: WAL 超过这个大小就在维护时截断。WAL 只有在没有读者时才能收缩，平时的
#: PASSIVE checkpoint 只会把内容搬进主库、不还给磁盘，于是文件一直长着。
#: 真机实测：32 MB 的 WAL，TRUNCATE 一次 0.13 秒就归零。
WAL_TRUNCATE_BYTES = 8 * 1024 * 1024

# 索引格式版本：分词器/切词规则改版即与旧库不兼容（如词级 jieba → 字级），
# 启动发现版本不符就清空内容、走全量重建——否则「原文里有、却怎么都搜不到」。
# 也兼作"强制重建"开关：v0.7.0 首启重扫会冻结 UI，多数人的库停在残缺态（部分文件 +
# 已盖 v2 标记 → 不会自动重扫）；2→3 让修复版（重扫已不冻结）自动重建这些残缺库。
# 5→6：内容搜索从只 pptx 扩到 docx/xlsx/txt/pdf，旧库需重建以纳入这些文档类型。
# 6→7：切词补上希腊字母/带音标拉丁/假名/韩文/CJK 扩展 A 汉字（原先这些字符被当
#      分隔符丢弃，τ λ Δ 一类根本搜不到）。旧库里没有这些 token，不重建等于没修。
INDEX_VERSION = "7"
META_INDEX_REBUILD_REASON = "last_index_rebuild_reason"
META_LAST_COMPLETED_SCAN_AT = "last_completed_scan_at"
META_LAST_KNOWN_RECONCILE_AT = "last_known_reconcile_at"
META_KNOWN_RECONCILE_CURSOR = "known_reconcile_cursor"
META_SCAN_POLICY_VERSION = "scan_policy_version"
META_LAST_SCAN_UNREADABLE_DIRS = "last_scan_unreadable_dirs"
META_LAST_SCAN_ERROR_EXAMPLES = "last_scan_error_examples"
META_LAST_SCAN_ERROR_PATHS = "last_scan_error_paths"

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
  parse_failures INTEGER DEFAULT 0,
  retry_after REAL DEFAULT 0,
  indexed_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
-- 「索引所有文件」开启后 files 可达百万行，而底部状态栏 / 仪表盘 / 启动对账的
-- 热查询全是 lower(ext) 与 status 上的过滤与分组——没有索引时每次都是全表扫。
-- 表达式索引（SQLite ≥3.9）直接匹配现有 lower(ext) 写法：不改一行 SQL、不动
-- 存量数据，也就不存在「假设 ext 一定小写」的迁移风险。(lower(ext), status)
-- 对 db.stats 的 COUNT、type_counts 的 GROUP BY 都是覆盖索引，对
-- status='filename_only' 的盘点残留计数也能走 index-only 全扫。
-- 100 万行实测：db.stats 853ms→1.5ms、type_counts 695ms→133ms；
-- 代价是盘点写入 +41%、库体积 +19%。再加一条 (status, lower(ext)) 只多换 10ms，
-- 却让写入再慢 78%，不值——故只保留这一条。
CREATE INDEX IF NOT EXISTS idx_files_ext_status ON files(lower(ext), status);
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
    # 这里刻意不设 journal_size_limit（版本库侧设了）：索引库的写入形态是建库时的
    # 批量灌入（盘点可达百万行），而回缩 WAL 要在 checkpoint 后额外截断文件，
    # A/B 实测让分档写入吞吐的末/首比从 0.84 掉到 0.72。索引库的 WAL 只在扫描期
    # 短暂变大、随后就被 checkpoint 收掉，为它牺牲建库速度不划算。
    return conn


def _readonly_uri(db_path: str | Path) -> str:
    """Build a SQLite URI without resolving or touching a redirected path."""
    raw = os.fspath(db_path)
    # Normalize Win32 extended prefixes before URI encoding. App data normally
    # uses a local path, but corporate profile redirection can legitimately put
    # the database on a UNC share.
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]
    normalized = os.path.abspath(raw).replace("\\", "/")
    if normalized.startswith("//"):
        # ``file://server/share`` treats ``server`` as a URI authority, which
        # standard SQLite rejects. Four slashes encode the UNC path itself.
        encoded = quote(normalized.lstrip("/"), safe="/:")
        return f"file:////{encoded}?mode=ro"
    return f"file:{quote(normalized, safe='/:')}?mode=ro"


def connect_readonly(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = 400,
) -> sqlite3.Connection:
    """Open a fail-fast, truly read-only connection for interactive queries.

    ``connect()`` deliberately waits for writers and executes
    ``PRAGMA journal_mode=WAL`` because indexing/version maintenance may write.
    Doing that for the first interactive search can itself wait several seconds
    behind a schema/VACUUM lock.  A search should instead preserve the current
    results and ask the user to retry quickly, never hold the spinner for the
    global eight-second writer timeout.
    """
    timeout_ms = max(0, int(busy_timeout_ms))
    uri = _readonly_uri(db_path)
    conn = sqlite3.connect(
        uri,
        uri=True,
        check_same_thread=False,
        timeout=timeout_ms / 1000.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    conn.execute("PRAGMA query_only=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 顺序有讲究：格式版本守门必须排在文件名索引回填之前。反过来（旧顺序）撞上
    # INDEX_VERSION 升级时，会先把全表 name_norm + FTS 老老实实重建一遍，下一句
    # 就 DELETE 清空——回填的活 100% 白干，还是在 GUI 线程上白干。
    _migrate_index_version(conn)
    _ensure_filename_index(conn)
    _ensure_parse_retry_columns(conn)
    conn.commit()


def _rebuild_filename_fts(conn: sqlite3.Connection) -> None:
    """整表重建文件名 FTS：一次清空 + 批量插入，O(n)。

    绝不逐行 `DELETE FROM file_names_fts WHERE file_id=?`——file_id 是 FTS5 的
    UNINDEXED 列，每次删都是全表扫，整体塌成 O(n²)。这正是 indexer 的盘点批量
    写入当年踩过并修掉的同一个坑（见 _write_filename_only_batch 的注释），
    此处是它漏网的另一半。实测旧实现：5k 行 2.3s / 1w 行 11s / 2w 行 41s /
    4w 行 156s（4× 行数 → 67× 耗时），而 init_db 是在 MainWindow.__init__ 里
    同步调用的，窗口还没 show 就已经假死。
    """
    conn.execute("DELETE FROM file_names_fts")
    conn.executemany(
        "INSERT INTO file_names_fts(content,file_id) VALUES(?,?)",
        (
            (tokenize(sqlite_safe_text(r["name"])), r["id"])
            for r in conn.execute("SELECT id, name FROM files")
        ),
    )


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
    # name_norm 回填与 FTS 重建刻意解耦：name_norm 只参与命中后的字面复核，
    # 且 search 侧本来就有 `row["name_norm"] or normalize(row["name"])` 兜底，
    # 补它不需要动 FTS。旧实现把两件事绑在一起，于是「老库新增一列」这种纯元数据
    # 迁移也要付一遍逐行 FTS 删除 = O(n²)。
    stale = conn.execute(
        "SELECT id, name FROM files WHERE name_norm IS NULL OR name_norm=''"
    ).fetchall()
    if stale:
        conn.executemany(
            "UPDATE files SET name_norm=? WHERE id=?",
            [(normalize(sqlite_safe_text(r["name"])), r["id"]) for r in stale],
        )
    # FTS 只在真的对不上（写入被中断过）时整表重建——O(n)，不是逐行修补。
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
    if file_count and fts_count < file_count:
        _rebuild_filename_fts(conn)


def _ensure_parse_retry_columns(conn: sqlite3.Connection) -> None:
    """Add retry state in place; this metadata does not require a content rebuild."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
    if "parse_failures" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN parse_failures INTEGER DEFAULT 0")
    if "retry_after" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN retry_after REAL DEFAULT 0")
    # PPT 内嵌的创建时间（dcterms:created）。存量行留 0，随各自下一次解析自然补齐——
    # 刻意不升 INDEX_VERSION：为一个「锦上添花」的统计口径让全体用户再全量重建一次
    # 不值当，报告侧拿不到就退回 mtime。
    if "created_at" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN created_at REAL DEFAULT 0")


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


class IndexStat:
    """增量比对的轻量投影行：与 sqlite3.Row 一样支持 ``row["字段"]`` 访问，
    消费方（indexer 的未变更快筛/删除通道）无需区分两种来源。"""

    __slots__ = (
        "size", "mtime", "status", "content_hash", "parse_failures", "retry_after",
    )

    def __init__(self, size, mtime, status, content_hash, parse_failures, retry_after):
        self.size = size
        self.mtime = mtime
        self.status = status
        self.content_hash = content_hash
        self.parse_failures = parse_failures
        self.retry_after = retry_after

    def __getitem__(self, key: str):
        return getattr(self, key)


def all_indexed_stats(conn: sqlite3.Connection) -> dict[str, IndexStat]:
    """path -> IndexStat 轻量投影，用于增量比对。

    ``all_indexed`` 把全量 Row 载入内存，百万行盘点库实测约 700MB；本投影只保留
    增量比对所需字段（path/size/mtime/status/content_hash/parse_failures/retry_after），
    且非 ok 行不带 content_hash（只有 ok 行的 sha256 回填判定会读它），峰值约减半。
    """
    out: dict[str, IndexStat] = {}
    for r in conn.execute(
        "SELECT path, size, mtime, status, content_hash, parse_failures, retry_after "
        "FROM files"
    ):
        status = sys.intern(r["status"] or "")  # 状态值高度重复，驻留去重
        out[r["path"]] = IndexStat(
            r["size"],
            r["mtime"],
            status,
            r["content_hash"] if status == "ok" else None,
            r["parse_failures"],
            r["retry_after"],
        )
    return out


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
    parse_failures: int = 0,
    retry_after: float = 0.0,
    created_at: float = 0.0,
) -> int:
    name = sqlite_safe_text(name)
    error = sqlite_safe_text(error)
    name_norm = normalize(name)
    cur = conn.execute(
        """
        INSERT INTO files(
          path,name,name_norm,ext,size,mtime,content_hash,page_count,status,error,
          parse_failures,retry_after,indexed_at,created_at
        )
        VALUES(
          :path,:name,:name_norm,:ext,:size,:mtime,:content_hash,:page_count,:status,:error,
          :parse_failures,:retry_after,:indexed_at,:created_at
        )
        ON CONFLICT(path) DO UPDATE SET
          name=excluded.name, name_norm=excluded.name_norm, ext=excluded.ext, size=excluded.size, mtime=excluded.mtime,
          content_hash=excluded.content_hash, page_count=excluded.page_count,
          status=excluded.status, error=excluded.error,
          parse_failures=excluded.parse_failures, retry_after=excluded.retry_after,
          indexed_at=excluded.indexed_at,
          -- 0 表示这次没解析出创建时间（如仅登记文件名），别把已有的好值冲掉
          created_at=CASE WHEN excluded.created_at>0 THEN excluded.created_at
                          ELSE files.created_at END
        RETURNING id
        """,
        dict(path=path, name=name, name_norm=name_norm, ext=ext, size=size, mtime=mtime,
             content_hash=content_hash, page_count=page_count, status=status,
             error=error, parse_failures=max(0, int(parse_failures)),
             retry_after=max(0.0, float(retry_after)), indexed_at=indexed_at,
             created_at=max(0.0, float(created_at or 0.0))),
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


def _wal_bytes(conn: sqlite3.Connection) -> int:
    """当前主库对应的 -wal 文件有多大；内存库或取不到时返回 0。"""
    try:
        for _seq, name, filename in conn.execute("PRAGMA database_list"):
            if name == "main" and filename:
                return os.path.getsize(str(filename) + "-wal")
    except (sqlite3.DatabaseError, OSError):
        pass
    return 0


def maintain(
    conn: sqlite3.Connection,
    *,
    min_free_bytes: int = DEFAULT_VACUUM_MIN_FREE_BYTES,
    min_free_ratio: float = DEFAULT_VACUUM_MIN_FREE_RATIO,
    floor_bytes: int = DEFAULT_VACUUM_FLOOR_BYTES,
    wal_truncate_bytes: int = WAL_TRUNCATE_BYTES,
) -> dict:
    """Run bounded SQLite maintenance after indexing.

    FTS optimize merges segment b-trees and keeps long-running local indexes from
    slowly degrading. A full VACUUM only runs when *both* the absolute and ratio
    thresholds are crossed, so routine incremental scans stay cheap while a
    one-off format contraction can actually return large freelists to disk.
    """
    result = {
        "fts_optimized": 0,
        "checkpointed": False,
        "vacuumed": False,
        "page_size": 0,
        "page_count_before": 0,
        "free_pages_before": 0,
        "free_bytes_before": 0,
        "free_ratio_before": 0.0,
        "page_count_after": 0,
        "free_pages_after": 0,
        "wal_bytes_before": 0,
        "wal_bytes_after": 0,
        "wal_truncated": False,
        "error": "",
    }
    try:
        for table in ("file_names_fts", "pages_fts"):
            try:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                result["fts_optimized"] += 1
            except sqlite3.DatabaseError as exc:
                log.debug("fts optimize skipped for %s: %s", table, exc)
        conn.commit()

        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        free_bytes = page_size * free_pages
        free_ratio = (free_pages / page_count) if page_count else 0.0
        result.update({
            "page_size": page_size,
            "page_count_before": page_count,
            "free_pages_before": free_pages,
            "free_bytes_before": free_bytes,
            "free_ratio_before": free_ratio,
        })

        # 「或」而不是「与」：两个门槛各自成立即可。原来是「与」，结果一个 132 MB
        # 的库永远够不到「空闲 256 MB」，VACUUM 一次都不会跑（实测 86% 是空闲页）。
        should_vacuum = free_pages > 0 and (
            free_bytes >= max(0, int(min_free_bytes))
            or (free_ratio >= max(0.0, float(min_free_ratio))
                and free_bytes >= max(0, int(floor_bytes)))
        )
        if should_vacuum:
            try:
                conn.execute("VACUUM")
                result["vacuumed"] = True
                log.info(
                    "sqlite vacuum reclaimed candidate space: %.1f MiB (%.1f%% freelist)",
                    free_bytes / (1024 * 1024),
                    free_ratio * 100,
                )
            except sqlite3.DatabaseError as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                log.warning("sqlite vacuum skipped: %s", exc)

        # WAL 只有 TRUNCATE 才会把文件还给磁盘；PASSIVE 只是把内容搬进主库，
        # 文件照样一直长着。刚 VACUUM 过、或者 WAL 已经攒得够大，就截断一次。
        wal_bytes = _wal_bytes(conn)
        result["wal_bytes_before"] = wal_bytes
        try:
            truncate = result["vacuumed"] or wal_bytes >= max(0, int(wal_truncate_bytes))
            row = conn.execute(
                f"PRAGMA wal_checkpoint({'TRUNCATE' if truncate else 'PASSIVE'})").fetchone()
            result["checkpointed"] = row is None or int(row[0]) == 0
            result["wal_truncated"] = bool(truncate)
        except sqlite3.DatabaseError as exc:
            log.debug("wal checkpoint skipped: %s", exc)
        result["wal_bytes_after"] = _wal_bytes(conn)
        conn.commit()

        result["page_count_after"] = int(conn.execute("PRAGMA page_count").fetchone()[0])
        result["free_pages_after"] = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
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
