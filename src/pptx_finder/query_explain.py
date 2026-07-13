"""User-facing explanation for how a search query will be interpreted."""
from __future__ import annotations

from dataclasses import dataclass

from .text_tokenize import parse_query


@dataclass(frozen=True)
class QueryExplanation:
    summary: str
    terms: list[str]
    phrases: list[str]
    short_ascii_terms: list[str]


def mode_label(mode_key: str) -> str:
    return {
        "filename": "仅文件名",
        "content": "仅内容",
    }.get(mode_key, "全部范围")


def explain_query(
    query: str,
    mode_key: str = "all",
    *,
    case_sensitive: bool = False,
) -> QueryExplanation:
    terms, phrases = parse_query(query)
    short_ascii = [
        t for t in terms
        if len(t) < 3 and t.isascii() and t.isalnum()
    ]

    parts: list[str] = [f"范围：{mode_label(mode_key)}"]
    if terms:
        parts.append("同页包含：" + " + ".join(terms))
    if phrases:
        parts.append("精确短语：" + " / ".join(phrases))
    if not phrases and len(terms) >= 2:
        parts.append("完整短语优先：" + " ".join(terms))
    if short_ascii:
        parts.append("短英文/数字按完整词匹配：" + "、".join(short_ascii))
    if len(terms) + len(phrases) > 1:
        parts.append("多条件为 AND，优先命中同一页")
    parts.append("区分大小写" if case_sensitive else "不区分大小写")
    return QueryExplanation(
        summary=" · ".join(parts),
        terms=terms,
        phrases=phrases,
        short_ascii_terms=short_ascii,
    )


def suggestion_keys(query: str, mode_key: str = "all") -> list[str]:
    terms, phrases = parse_query(query)
    keys: list[str] = []
    if phrases:
        keys.append("unquote")
    if len(terms) + len(phrases) > 1:
        keys.append("fewer")
    if mode_key != "all":
        keys.append("allmode")
    if mode_key != "filename":
        keys.append("filename")
    return keys
