"""jieba 分词封装。写入索引与查询必须用同一套，否则搜不到。"""
from __future__ import annotations

import re

import jieba

jieba.setLogLevel(20)  # 关闭启动日志

_PHRASE_RE = re.compile(r'"([^"]+)"')


def normalize(text: str) -> str:
    """基础归一：全角→半角、英文小写。繁简归一为 P1（opencc）。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).lower()


def tokenize(text: str) -> str:
    """分词后用空格拼接，供 FTS5(unicode61) 索引/匹配。"""
    if not text:
        return ""
    text = normalize(text)
    return " ".join(w for w in jieba.cut(text) if w.strip())


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """拆查询为 (普通词, 精确短语)。
    普通词彼此 AND；精确短语整体匹配。
    """
    phrases = [m.strip() for m in _PHRASE_RE.findall(query) if m.strip()]
    rest = _PHRASE_RE.sub(" ", query)
    rest = normalize(rest)
    terms = [t for t in rest.split() if t.strip()]
    return terms, phrases


def build_fts_match(query: str) -> str:
    """把用户 query 转成 FTS5 MATCH 表达式。
    - 普通词：各自 jieba 分词后的 token 组成短语，词间 AND
    - 引号短语：jieba 分词后作为 FTS 短语 "a b c"
    """
    terms, phrases = parse_query(query)
    clauses: list[str] = []
    for t in terms:
        toks = tokenize(t).split()
        if not toks:
            continue
        if len(toks) == 1:
            clauses.append(f'"{toks[0]}"')
        else:
            # 一个中文词被 jieba 再切成多 token 时，要求相邻出现
            clauses.append('"' + " ".join(toks) + '"')
    for p in phrases:
        toks = tokenize(p).split()
        if toks:
            clauses.append('"' + " ".join(toks) + '"')
    return " AND ".join(clauses)
