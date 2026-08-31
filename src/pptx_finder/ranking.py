"""Shared search-result relevance ordering.

The database search and the UI's in-memory re-sort must use the same hard
tiers. Keeping the tuple here prevents the UI from undoing a correct backend
order after the user touches the sort controls.
"""
from __future__ import annotations

from typing import Any


SORT_KEYS = ("relevance", "recent", "name", "size", "path")


_MATCH_QUALITY_ORDER = {
    "filename_exact": 0,
    "filename_phrase": 1,
    # phrase = 原文中的连续完整词组；exact = 忽略分隔符后的全字匹配。
    # 因此前者刻意更强，并非“模糊短语压过精确匹配”。
    "content_phrase": 0,
    "content_exact": 1,
    "partial": 2,
    "filename_alias": 3,
    "content_alias": 3,
    "filename_fuzzy": 4,
    "content_fuzzy": 4,
}


def relevance_components(result: Any) -> tuple[int, int, int, float]:
    """Return the hard relevance tiers before recency/name tie-breakers.

    Priority is intentionally lexicographic, not a soft bonus:

    1. filename source before slide-content source;
    2. same-case match before case-folded fallback;
    3. contiguous/whole-query quality before separator-compacted or partial match;
    4. the existing BM25/name-quality/recency score.
    """
    name_hit = bool(getattr(result, "name_hit", False))
    match_kind = str(getattr(result, "match_kind", "partial") or "partial")
    # Strict content must beat even a high-scoring relaxed filename hit.  Fold
    # source and relaxation into one tier to preserve the historical 4-tuple
    # shape used by UI/tests: strict-name, strict-content, relaxed-name,
    # relaxed-content.
    relaxed = bool(getattr(result, "relaxed", False))
    source_tier = (2 if name_hit else 3) if relaxed else (0 if name_hit else 1)
    return (
        source_tier,
        0 if bool(getattr(result, "case_exact", False)) else 1,
        _MATCH_QUALITY_ORDER.get(match_kind, _MATCH_QUALITY_ORDER["partial"]),
        -float(getattr(result, "score", 0.0) or 0.0),
    )


def result_sort_key(result: Any, keys) -> tuple:
    keys = (keys,) if isinstance(keys, str) else tuple(keys or ())
    keys = tuple(dict.fromkeys(k for k in keys if k in SORT_KEYS)) or ("relevance",)
    out: list = []
    for key in keys:
        if key == "recent":
            out.append(-float(getattr(result, "mtime", 0.0) or 0.0))
        elif key == "name":
            out.append(str(getattr(result, "name", "") or "").casefold())
        elif key == "size":
            out.append(-int(getattr(result, "size", 0) or 0))
        elif key == "path":
            out.append(str(getattr(result, "path", "") or "").casefold())
        else:
            out.extend(relevance_components(result))
    out.extend((
        -float(getattr(result, "mtime", 0.0) or 0.0),
        str(getattr(result, "name", "") or "").casefold(),
    ))
    return tuple(out)


def _regroup_relevance(ordered: list) -> list:
    grouped: dict[str, list] = {}
    order: list[str] = []
    for result in ordered:
        gid = getattr(result, "group_id", None)
        key = f"g:{gid}" if gid is not None else f"s:{getattr(result, 'file_id', id(result))}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(result)
    return [result for key in order for result in grouped[key]]


def sort_results(results: list, keys, *, descending: bool = False) -> list:
    normalized = (keys,) if isinstance(keys, str) else tuple(keys or ())
    normalized = tuple(dict.fromkeys(k for k in normalized if k in SORT_KEYS)) or ("relevance",)

    def order_tier(items: list) -> list:
        ordered = sorted(items, key=lambda result: result_sort_key(result, normalized))
        if normalized[0] == "relevance":
            ordered = _regroup_relevance(ordered)
        return list(reversed(ordered)) if descending else ordered

    # A column sort is secondary to the strict/relaxed contract.  In
    # particular, reversing size/path order must never move an alias or typo
    # guess above a literal hit.  Split the tiers before applying direction so
    # ``descending`` cannot accidentally reverse this hard boundary.
    strict = [result for result in results if not bool(getattr(result, "relaxed", False))]
    relaxed = [result for result in results if bool(getattr(result, "relaxed", False))]
    return order_tier(strict) + order_tier(relaxed)
