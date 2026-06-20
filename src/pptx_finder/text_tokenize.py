"""分词与归一化:基础召回 = 字级索引(FTS5 召回候选)+ 原文验证(精度)。

- 中文逐字、英文/数字整词(单字母信息量太低,按词避免召回爆炸)。
- 归一化:全半角(NFKC)+ 繁→简(OpenCC,装了才生效)+ 大小写(casefold)。
- normalize 刻意保留标点——供 search 用原文做「连续子串」精确验证。
- 写入索引与查询必须用同一套,否则搜不到。
"""
from __future__ import annotations

import re
import unicodedata

try:
    from opencc import OpenCC

    _T2S = OpenCC("t2s")

    def _to_simplified(s: str) -> str:
        return _T2S.convert(s)
except Exception:  # noqa: BLE001 OpenCC 不可用则跳过繁简(降级,不致命)
    def _to_simplified(s: str) -> str:
        return s


_PHRASE_RE = re.compile(r'"([^"]+)"')
# 字级 token:连续英文/数字算一个词、每个中文字算一个 token;其余(标点/空白)作分隔
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")


def normalize(text: str) -> str:
    """全半角(NFKC)+ 繁→简(OpenCC)+ 大小写(casefold)。保留标点供原文验证。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _to_simplified(text)
    return text.casefold()


def to_chars(text: str) -> str:
    """字级切词:中文逐字、英文数字整词,空格分隔。供 FTS5 索引 + 查询召回。"""
    return " ".join(_TOKEN_RE.findall(normalize(text)))


def tokenize(text: str) -> str:
    """索引建库用(indexer 调此名)。基础召回 = 字级。"""
    return to_chars(text)


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """拆查询为 (普通词, 精确短语)。普通词彼此 AND;精确短语整体匹配。"""
    phrases = [m.strip() for m in _PHRASE_RE.findall(query) if m.strip()]
    rest = _PHRASE_RE.sub(" ", query)
    terms = [t for t in rest.split() if t.strip()]
    return terms, phrases


def char_match(word: str) -> str:
    """单个词 → FTS5 字级相邻短语(子串)。单 token 直接匹配,多 token 要求相邻。"""
    toks = to_chars(word).split()
    if not toks:
        return ""
    if len(toks) == 1:
        return f'"{toks[0]}"'
    return '"' + " ".join(toks) + '"'  # phrase:位置相邻 = 子串


def build_fts_match(query: str) -> str:
    """整个 query → FTS5 MATCH(多词 AND,每词字级相邻短语)。"""
    terms, phrases = parse_query(query)
    clauses = [c for c in (char_match(w) for w in terms + phrases) if c]
    return " AND ".join(clauses)
