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
        "any_filename": "全部文件（仅文件名）",
    }.get(mode_key, "全部范围")


#: 「全部文件」范围的语法速查，轮流提示。这套语法只有用户知道才有价值——
#: `ext:pdf`、`size:>10mb` 这些能力藏着不说等于没做。
ALL_FILES_HINTS = (
    "*.pdf 通配符",
    "ext:pdf;docx 按扩展名",
    "size:>10mb 按大小",
    "dm:today 按修改时间",
    "a|b 或　!b 排除",
    "写 \\ 或 / 时按完整路径找",
    "folder: 只看文件夹",
)


def explain_all_files(query: str) -> QueryExplanation:
    """「全部文件」范围的查询说明：这一支走 Everything 语法，不是内容搜索那套。"""
    from . import namequery

    try:
        namequery.parse(query)
    except namequery.QueryError as exc:
        return QueryExplanation(
            summary=f"范围：{mode_label('any_filename')} · 语法有误：{exc}",
            terms=[], phrases=[], short_ascii_terms=[])
    # 按查询长度轮换提示，让用户逐渐把语法都见一遍，而不是永远只看到第一条
    hint = ALL_FILES_HINTS[len(query) % len(ALL_FILES_HINTS)]
    return QueryExplanation(
        summary=f"范围：{mode_label('any_filename')} · 按文件名与文件夹名查找 · 试试 {hint}",
        terms=[], phrases=[], short_ascii_terms=[])


def explain_query(
    query: str,
    mode_key: str = "all",
    *,
    case_sensitive: bool = False,
) -> QueryExplanation:
    if mode_key == "any_filename":
        return explain_all_files(query)
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
