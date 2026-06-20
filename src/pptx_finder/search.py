"""检索：FTS5 内容命中 + 文件名命中，按相关度+修改时间排序，生成高亮片段。"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

from . import cluster
from .models import FileResult, SearchHit
from .text_tokenize import char_match, normalize, parse_query

log = logging.getLogger(__name__)

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


def _raw_contains(conn, fid: int, page: int, nw: str) -> bool:
    """原文验证：归一化后的页原文里有没有这个连续子串（尊重标点，保证精度）。"""
    row = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? AND page_no=?", (fid, page)
    ).fetchone()
    return bool(row and row["raw_text"] and nw in normalize(row["raw_text"]))


def _recall(conn, words: list[str]) -> dict[int, list[tuple[int, float]]]:
    """每词字级 FTS5 召回 + 原文验证 → {file_id: [(page, rank)]}。

    多词：同页（所有词都在）优先；无同页时放宽到「同一文件不同页」，低相关排后。
    """
    word_hits: list[dict[tuple[int, int], float]] = []
    for w in words:
        m = char_match(w)
        hits: dict[tuple[int, int], float] = {}
        if m:
            nw = normalize(w)
            try:
                for r in conn.execute(
                    "SELECT file_id, page_no, bm25(pages_fts) AS rank "
                    "FROM pages_fts WHERE pages_fts MATCH ? ORDER BY rank LIMIT 800", (m,)):
                    key = (r["file_id"], r["page_no"])
                    if key not in hits and _raw_contains(conn, r["file_id"], r["page_no"], nw):
                        hits[key] = r["rank"]  # FTS5 召回 + 原文确认连续子串
            except sqlite3.OperationalError as e:
                log.warning("FTS match failed %r: %s", m, e)
        word_hits.append(hits)
    if not word_hits:
        return {}
    content: dict[int, list[tuple[int, float]]] = {}
    common = set(word_hits[0])
    for wh in word_hits[1:]:
        common &= set(wh)
    for fid, pg in common:  # 同页：所有词都在这一页
        content.setdefault(fid, []).append((pg, min(wh[(fid, pg)] for wh in word_hits)))
    if len(words) > 1:  # 多词放宽：每词在该文件某页（跨页），且无同页命中
        files = {f for f, _ in word_hits[0]}
        for wh in word_hits[1:]:
            files &= {f for f, _ in wh}
        for fid in files:
            if fid not in content:
                pg = min(p for f, p in word_hits[0] if f == fid)
                content[fid] = [(pg, 1000.0)]  # 跨页命中，低相关排后
    return content


def search(conn: sqlite3.Connection, query: str, scope: str | None = None,
           limit: int = 200) -> list[FileResult]:
    terms, phrases = parse_query(query)
    if not terms and not phrases:
        return []
    needles = [normalize(x) for x in (phrases + terms) if x.strip()]
    # 文件名搜索意图：整个 query 去扩展名，用于「完全/前缀匹配」加权（如搜 b.pptx → b）
    q_stem = normalize(query).strip()
    for _e in (".pptx", ".ppt"):
        if q_stem.endswith(_e):
            q_stem = q_stem[: -len(_e)]
            break

    # 字级召回 + 原文验证（精度） + 多词（同页优先，无则放宽同文件）
    content = _recall(conn, terms + phrases)

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

    gmap = cluster.group_map(conn)  # file_id -> group_id（仅多成员版本组）

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

    def name_bonus(name: str) -> float:
        """文件名命中质量分级：完全匹配 > 前缀 > 普通包含（让搜 b.pptx 时 b.pptx 居首）。"""
        nstem = normalize(name)
        for _e in (".pptx", ".ppt"):
            if nstem.endswith(_e):
                nstem = nstem[: -len(_e)]
                break
        if q_stem and nstem == q_stem:
            return 2.0   # 文件名完全匹配 → 绝对优先（盖过 内容0.6+时间0.25+包含0.5=1.35）
        if q_stem and nstem.startswith(q_stem):
            return 1.0   # 前缀匹配
        return NAME_BONUS  # 普通包含（0.50）

    results: list[FileResult] = []
    for row, hits, name_hit, best_rank in raw_items:
        score = (
            W_REL * rel_norm(best_rank)
            + W_RECENCY * rec_norm(row["mtime"])
            + (name_bonus(row["name"]) if name_hit else 0.0)
        )
        results.append(FileResult(
            file_id=row["id"], path=row["path"], name=row["name"], ext=row["ext"],
            mtime=row["mtime"], size=row["size"], page_count=row["page_count"],
            status=row["status"], score=score, name_hit=name_hit, hits=hits,
            group_id=gmap.get(row["id"]),
        ))

    results.sort(key=lambda r: r.score, reverse=True)

    # 版本组内标记“最新版”：文件名含 终稿/定稿/final/最终 优先，否则修改时间最新
    members: dict[int, list[FileResult]] = defaultdict(list)
    for r in results:
        if r.group_id is not None:
            members[r.group_id].append(r)
    for ms in members.values():
        def _latest_key(r: FileResult):
            n = r.name.lower()
            kw = any(k in n for k in ("终稿", "定稿", "final", "最终"))
            return (kw, r.mtime)
        max(ms, key=_latest_key).is_latest = True

    # 同组聚集：组按其最高分成员首次出现的位置排列，组内按分降序
    grouped: dict[str, list[FileResult]] = defaultdict(list)
    order: list[str] = []
    for r in results:
        key = f"g{r.group_id}" if r.group_id is not None else f"s{r.file_id}"
        if key not in grouped:
            order.append(key)
        grouped[key].append(r)
    final: list[FileResult] = []
    for key in order:
        final.extend(grouped[key])
    return final[:limit]
