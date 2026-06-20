"""分词与归一化:基础召回 = 字级索引(FTS5 召回候选)+ 原文验证(精度)。

- 中文逐字、英文/数字整词 + 长英数补字符 trigram(子串召回,不退化成按单字母→不召回爆炸)。
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
# 中文弯引号/书名号/方角引号 → ASCII 双引号，使其也能当短语定界符
# （修「用 “…”/「…」/《…》 包短语搜不到」——否则引号字符进了原文验证、原文里没有→0 结果）
_FANCY_QUOTES = str.maketrans({
    "“": '"', "”": '"',  # “ ”
    "「": '"', "」": '"',  # 「 」
    "『": '"', "』": '"',  # 『 』
    "《": '"', "》": '"',  # 《 》
    "〈": '"', "〉": '"',  # 〈 〉
})


def normalize(text: str) -> str:
    """全半角(NFKC)+ 繁→简(OpenCC)+ 大小写(casefold)。保留标点供原文验证。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _to_simplified(text)
    return text.casefold()


_TRI_MIN = 3  # 英数 token 长度 ≥ 此值才补字符 trigram（供子串召回）


def _base_tokens(text: str) -> list[str]:
    """基础切词:中文逐字、连续英文/数字整词。"""
    return _TOKEN_RE.findall(normalize(text))


def _trigrams(tok: str) -> list[str]:
    return [tok[i:i + 3] for i in range(len(tok) - 2)]


def to_chars(text: str) -> str:
    """基础切词(中文逐字、英数整词),空格分隔。"""
    return " ".join(_base_tokens(text))


def tokenize(text: str) -> str:
    """索引建库用(indexer 调此名)。基础 token + 长英数 token 的字符 trigram。

    trigram 追加在所有基础 token **之后**——让 GPT4 能子串命中 GPT4Turbo（英文/数字
    片段搜索），同时不打断中文/词在前段的相邻位置，phrase 子串匹配（如「明硕」）不受影响。
    精度仍由 search 的原文验证兜底（trigram 只负责把候选召回出来）。
    """
    base = _base_tokens(text)
    tris: list[str] = []
    for t in base:
        if len(t) >= _TRI_MIN and t.isascii():
            tris.extend(_trigrams(t))
    return " ".join(base + tris)


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """拆查询为 (普通词, 精确短语)。普通词彼此 AND;精确短语整体匹配。"""
    query = query.translate(_FANCY_QUOTES)  # 中文引号统一成 ASCII 引号再拆短语
    phrases = [m.strip() for m in _PHRASE_RE.findall(query) if m.strip()]
    rest = _PHRASE_RE.sub(" ", query)
    terms = [t for t in rest.split() if t.strip()]
    return terms, phrases


def char_match(word: str) -> str:
    """单个查询词 → FTS5 MATCH。
    纯英数且 ≥3:用字符 trigram AND（子串召回,如 GPT4 命中 GPT4Turbo,配原文验证保精度）；
    其余（含中文/短英数）:相邻短语（位置相邻 = 子串）。
    """
    toks = _base_tokens(word)
    if not toks:
        return ""
    if len(toks) == 1 and toks[0].isascii() and len(toks[0]) >= _TRI_MIN:
        return " AND ".join(f'"{g}"' for g in _trigrams(toks[0]))  # 子串召回
    if len(toks) == 1:
        return f'"{toks[0]}"'
    return '"' + " ".join(toks) + '"'  # phrase:位置相邻 = 子串


def build_fts_match(query: str) -> str:
    """整个 query → FTS5 MATCH(多词 AND,每词字级相邻短语)。"""
    terms, phrases = parse_query(query)
    clauses = [c for c in (char_match(w) for w in terms + phrases) if c]
    return " AND ".join(clauses)
