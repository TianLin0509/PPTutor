"""Shared search-result relevance ordering.

The database search and the UI's in-memory re-sort must use the same hard
tiers. Keeping the tuple here prevents the UI from undoing a correct backend
order after the user touches the sort controls.
"""
from __future__ import annotations

from typing import Any


_MATCH_QUALITY_ORDER = {
    "filename_exact": 0,
    "filename_phrase": 1,
    # phrase = 原文中的连续完整词组；exact = 忽略分隔符后的全字匹配。
    # 因此前者刻意更强，并非“模糊短语压过精确匹配”。
    "content_phrase": 0,
    "content_exact": 1,
    "partial": 2,
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
    return (
        0 if name_hit else 1,
        0 if bool(getattr(result, "case_exact", False)) else 1,
        _MATCH_QUALITY_ORDER.get(match_kind, _MATCH_QUALITY_ORDER["partial"]),
        -float(getattr(result, "score", 0.0) or 0.0),
    )
