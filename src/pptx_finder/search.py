"""检索：FTS5 内容命中 + 文件名命中，按相关度+修改时间排序，生成高亮片段。"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from functools import lru_cache

from . import cluster
from .models import FileResult, SearchHit
from .text_tokenize import char_match, normalize, parse_query

log = logging.getLogger(__name__)

# 排序权重
W_REL = 0.60      # 内容相关度（bm25）
W_RECENCY = 0.25  # 修改时间（越新越高）
NAME_BONUS = 0.50  # 文件名命中加分
MAX_HITS_PER_FILE = 10

_EXT_RE = re.compile(r"\.(pptx?|potx?|ppsx?)$", re.IGNORECASE)
_CAND_SPLIT_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_TEXT_CAND_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,40}|[0-9A-Za-z]{3,40}|[\u4e00-\u9fff]{2,12}")
_COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_MATCH_KIND_ORDER = {
    "filename_phrase": 0,
    "content_phrase": 1,
    "filename_exact": 2,
    "content_exact": 3,
    "partial": 4,
}


def _stem_name(name: str) -> str:
    return _EXT_RE.sub("", name or "").strip()


def _candidate_parts(text: str) -> list[str]:
    parts: list[str] = []
    for p in _CAND_SPLIT_RE.split(text or ""):
        p = p.strip()
        if len(normalize(p)) >= 2:
            parts.append(p)
    return parts


def _suggest_threshold(target_norm: str) -> float:
    if target_norm.isascii():
        return 0.72 if len(target_norm) <= 6 else 0.66
    return 0.54 if len(target_norm) <= 4 else 0.50


def _suggest_score(target_norm: str, cand_norm: str, weight: float) -> float:
    if not target_norm or not cand_norm or target_norm == cand_norm:
        return 0.0
    ratio = SequenceMatcher(None, target_norm, cand_norm).ratio()
    if target_norm in cand_norm or cand_norm in target_norm:
        ratio += 0.18
    # Very long candidates often look close only because they contain common words.
    length_penalty = min(abs(len(cand_norm) - len(target_norm)) / max(len(target_norm), 1), 1.4) * 0.08
    return ratio + weight - length_penalty


def suggest_queries(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 3,
    max_files: int = 1200,
    max_pages: int = 1200,
) -> list[str]:
    """Return lightweight zero-result query suggestions.

    This is intentionally bounded and only called after a search misses. It uses
    filenames first, then a small recent-page text sample, so normal typing never
    pays this cost.
    """
    terms, phrases = parse_query(query)
    pieces = [p.strip() for p in (phrases + terms) if p.strip()]
    if not pieces:
        return []
    target = max(pieces, key=lambda p: len(normalize(p)))
    target_norm = normalize(target).strip()
    if len(target_norm) < 2 or (target_norm.isascii() and len(target_norm) < 3):
        return []

    best: dict[str, tuple[float, str]] = {}

    def add_candidate(value: str, *, weight: float) -> None:
        value = value.strip()
        cand_norm = normalize(value).strip()
        if len(cand_norm) < 2 or cand_norm == target_norm:
            return
        score = _suggest_score(target_norm, cand_norm, weight)
        if score < _suggest_threshold(target_norm):
            return
        prev = best.get(cand_norm)
        if prev is None or score > prev[0]:
            best[cand_norm] = (score, value)

    for row in conn.execute(
        "SELECT name FROM files ORDER BY mtime DESC LIMIT ?",
        (int(max_files),),
    ):
        stem = _stem_name(row["name"])
        add_candidate(stem, weight=0.12)
        for part in _candidate_parts(stem):
            add_candidate(part, weight=0.06)

    # Content terms help when the filename is generic ("template.pptx") but the
    # user mistyped an in-slide term. The LIMIT keeps this a fallback, not a scan.
    for row in conn.execute(
        "SELECT raw_text FROM pages_raw WHERE raw_text IS NOT NULL AND raw_text<>'' "
        "ORDER BY file_id DESC, page_no LIMIT ?",
        (int(max_pages),),
    ):
        raw = row["raw_text"] or ""
        for m in _TEXT_CAND_RE.finditer(raw):
            add_candidate(m.group(0), weight=0.0)

    suggestions: list[str] = []
    seen: set[str] = set()
    for _score, value in sorted(best.values(), key=lambda x: x[0], reverse=True):
        suggested = query.replace(target, value, 1) if target in query else value
        norm = normalize(suggested).strip()
        if norm and norm not in seen and norm != normalize(query).strip():
            seen.add(norm)
            suggestions.append(suggested)
        if len(suggestions) >= limit:
            break
    return suggestions


@lru_cache(maxsize=2048)
def _normalized_raw(raw: str) -> str:
    """Bounded hot-query cache; the raw string itself makes stale entries harmless."""
    return normalize(raw)


@lru_cache(maxsize=512)
def _normalized_raw_case_sensitive(raw: str) -> str:
    """Case-preserving counterpart used only after case-insensitive FTS recall."""
    return normalize(raw, case_sensitive=True)


def _normalized_for_verify(text: str, *, case_sensitive: bool) -> str:
    return (
        _normalized_raw_case_sensitive(text)
        if case_sensitive
        else _normalized_raw(text)
    )


def _compact_normalized(text: str, *, case_sensitive: bool = False) -> str:
    return _COMPACT_RE.sub("", normalize(text, case_sensitive=case_sensitive))


def _contains_compact_exact(text_norm: str, query_exact: str) -> bool:
    """Match the compact query through separators without ASCII prefix leaks."""
    if not query_exact:
        return False
    separator = r"[^0-9A-Za-z\u4e00-\u9fff]*"
    pattern = separator.join(re.escape(ch) for ch in query_exact)
    if query_exact[0].isascii() and query_exact[0].isalnum():
        pattern = r"(?<![0-9A-Za-z])" + pattern
    if query_exact[-1].isascii() and query_exact[-1].isalnum():
        pattern += r"(?![0-9A-Za-z])"
    return re.search(pattern, text_norm) is not None


def _full_query_phrase(
    terms: list[str],
    phrases: list[str],
    *,
    case_sensitive: bool,
) -> str:
    """Return the user's whole multi-word phrase for priority classification.

    Unquoted ``AI SP`` remains an AND query for recall, but the contiguous phrase
    receives a harder ranking tier. A single explicit quoted phrase gets the same
    treatment. Mixed quoted/unquoted clauses keep their existing AND semantics.
    """
    value = ""
    if not phrases and len(terms) >= 2:
        value = " ".join(terms)
    elif not terms and len(phrases) == 1:
        value = phrases[0]
    if not value:
        return ""
    return _WS_RE.sub(" ", normalize(value, case_sensitive=case_sensitive)).strip()


def _contains_full_phrase(text_norm: str, phrase_norm: str) -> bool:
    if not phrase_norm:
        return False
    text = _WS_RE.sub(" ", text_norm).strip()
    start = 0
    while True:
        pos = text.find(phrase_norm, start)
        if pos < 0:
            return False
        end = pos + len(phrase_norm)
        # FTS treats contiguous ASCII letters/digits as one token. Mirror that
        # boundary here so ``AI SP`` is not promoted by the prefix of ``AI SPARK``.
        # Chinese remains substring-based, preserving the existing character recall.
        left_ok = not (
            phrase_norm[0].isascii()
            and phrase_norm[0].isalnum()
            and pos > 0
            and text[pos - 1].isascii()
            and text[pos - 1].isalnum()
        )
        right_ok = not (
            phrase_norm[-1].isascii()
            and phrase_norm[-1].isalnum()
            and end < len(text)
            and text[end].isascii()
            and text[end].isalnum()
        )
        if left_ok and right_ok:
            return True
        start = pos + 1


def _snippet_from_raw(
    raw: str,
    needles: list[str],
    width: int = 34,
    *,
    raw_norm: str | None = None,
) -> str:
    if not raw:
        return ""
    raw = raw.replace("\n", " ")
    # 搜索召回阶段已经归一化过原文；复用它，避免为每条摘要再次跑 OpenCC。
    low = raw_norm.replace("\n", " ") if raw_norm is not None else _normalized_raw(raw)
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


def _snippet(conn: sqlite3.Connection, file_id: int, page_no: int,
             needles: list[str], width: int = 34) -> str:
    """Compatibility wrapper for callers/tests; search() uses the joined raw row."""
    row = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? AND page_no=?",
        (file_id, page_no),
    ).fetchone()
    return _snippet_from_raw(row["raw_text"] if row and row["raw_text"] else "", needles, width)


def _raw_contains(conn, fid: int, page: int, nw: str) -> bool:
    """原文验证：归一化后的页原文里有没有这个连续子串（尊重标点，保证精度）。"""
    row = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? AND page_no=?", (fid, page)
    ).fetchone()
    return bool(row and row["raw_text"] and nw in _normalized_raw(row["raw_text"]))


def _first_verified_page(conn, fid: int, clause: str, nw: str) -> int | None:
    """跨页放宽用：找该文件里含此词、且原文连续子串验证通过的某页（作代表页/片段）。"""
    try:
        for r in conn.execute(
            "SELECT page_no FROM pages_fts WHERE pages_fts MATCH ? AND file_id=? LIMIT 50",
            (clause, fid)):
            if _raw_contains(conn, fid, r["page_no"], nw):
                return r["page_no"]
    except sqlite3.OperationalError:
        pass
    return None


def _recall(
    conn,
    words: list[str],
    *,
    scope: str | None = None,
    exts: tuple[str, ...] | None = None,
    case_sensitive: bool = False,
) -> dict[int, list[tuple[int, float, str, str]]]:
    """字级 FTS5 召回 + 原文验证 → {file_id: [(page, rank)]}。

    同页（所有词都在一页）优先：用 FTS5 一次性 AND，**只返回全含的页**——天然被最稀有
    的词收窄，无需 per-term 限额（根治「常见词召回截断漏掉同时含稀有词的文件」假阴性）。
    多词无同页命中时放宽到「同一文件不同页」，低相关排后。原文验证保精度（不相邻不误中）。
    """
    pairs = [
        (char_match(w), normalize(w, case_sensitive=case_sensitive))
        for w in words
    ]
    pairs = [(c, nw) for c, nw in pairs if c]
    if not pairs:
        return {}
    clauses = [c for c, _ in pairs]
    nws = [nw for _, nw in pairs]
    # file_id -> (page_no, bm25 rank, raw text, normalized raw text)
    content: dict[int, list[tuple[int, float, str, str]]] = {}

    # 同页：一次 FTS5 AND，只命中所有词都在的页（选择性查询结果集很小，LIMIT 仅兜底）
    m_and = " AND ".join(clauses)
    # 类型/目录筛选必须在 LIMIT 前进入 SQL。旧实现先从全库截 3000 条、再在 Python
    # 里筛选，某一类型或目录被更高相关候选挤到第 3001 名后会稳定漏召回。
    predicates = ["pages_fts MATCH ?"]
    params: list[object] = [m_and]
    if scope:
        predicates.append("instr(lower(f.path), lower(?)) = 1")
        params.append(scope)
    ext_values = tuple(e.lower() for e in (exts or ()) if e)
    if ext_values:
        predicates.append(f"lower(f.ext) IN ({','.join('?' * len(ext_values))})")
        params.extend(ext_values)
    sql = (
        "SELECT pages_fts.file_id, pages_fts.page_no, bm25(pages_fts) AS rank, "
        "       pr.raw_text AS raw_text "
        "FROM pages_fts JOIN files AS f ON f.id=pages_fts.file_id "
        "JOIN pages_raw AS pr ON pr.file_id=pages_fts.file_id AND pr.page_no=pages_fts.page_no "
        f"WHERE {' AND '.join(predicates)} ORDER BY rank LIMIT 3000"
    )
    try:
        for r in conn.execute(sql, tuple(params)):
            fid, pg = r["file_id"], r["page_no"]
            raw = r["raw_text"] or ""
            raw_norm = _normalized_for_verify(raw, case_sensitive=case_sensitive)
            if all(nw in raw_norm for nw in nws):
                content.setdefault(fid, []).append((pg, r["rank"], raw, raw_norm))
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).casefold():
            raise
        log.warning("FTS match failed %r: %s", m_and, e)

    # 多词只认「同一页」：所有词必须出现在同一页（上面同页 AND 已实现）。不再做「跨页放宽」
    # （A 在第 3 页、B 在第 50 页也算命中）——按用户要求，多词搜索更精准、避免结果过多。
    return content


def search(conn: sqlite3.Connection, query: str, scope: str | None = None,
           limit: int = 200, exts: tuple[str, ...] | None = None,
           case_sensitive: bool = False) -> list[FileResult]:
    ext_filter = {e.lower() for e in exts} if exts else None  # 文件类型过滤；None=全部类型
    terms, phrases = parse_query(query)
    if not terms and not phrases:
        return []
    needles = [
        normalize(x, case_sensitive=case_sensitive)
        for x in (phrases + terms)
        if x.strip()
    ]
    full_phrase = _full_query_phrase(
        terms, phrases, case_sensitive=case_sensitive)
    # 文件名搜索意图：整个 query 去扩展名，用于「完全/前缀匹配」加权（如搜 b.pptx → b）
    q_stem = normalize(query, case_sensitive=case_sensitive).strip()
    for _e in (".pptx", ".ppt"):
        if q_stem.casefold().endswith(_e):
            q_stem = q_stem[: -len(_e)]
            break

    # 字级召回 + 原文验证（精度）；多词必须在同一页共同出现。
    content = _recall(
        conn,
        terms + phrases,
        scope=scope,
        exts=exts,
        case_sensitive=case_sensitive,
    )

    # 文件名命中：索引期维护 name_norm + file_names_fts。查询先走 FTS 收窄候选，再用
    # name_norm 字面子串做最终验证，避免每次搜索都把 files 全表拉到 Python 逐行 normalize。
    name_hits: set[int] = set()
    nterms = [
        normalize(t, case_sensitive=case_sensitive)
        for t in (terms + phrases)
        if t.strip()
    ]
    if nterms:
        clauses = [c for c in (char_match(t) for t in (terms + phrases)) if c]
        if clauses:
            match = " AND ".join(clauses)
            try:
                name_predicates = ["file_names_fts MATCH ?"]
                name_params: list[object] = [match]
                if scope:
                    name_predicates.append("instr(lower(f.path), lower(?)) = 1")
                    name_params.append(scope)
                if ext_filter:
                    name_predicates.append(
                        f"lower(f.ext) IN ({','.join('?' * len(ext_filter))})")
                    name_params.extend(sorted(ext_filter))
                name_rows = conn.execute(
                    "SELECT f.id, f.name, f.name_norm "
                    "FROM file_names_fts JOIN files AS f ON f.id=file_names_fts.file_id "
                    f"WHERE {' AND '.join(name_predicates)} LIMIT 3000",
                    tuple(name_params),
                )
            except sqlite3.OperationalError as e:
                if "interrupted" in str(e).casefold():
                    raise
                log.warning("filename fts match failed %r: %s", match, e)
                name_rows = ()
            for r in name_rows:
                nm = (
                    normalize(r["name"], case_sensitive=True)
                    if case_sensitive
                    else (r["name_norm"] or normalize(r["name"]))
                )
                if all(t in nm for t in nterms):
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
    raw_items = []  # (row, hits, name_hit, best_rank, recalled_pages)
    for fid in file_ids:
        row = rows.get(fid)
        if row is None:
            continue
        if scope and not row["path"].lower().startswith(scope.lower()):
            continue
        if ext_filter is not None and (row["ext"] or "").lower() not in ext_filter:
            continue
        pages = sorted(content.get(fid, []), key=lambda x: x[1])  # rank 升序=更相关
        best_rank = pages[0][1] if pages else None
        hits = [
            SearchHit(pno, _snippet_from_raw(raw, needles, raw_norm=raw_norm))
            for pno, _rank, raw, raw_norm in pages[:MAX_HITS_PER_FILE]
        ]
        raw_items.append((row, hits, fid in name_hits, best_rank, pages))

    if not raw_items:
        return []

    ranks = [item[3] for item in raw_items if item[3] is not None]
    rmin, rmax = (min(ranks), max(ranks)) if ranks else (0.0, 0.0)
    mtimes = [item[0]["mtime"] for item in raw_items]
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

    def name_bonus(name: str, name_norm: str | None = None) -> float:
        """文件名命中质量分级：完全匹配 > 前缀 > 普通包含（让搜 b.pptx 时 b.pptx 居首）。"""
        nstem = _stem_name(name_norm or normalize(name))
        if q_stem and nstem == q_stem:
            return 2.0   # 文件名完全匹配 → 绝对优先（盖过 内容0.6+时间0.25+包含0.5=1.35）
        if q_stem and nstem.startswith(q_stem):
            return 1.0   # 前缀匹配
        return NAME_BONUS  # 普通包含（0.50）

    query_exact = _compact_normalized(query, case_sensitive=case_sensitive)
    results: list[FileResult] = []
    for row, hits, name_hit, best_rank, pages in raw_items:
        normalized_name = (
            normalize(row["name"], case_sensitive=True)
            if case_sensitive
            else (row["name_norm"] or normalize(row["name"]))
        )
        filename_phrase = bool(
            name_hit
            and _contains_full_phrase(_stem_name(normalized_name), full_phrase)
        )
        content_phrase = bool(
            full_phrase
            and any(
                _contains_full_phrase(raw_norm, full_phrase)
                for *_head, raw_norm in pages
            )
        )
        filename_exact = bool(
            name_hit
            and query_exact
            and _COMPACT_RE.sub("", _stem_name(normalized_name)) == query_exact
        )
        content_exact = bool(
            query_exact
            and any(_contains_compact_exact(raw_norm, query_exact) for *_head, raw_norm in pages)
        )
        match_kind = (
            "filename_phrase" if filename_phrase
            else "content_phrase" if content_phrase
            else "filename_exact" if filename_exact
            else "content_exact" if content_exact
            else "partial"
        )
        score = (
            W_REL * rel_norm(best_rank)
            + W_RECENCY * rec_norm(row["mtime"])
            + (name_bonus(row["name"], normalized_name) if name_hit else 0.0)
        )
        results.append(FileResult(
            file_id=row["id"], path=row["path"], name=row["name"], ext=row["ext"],
            mtime=row["mtime"], size=row["size"], page_count=row["page_count"],
            status=row["status"], score=score, name_hit=name_hit, hits=hits,
            match_kind=match_kind,
            content_hash=row["content_hash"] or "", group_id=gmap.get(row["id"]),
        ))

    # 相关度硬分层：完整短语（文件名 > 内容）> 分隔符压缩后的全字 > 拆词部分命中。
    results.sort(key=lambda r: (
        _MATCH_KIND_ORDER.get(r.match_kind, _MATCH_KIND_ORDER["partial"]),
        -r.score,
        -r.mtime,
        r.name.casefold(),
    ))

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
    return _collapse_exact_duplicates(final)[:limit]


def _is_exact_hash(value: str) -> bool:
    return bool(value and value.startswith("sha256:") and len(value) == len("sha256:") + 64)


def _collapse_exact_duplicates(results: list[FileResult]) -> list[FileResult]:
    """Collapse byte-identical PPTX copies into the first-ranked result.

    Search ranking has already decided which copy is most relevant for this query.
    We keep that row as the actionable primary path, while preserving all locations
    in duplicate_paths for UI display.
    """
    by_hash: dict[str, list[FileResult]] = defaultdict(list)
    for r in results:
        if _is_exact_hash(r.content_hash):
            by_hash[r.content_hash].append(r)

    duplicate_sets = {h: rs for h, rs in by_hash.items() if len(rs) > 1}
    if not duplicate_sets:
        for r in results:
            r.duplicate_paths = []
        return results

    seen: set[str] = set()
    collapsed: list[FileResult] = []
    for r in results:
        group = duplicate_sets.get(r.content_hash)
        if not group:
            r.duplicate_paths = []
            collapsed.append(r)
            continue
        if r.content_hash in seen:
            continue
        seen.add(r.content_hash)
        ordered = [r] + [x for x in group if x is not r]
        r.duplicate_paths = [x.path for x in ordered]
        collapsed.append(r)
    return collapsed
