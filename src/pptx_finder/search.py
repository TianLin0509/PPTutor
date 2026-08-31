"""检索：FTS5 内容命中 + 文件名命中，按相关度+修改时间排序，生成高亮片段。"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
import heapq
from collections import defaultdict
from collections.abc import Callable
from difflib import SequenceMatcher
from functools import lru_cache

from . import cluster, namequery, search_relax
from .models import FileResult, SearchHit
from .ranking import (
    result_sort_key,
    sort_results as _core_sort_results,
)
from .text_tokenize import SEPARATOR_CLASS, char_match, normalize, parse_query

log = logging.getLogger(__name__)

# 排序权重
W_REL = 0.60      # 内容相关度（bm25）
W_RECENCY = 0.25  # 修改时间（越新越高）
NAME_BONUS = 0.50  # 文件名命中加分
MAX_HITS_PER_FILE = 10
# 「任意文件名」模式的文件名 FTS 候选上限：全盘盘点后 3000 会稳定截断召回
ANY_FILE_NAME_LIMIT = 100_000
# SQLite 单语句变量上限 32766：id IN (...) 查询按此分批，留足余量
_ID_IN_BATCH = 10_000

_EXT_RE = re.compile(r"\.(pptx?|potx?|ppsx?)$", re.IGNORECASE)
_CAND_SPLIT_RE = re.compile(rf"{SEPARATOR_CLASS}+")
_TEXT_CAND_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,40}|[0-9A-Za-z]{3,40}|[\u4e00-\u9fff]{2,12}")
# 只删标点/空白/下划线，保留所有 Unicode 字母数字。原先写死 [^0-9a-z 汉字]
# 会把 τ é ひ 一并抹掉，导致 query_exact 变空串、含希腊字母的命中永远评不到
# exact 档、只能落 partial（召回没问题，排序被压低）。
_COMPACT_RE = re.compile(rf"{SEPARATOR_CLASS}+")
_WS_RE = re.compile(r"\s+")
_ASCII_CASE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
def _stem_name(name: str) -> str:
    return _EXT_RE.sub("", name or "").strip()


def _case_rank_needles(query: str) -> list[str]:
    """Extract cheap case-bearing tokens used only as a ranking signal.

    Recall and strict case-sensitive filtering keep using the full normalized
    pipeline. Default relevance merely needs to distinguish ``FINAL`` from
    ``final``; doing a second OpenCC pass over up to 3000 slide bodies would add
    needless CPU. NFKC handles full-width Latin text, and uncased Chinese does
    not affect a case comparison.
    """
    normalized = unicodedata.normalize("NFKC", query or "")
    return [
        token
        for token in _ASCII_CASE_TOKEN_RE.findall(normalized)
        if any(ch.isalpha() for ch in token)
    ]


def _candidate_parts(text: str) -> list[str]:
    parts: list[str] = []
    for p in _CAND_SPLIT_RE.split(text or ""):
        p = p.strip()
        if len(normalize(p)) >= 2:
            parts.append(p)
    return parts


#: _suggest_score 里「包含关系」最多能加的分。上界剪枝要扣掉它才仍是必要条件。
_SUGGEST_MAX_BONUS = 0.18


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
    budget: search_relax.RelaxBudget | None = None,
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
    threshold = _suggest_threshold(target_norm)

    def add_candidate(value: str, *, weight: float) -> None:
        value = value.strip()
        cand_norm = normalize(value).strip()
        if len(cand_norm) < 2 or cand_norm == target_norm:
            return
        # 先用便宜的上界挡一道。real_quick_ratio()/quick_ratio() 都是 ratio() 的
        # 严格上界（只看长度、只看字符多重集），够不到门槛的候选压根不必跑
        # O(n·m) 的 ratio()。加分项只会让分数变高，所以扣掉最大加分再比就仍是
        # 必要条件，召回一条不少。
        # 真机 profile：这一层把 SequenceMatcher 从 67,263 次降到几百次。
        need = threshold - _SUGGEST_MAX_BONUS - weight
        if need > 0:
            matcher = SequenceMatcher(None, target_norm, cand_norm)
            if matcher.real_quick_ratio() < need or matcher.quick_ratio() < need:
                return
        score = _suggest_score(target_norm, cand_norm, weight)
        if score < threshold:
            return
        prev = best.get(cand_norm)
        if prev is None or score > prev[0]:
            best[cand_norm] = (score, value)

    budget = budget if budget is not None else search_relax.RelaxBudget()
    for row in conn.execute(
        "SELECT name FROM files ORDER BY mtime DESC LIMIT ?",
        (int(max_files),),
    ):
        if not budget.spend():
            break
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
        if not budget.spend():
            break
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
    separator = rf"{SEPARATOR_CLASS}*"
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
    """Return the user's whole single- or multi-word query for classification.

    Unquoted ``AI SP`` remains an AND query for recall, but the contiguous phrase
    receives a harder ranking tier. A single explicit quoted phrase gets the same
    treatment. Mixed quoted/unquoted clauses keep their existing AND semantics.
    """
    value = ""
    if not phrases and terms:
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
        db_error = str(e).casefold()
        # A lock/busy error is a database availability condition, not an empty
        # FTS result. Propagate it once so SearchWorker can clear the spinner
        # promptly; swallowing it makes the remaining fallback queries each
        # consume another busy-timeout window.
        if any(marker in db_error for marker in ("interrupted", "locked", "busy")):
            raise
        log.warning("FTS match failed %r: %s", m_and, e)

    # 多词只认「同一页」：所有词必须出现在同一页（上面同页 AND 已实现）。不再做「跨页放宽」
    # （A 在第 3 页、B 在第 50 页也算命中）——按用户要求，多词搜索更精准、避免结果过多。
    return content


def _search_strict(conn: sqlite3.Connection, query: str, scope: str | None = None,
                   limit: int = 200, exts: tuple[str, ...] | None = None,
                   case_sensitive: bool = False,
                   group_similar: bool = True,
                   name_limit: int = 3000,
                   name_only: bool = False,
                   sort_keys=None,
                   descending: bool = False) -> list[FileResult]:
    ext_filter = {e.lower() for e in exts} if exts else None  # 文件类型过滤；None=全部类型
    terms, phrases = parse_query(query)
    if not terms and not phrases:
        return []
    needles = [
        normalize(x, case_sensitive=case_sensitive)
        for x in (phrases + terms)
        if x.strip()
    ]
    case_needles = _case_rank_needles(query)
    has_case_signal = bool(case_needles)
    full_phrase = _full_query_phrase(
        terms, phrases, case_sensitive=case_sensitive)
    # 文件名搜索意图：整个 query 去扩展名，用于「完全/前缀匹配」加权（如搜 b.pptx → b）
    q_stem = normalize(query, case_sensitive=case_sensitive).strip()
    for _e in (".pptx", ".ppt"):
        if q_stem.casefold().endswith(_e):
            q_stem = q_stem[: -len(_e)]
            break

    # 字级召回 + 原文验证（精度）；多词必须在同一页共同出现。
    # name_only（任意文件名模式）：结果只保留文件名命中，内容召回纯空转，直接跳过。
    content = (
        {}
        if name_only
        else _recall(
            conn,
            terms + phrases,
            scope=scope,
            exts=exts,
            case_sensitive=case_sensitive,
        )
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
                    f"WHERE {' AND '.join(name_predicates)} "
                    # LIMIT 截断必须有确定性序：否则常见词的召回丢弃是随机的
                    f"ORDER BY f.mtime DESC, f.id LIMIT {max(1, int(name_limit))}",
                    tuple(name_params),
                )
            except sqlite3.OperationalError as e:
                db_error = str(e).casefold()
                if any(marker in db_error for marker in ("interrupted", "locked", "busy")):
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

    # 相似稿归组是高阶功能。关闭时连旧 minhash 表也不读，避免基础搜索
    # 为低频能力承担额外查询和折叠语义。
    gmap = cluster.group_map(conn) if group_similar else {}

    rows: dict[int, sqlite3.Row] = {}
    # id IN (...) 单语句变量上限 32766：任意文件名模式候选可达 10 万，必须分批
    id_list = list(file_ids)
    for i in range(0, len(id_list), _ID_IN_BATCH):
        chunk = id_list[i:i + _ID_IN_BATCH]
        qmarks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT * FROM files WHERE id IN ({qmarks})", chunk
        ):
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
        case_preserved_name = unicodedata.normalize("NFKC", row["name"] or "")
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
        if name_hit:
            match_kind = (
                "filename_exact" if filename_exact
                else "filename_phrase" if filename_phrase
                else "partial"
            )
        else:
            match_kind = (
                "content_phrase" if content_phrase
                else "content_exact" if content_exact
                else "partial"
            )
        if not has_case_signal:
            case_exact = True
        elif name_hit:
            case_exact = bool(
                case_needles
                and all(needle in case_preserved_name for needle in case_needles)
            )
        else:
            case_exact = any(
                all(
                    needle in unicodedata.normalize("NFKC", raw)
                    for needle in case_needles
                )
                for _page_no, _rank, raw, _raw_norm in pages
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
            match_kind=match_kind, case_exact=case_exact,
            content_hash=row["content_hash"] or "", group_id=gmap.get(row["id"]),
        ))

    # 相关度硬分层：文件名来源 > 内容来源；同来源内大小写一致 > 折叠命中，
    # 然后比较连续全字/分隔符压缩/部分命中的质量。
    # 修改时间仍进入 score，并作为最终 tie-breaker。这里与 UI 二次排序共用同一组件。
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

    ordered = _core_sort_results(
        results,
        sort_keys or ("relevance",),
        descending=bool(descending),
    )
    return _collapse_exact_duplicates(ordered)[:limit]


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


_RELAX_TRIGGER_MAX_STRICT = 40
#: 模糊（编辑距离）比别名贵好几个量级，只在严格几乎没结果时才兜底。
_FUZZY_TRIGGER_MAX_STRICT = 3
_RELAX_VARIANT_LIMIT = 4


def _mark_relaxed(result: FileResult, relaxation: search_relax.Relaxation) -> FileResult:
    result.relaxed = True
    result.relaxed_kind = relaxation.kind
    result.relaxed_query = relaxation.value
    result.match_kind = (
        f"filename_{relaxation.kind}" if result.name_hit
        else f"content_{relaxation.kind}"
    )
    return result


def _fuzzy_ppt_results(
    conn: sqlite3.Connection,
    query: str,
    *,
    scope: str | None,
    exts: tuple[str, ...] | None,
    group_similar: bool,
    name_only: bool,
    candidate_limit: int = 300,
    budget: search_relax.RelaxBudget | None = None,
) -> list[FileResult]:
    """Use strict n-gram anchors to find, then Unicode similarity to verify."""
    anchors = search_relax.fuzzy_anchors(query)
    if not anchors:
        return []
    budget = budget if budget is not None else search_relax.RelaxBudget()
    best: dict[int, tuple[float, FileResult]] = {}
    normalized_anchors = [normalize(anchor) for anchor in anchors]
    for anchor in anchors:
        if budget.exhausted:
            break
        for row in _search_strict(
            conn,
            anchor,
            scope=scope,
            limit=candidate_limit,
            exts=exts,
            case_sensitive=False,
            group_similar=group_similar,
            name_limit=candidate_limit,
            name_only=name_only,
        ):
            if budget.exhausted:
                break
            name_score = search_relax.fuzzy_name_score(
                query, row.name, budget=budget, anchors=anchors)
            content_score = 0.0
            fuzzy_hits: list[SearchHit] = []
            if not name_only:
                for hit in row.hits:
                    if budget.exhausted:
                        break
                    raw_row = conn.execute(
                        "SELECT raw_text FROM pages_raw WHERE file_id=? AND page_no=?",
                        (row.file_id, hit.page_no),
                    ).fetchone()
                    raw = str(raw_row["raw_text"] or "") if raw_row else ""
                    score = search_relax.fuzzy_text_score(
                        query, raw, budget=budget, anchors=anchors)
                    if score:
                        content_score = max(content_score, score)
                        fuzzy_hits.append(SearchHit(
                            hit.page_no,
                            _snippet_from_raw(raw, normalized_anchors),
                        ))
            score = max(name_score, content_score)
            if not score:
                continue
            row.name_hit = bool(name_score)
            row.hits = fuzzy_hits[:MAX_HITS_PER_FILE]
            if not row.name_hit and not row.hits:
                continue
            row.score += score
            row.case_exact = True
            _mark_relaxed(row, search_relax.Relaxation(query, "fuzzy"))
            previous = best.get(row.file_id)
            if previous is None or score > previous[0]:
                best[row.file_id] = (score, row)
    return [item[1] for item in best.values()]


def search(conn: sqlite3.Connection, query: str, scope: str | None = None,
           limit: int = 200, exts: tuple[str, ...] | None = None,
           case_sensitive: bool = False,
           group_similar: bool = True,
           name_limit: int = 3000,
           name_only: bool = False,
           sort_keys=None,
           descending: bool = False,
           enable_relaxed: bool = True,
           cancel=None) -> list[FileResult]:
    """Strict results plus bounded automatic aliases/typo correction.

    Relaxed results are concatenated after the complete strict tier even when
    the user selects another secondary sort.  This is the hard product promise:
    fuzzy recall can fill an empty/short result set, never steal the first row.
    """
    strict = _search_strict(
        conn, query, scope=scope, limit=limit, exts=exts,
        case_sensitive=case_sensitive, group_similar=group_similar,
        name_limit=name_limit, name_only=name_only,
        sort_keys=sort_keys, descending=descending,
    )
    if (
        not enable_relaxed
        or len(strict) >= max(1, int(limit))
        or len(strict) > _RELAX_TRIGGER_MAX_STRICT
        or not search_relax.is_relaxable_query(query)
    ):
        return strict

    # 三条打分路径（建议库 / PPT 名字 / PPT 正文）共用一份预算，
    # 免得各自限一次、合起来还是超。
    budget = search_relax.RelaxBudget(cancel=cancel)
    if (search_relax.alias_expansions(query)
            or len(strict) > _FUZZY_TRIGGER_MAX_STRICT):
        # 建议库要扫 1200 个文件名 + 1200 页正文，只在「严格几乎没结果」时才值得。
        suggestions = []
    else:
        try:
            suggestions = suggest_queries(conn, query, limit=3, budget=budget)
        except Exception:  # noqa: BLE001 relaxation must never break strict search
            log.debug("query suggestion fallback failed", exc_info=True)
            suggestions = []
    relaxations = search_relax.automatic_relaxations(query, suggestions)
    seen = {row.file_id for row in strict}
    # ``name_limit`` is a public candidate cap used by the legacy SQLite
    # filename path.  Automatic relaxation must not silently bypass it by
    # issuing several independent strict searches (or by using fuzzy anchors).
    # Content-only matches do not consume this budget, preserving the original
    # meaning of the parameter.
    remaining_name_slots = max(
        0,
        max(1, int(name_limit)) - sum(1 for row in strict if row.name_hit),
    )
    relaxed: list[FileResult] = []
    for relaxation in relaxations[:_RELAX_VARIANT_LIMIT]:
        rows = _search_strict(
            conn, relaxation.value, scope=scope, limit=limit, exts=exts,
            case_sensitive=False, group_similar=group_similar,
            name_limit=name_limit, name_only=name_only,
            sort_keys=sort_keys, descending=descending,
        )
        for row in rows:
            if row.file_id in seen:
                continue
            if row.name_hit and remaining_name_slots <= 0:
                continue
            seen.add(row.file_id)
            relaxed.append(_mark_relaxed(row, relaxation))
            if row.name_hit:
                remaining_name_slots -= 1

    # Alias/suggester expansion handles known terms.  N-gram anchors extend the
    # same automatic behavior to old filenames/slide bodies outside the bounded
    # suggestion sample, while final similarity prevents anchor-only false hits.
    #
    # 别名很便宜（几次普通检索），模糊匹配很贵（逐条编辑距离），所以两者的触发
    # 门槛不同：别名沿用 _RELAX_TRIGGER_MAX_STRICT，模糊只在「严格几乎没结果」
    # 时才兜底。原来两者共用 40 这个门槛，等于用户边打字边搜的每一个中间态都要
    # 付一次最贵的路径——真机实测 0.1 毫秒的查询被拖到 51 秒。
    for row in ([] if len(strict) > _FUZZY_TRIGGER_MAX_STRICT else _fuzzy_ppt_results(
        conn,
        query,
        scope=scope,
        exts=exts,
        group_similar=group_similar,
        name_only=name_only,
        budget=budget,
    )):
        if row.file_id in seen:
            continue
        if row.name_hit and remaining_name_slots <= 0:
            continue
        seen.add(row.file_id)
        relaxed.append(row)
        if row.name_hit:
            remaining_name_slots -= 1

    if not relaxed:
        return strict
    relaxed = _collapse_exact_duplicates(_core_sort_results(
        relaxed, sort_keys or ("relevance",), descending=bool(descending)))
    return (strict + relaxed)[:max(1, int(limit))]


# ---------------------------------------------------------------- 全盘文件名（namestore）

def search_names(store, query: str, *, limit: int = 200,
                 recall_limit: int = ANY_FILE_NAME_LIMIT,
                 scope: str | None = None,
                 exts: tuple[str, ...] | None = None,
                 case_sensitive: bool = False,
                 exists: Callable[[str], bool] = os.path.exists,
                 sort_keys=None,
                 descending: bool = False,
                 cancel=None,
                 enable_relaxed: bool = True) -> list[FileResult]:
    """「全部文件」范围的搜索：只认文件名，数据来自平铺索引而不是 SQLite。

    刻意长在 search.py 里而不是单开一个模块：打分要素（name_bonus、match_kind、
    case_exact、relevance_components 的排序口径）必须与内容搜索**逐条一致**，
    抄一份到别处的结局一定是两边慢慢漂开。这里直接复用上面那些私有函数。

    file_id 给的是「按名次递减的负数」。三个理由，缺一不可：
      · 负数 → 那两处 `WHERE file_id=?` 的查询（页标题、复制本页文字）必然查空，
        而不是撞上某个真实 PPT 的行、把别人的内容显示成这个文件的；
      · 每条不同 → search.py 与 ui/result_utils.py 都拿 f"s{file_id}" 当归组桶键，
        全体共用一个 id 会让所有文件名结果塌进同一个桶、被整体提到首条的位置，
        排序当场作废；
      · 不入库、不持久化 → 全应用没有任何地方存过 file_id，用完即弃是安全的。
    """
    # 这个范围用 Everything 那套语法（通配符 / ext: / size: / dm: / | / !），
    # 而不是 PPT 内容搜索的 parse_query——两者的用户习惯完全不同。
    # 语法写错时当零结果处理，不能让搜索线程抛异常。
    stores = [store] if not isinstance(store, (list, tuple)) else list(store)
    stores = [st for st in stores if st is not None]
    if not stores:
        return []
    keep = max(1, int(limit))
    ext_filter = {e.lower() for e in exts} if exts else None
    keys = (sort_keys,) if isinstance(sort_keys, str) else tuple(sort_keys or ("relevance",))
    explicit_sort = bool(keys and keys[0] != "relevance")

    # Later stores (the live overlay) replace older metadata/name state even if
    # the new name no longer matches the query.  Build only the small suffix
    # override sets; never materialize the 2M-entry main index.
    suffix_overrides: list[set[str]] = [set() for _ in stores]
    running: set[str] = set()
    for pos in range(len(stores) - 1, -1, -1):
        suffix_overrides[pos] = set(running)
        if pos > 0:
            st = stores[pos]
            running.update(st.path_keys())

    def row_from(st, i):
        path, name, size, mtime, is_dir = st.entry(i)
        if scope:
            try:
                if os.path.commonpath([
                    os.path.normcase(os.path.abspath(path)),
                    os.path.normcase(os.path.abspath(scope)),
                ]) != os.path.normcase(os.path.abspath(scope)):
                    return None
            except ValueError:
                return None
        ext = "" if is_dir else os.path.splitext(name)[1].lower()
        if ext_filter is not None and (is_dir or ext not in ext_filter):
            return None
        return (path, name, ext, int(size), float(mtime), bool(is_dir))

    def iter_rows(parsed, *, unlimited: bool):
        for pos, st in enumerate(stores):
            ordinals = (
                st.iter_search(parsed, scope=scope or "", cancel=cancel)
                if unlimited else
                st.search(parsed, limit=max(1, int(recall_limit)),
                          scope=scope or "", cancel=cancel)
            )
            for i in ordinals:
                row = row_from(st, i)
                if row is None:
                    continue
                path_key = os.path.normcase(row[0])
                if path_key in suffix_overrides[pos]:
                    continue
                yield row

    def build_results(rows, effective_query: str, *, relaxation=None,
                      fuzzy_scores: dict[str, float] | None = None):
        rows = list(rows)
        if not rows:
            return []
        terms, phrases = parse_query(effective_query)
        q_norm = (effective_query.strip() if case_sensitive and relaxation is None
                  else namequery.fold(effective_query).strip())
        q_stem = os.path.splitext(q_norm)[0] or q_norm
        full_phrase = _full_query_phrase(
            terms, phrases, case_sensitive=case_sensitive and relaxation is None)
        query_exact = _compact_normalized(
            effective_query, case_sensitive=case_sensitive and relaxation is None)
        case_needles = _case_rank_needles(effective_query)
        has_case_signal = bool(case_needles)
        mtimes = [row[4] for row in rows]
        mmin, mmax = min(mtimes), max(mtimes)

        def rec_norm(value: float) -> float:
            return 1.0 if mmax == mmin else (value - mmin) / (mmax - mmin)

        built: list[FileResult] = []
        for ordinal, (path, name, ext, size, mtime, is_dir) in enumerate(rows, start=1):
            normalized_name = namequery.fold(name)
            stem = os.path.splitext(normalized_name)[0] or normalized_name
            case_preserved_name = namequery.fold(name, case_sensitive=True)
            if q_norm and (normalized_name == q_norm or stem == q_stem):
                bonus = 2.0
            elif q_norm and (normalized_name.startswith(q_norm) or stem.startswith(q_stem)):
                bonus = 1.0
            else:
                bonus = NAME_BONUS
            if relaxation is not None:
                match_kind = f"filename_{relaxation.kind}"
            elif query_exact and query_exact in (
                    _COMPACT_RE.sub("", stem), _COMPACT_RE.sub("", normalized_name)):
                match_kind = "filename_exact"
            elif (_contains_full_phrase(stem, full_phrase)
                  or _contains_full_phrase(normalized_name, full_phrase)):
                match_kind = "filename_phrase"
            else:
                match_kind = "partial"
            case_exact = True if relaxation is not None or not has_case_signal else bool(
                case_needles and all(n in case_preserved_name for n in case_needles))
            extra = (fuzzy_scores or {}).get(os.path.normcase(path), 0.0)
            built.append(FileResult(
                file_id=-ordinal, path=path, name=name, ext=ext, mtime=mtime, size=size,
                page_count=0, status="filename_only",
                score=W_RECENCY * rec_norm(mtime) + bonus + extra,
                name_hit=True, hits=[], match_kind=match_kind, case_exact=case_exact,
                is_dir=is_dir,
                relaxed=relaxation is not None,
                relaxed_kind=relaxation.kind if relaxation is not None else "",
                relaxed_query=relaxation.value if relaxation is not None else "",
            ))
        return built

    def raw_sort_key(row, q_norm: str, q_stem: str):
        path, name, _ext, size, mtime, _is_dir = row
        out: list = []
        for key in keys:
            if key == "recent":
                out.append(-mtime)
            elif key == "name":
                out.append(name.casefold())
            elif key == "size":
                out.append(-size)
            elif key == "path":
                out.append(path.casefold())
            else:
                normalized = namequery.fold(name)
                stem = os.path.splitext(normalized)[0] or normalized
                quality = 0 if q_norm and (normalized == q_norm or stem == q_stem) else (
                    1 if q_norm and (normalized.startswith(q_norm) or stem.startswith(q_stem))
                    else 2
                )
                out.extend((0, 0, quality, -float(2 - quality)))
        out.extend((-mtime, name.casefold()))
        return tuple(out)

    def strict_for(effective_query: str, *, relaxation=None):
        try:
            parsed = namequery.parse(effective_query)
            if not parsed:
                return []
            if explicit_sort:
                pool = max(keep * 5, 1000)
                rows_iter = iter_rows(parsed, unlimited=True)
                chooser = heapq.nlargest if descending else heapq.nsmallest
                q_norm = namequery.fold(effective_query).strip()
                q_stem = os.path.splitext(q_norm)[0] or q_norm
                rows = chooser(pool, rows_iter,
                               key=lambda row: raw_sort_key(row, q_norm, q_stem))
            else:
                by_path = {
                    os.path.normcase(row[0]): row
                    for row in iter_rows(parsed, unlimited=False)
                }
                rows = list(by_path.values())
        except namequery.QueryError as exc:
            # Syntax mistakes remain a friendly zero-result query, but a
            # runtime timeout is operational failure and must reach the worker
            # so the UI can say so instead of lying that nothing matched.
            if "超时" in str(exc):
                raise
            log.info("bad all-files query %r: %s", effective_query, exc)
            return []
        except RuntimeError as exc:
            if "interrupted" in str(exc).casefold():
                raise
            log.info("bad all-files query %r: %s", effective_query, exc)
            return []
        results = build_results(rows, effective_query, relaxation=relaxation)
        results.sort(key=lambda result: result_sort_key(result, keys),
                     reverse=bool(descending))
        alive: list[FileResult] = []
        for result in results:
            if exists(result.path):
                alive.append(result)
                if len(alive) >= keep:
                    break
        return alive

    strict = strict_for(query)
    if (
        not enable_relaxed
        or len(strict) >= keep
        or len(strict) > _RELAX_TRIGGER_MAX_STRICT
        or not search_relax.is_relaxable_query(query, all_files=True)
    ):
        final = strict
    else:
        relaxed: list[FileResult] = []
        for relaxation in search_relax.automatic_relaxations(query)[:_RELAX_VARIANT_LIMIT]:
            relaxed.extend(strict_for(relaxation.value, relaxation=relaxation))

        # Corpus-independent typo/fuzzy fallback: n-grams only generate a small
        # candidate set; Unicode similarity is the final gate.
        anchors = search_relax.fuzzy_anchors(query)
        fuzzy_rows: dict[str, tuple] = {}
        fuzzy_scores: dict[str, float] = {}
        # 候选生成会查 cancel，但打分循环原来一次都不查——实测「1 秒时置取消」
        # 仍要跑满 7.3 秒。预算对象把墙钟、工作量、取消三件事一起管住。
        budget = search_relax.RelaxBudget(cancel=cancel)
        if anchors and len(strict) <= _FUZZY_TRIGGER_MAX_STRICT:
            for pos, st in enumerate(stores):
                if budget.exhausted:
                    break
                for i in st.fuzzy_candidates(anchors, cancel=cancel):
                    if budget.exhausted:
                        break
                    row = row_from(st, i)
                    if row is None:
                        continue
                    key = os.path.normcase(row[0])
                    if key in suffix_overrides[pos]:
                        continue
                    score = search_relax.fuzzy_name_score(
                        query, row[1], budget=budget, anchors=anchors)
                    if score and score > fuzzy_scores.get(key, 0.0):
                        fuzzy_rows[key] = row
                        fuzzy_scores[key] = score
            fuzzy_relaxation = search_relax.Relaxation(query, "fuzzy")
            fuzzy = build_results(
                fuzzy_rows.values(), query, relaxation=fuzzy_relaxation,
                fuzzy_scores=fuzzy_scores,
            )
            fuzzy.sort(key=lambda result: result_sort_key(result, keys),
                       reverse=bool(descending))
            relaxed.extend(fuzzy)

        seen = {os.path.normcase(result.path) for result in strict}
        deduped: list[FileResult] = []
        for result in relaxed:
            key = os.path.normcase(result.path)
            if key in seen or not exists(result.path):
                continue
            seen.add(key)
            deduped.append(result)
        # Strict tier is never mixed with relaxed sorting.
        final = (strict + deduped)[:keep]

    for rank, result in enumerate(final, start=1):
        result.file_id = -rank
    return final
