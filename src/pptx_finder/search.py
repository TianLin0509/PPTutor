"""检索：FTS5 内容命中 + 文件名命中，按相关度+修改时间排序，生成高亮片段。"""
from __future__ import annotations

import sqlite3

from .models import FileResult, SearchHit
from .text_tokenize import build_fts_match, normalize, parse_query

# 排序权重
W_REL = 0.60      # 内容相关度（bm25）
W_RECENCY = 0.25  # 修改时间（越新越高）
NAME_BONUS = 0.50  # 文件名命中加分
MAX_HITS_PER_FILE = 10


def _snippet(conn: sqlite3.Connection, file_id: int, page_no: int,
             needles: list[str], width: int = 34) -> str:
    row = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? AND page_no=?",
        (file_id, page_no),
    ).fetchone()
    if not row or not row["raw_text"]:
        return ""
    raw = row["raw_text"].replace("\n", " ")
    low = normalize(raw)  # normalize 保持长度 1:1，可用同索引切回原文
    pos, hit_len = -1, 0
    for n in needles:
        if not n:
            continue
        i = low.find(n)
        if i >= 0:
            pos, hit_len = i, len(n)
            break
    if pos < 0:
        return raw[: width * 2].strip()
    start = max(0, pos - width)
    end = min(len(raw), pos + hit_len + width)
    rel = pos - start
    seg = raw[start:end]
    seg = seg[:rel] + "【" + seg[rel:rel + hit_len] + "】" + seg[rel + hit_len:]
    return ("…" if start > 0 else "") + seg + ("…" if end < len(raw) else "")


def search(conn: sqlite3.Connection, query: str, scope: str | None = None,
           limit: int = 200) -> list[FileResult]:
    terms, phrases = parse_query(query)
    if not terms and not phrases:
        return []
    match = build_fts_match(query)
    needles = [normalize(x) for x in (phrases + terms) if x.strip()]

    # 内容命中：file_id -> [(page_no, rank)]
    content: dict[int, list[tuple[int, float]]] = {}
    if match:
        for r in conn.execute(
            "SELECT file_id, page_no, bm25(pages_fts) AS rank "
            "FROM pages_fts WHERE pages_fts MATCH ? ORDER BY rank",
            (match,),
        ):
            content.setdefault(r["file_id"], []).append((r["page_no"], r["rank"]))

    # 文件名命中：name 包含所有普通词（AND）
    name_hits: set[int] = set()
    like_terms = [normalize(t) for t in (terms + phrases) if t.strip()]
    if like_terms:
        where = " AND ".join(["lower(name) LIKE ?"] * len(like_terms))
        params = [f"%{t}%" for t in like_terms]
        for r in conn.execute(f"SELECT id FROM files WHERE {where}", params):
            name_hits.add(r["id"])

    file_ids = set(content) | name_hits
    if not file_ids:
        return []

    rows: dict[int, sqlite3.Row] = {}
    qmarks = ",".join("?" * len(file_ids))
    for r in conn.execute(f"SELECT * FROM files WHERE id IN ({qmarks})", tuple(file_ids)):
        rows[r["id"]] = r

    # 收集中间结果用于归一化
    raw_items = []  # (row, hits, name_hit, best_rank)
    for fid in file_ids:
        row = rows.get(fid)
        if row is None:
            continue
        if scope and not row["path"].lower().startswith(scope.lower()):
            continue
        pages = sorted(content.get(fid, []), key=lambda x: x[1])  # rank 升序=更相关
        best_rank = pages[0][1] if pages else None
        hits = [
            SearchHit(pno, _snippet(conn, fid, pno, needles))
            for pno, _ in pages[:MAX_HITS_PER_FILE]
        ]
        raw_items.append((row, hits, fid in name_hits, best_rank))

    if not raw_items:
        return []

    ranks = [b for *_, b in raw_items if b is not None]
    rmin, rmax = (min(ranks), max(ranks)) if ranks else (0.0, 0.0)
    mtimes = [row["mtime"] for row, *_ in raw_items]
    mmin, mmax = min(mtimes), max(mtimes)

    def rel_norm(b: float | None) -> float:
        if b is None:
            return 0.0
        if rmax == rmin:
            return 1.0
        return (rmax - b) / (rmax - rmin)

    def rec_norm(m: float) -> float:
        if mmax == mmin:
            return 1.0
        return (m - mmin) / (mmax - mmin)

    results: list[FileResult] = []
    for row, hits, name_hit, best_rank in raw_items:
        score = (
            W_REL * rel_norm(best_rank)
            + W_RECENCY * rec_norm(row["mtime"])
            + (NAME_BONUS if name_hit else 0.0)
        )
        results.append(FileResult(
            file_id=row["id"], path=row["path"], name=row["name"], ext=row["ext"],
            mtime=row["mtime"], page_count=row["page_count"], status=row["status"],
            score=score, name_hit=name_hit, hits=hits,
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]
