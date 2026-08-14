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

# 汉字区间:基本区 + 扩展 A + 兼容汉字。原先只有基本区(U+4E00–U+9FFF),
# 扩展 A 的生僻字(㐂 䶮 等姓氏用字)会被当分隔符整条丢掉。
CJK_RANGES = "㐀-䶿一-鿿豈-﫿"

# 「非内容字符」的唯一事实源:标点/空白/下划线才是分隔符,任何 Unicode 字母
# 数字都算内容。search 层的紧凑归一化与原文验证共用这一份——本次 bug 的成因
# 正是同一套假设在 text_tokenize / search 各写了一份、只改一处等于没改。
SEPARATOR_CLASS = r"[\W_]"

# 字级 token:
#   ① 连续英文/数字 = 一个词
#   ② 每个汉字 = 一个 token
#   ③ 其余任意 Unicode 字母/数字 = 逐字 token
# 第 ③ 条是 2026-07-28 补的:希腊字母(τ λ Δ σ μ)、带音标拉丁(é ü ñ)、假名、
# 韩文原本全部落在两条规则之外 → 被当分隔符丢弃 → char_match 返回空串 →
# build_fts_match 整个变成空 → 搜索在进 SQL 之前就废了。逐字成 token 与中文
# 的处理方式一致,精度仍由 search 的原文验证兜底。
# 2026-08-14:③ 从 [^\W\d_] 放宽到 [^\W_]——原先排除 \d,非 ASCII 十进制数字
# (阿拉伯-印度数字 ٤٥٦ 等)切不出 token,而 SEPARATOR_CLASS/compact 验证侧把
# 它们当内容,两侧口径不一,「会议室 ٤٥٦」这类名字搜不到。
_TOKEN_RE = re.compile(rf"[a-z0-9]+|[{CJK_RANGES}]|[^\W_]")
# 中文弯引号/书名号/方角引号 → ASCII 双引号，使其也能当短语定界符
# （修「用 “…”/「…」/《…》 包短语搜不到」——否则引号字符进了原文验证、原文里没有→0 结果）
_FANCY_QUOTES = str.maketrans({
    "“": '"', "”": '"',  # “ ”
    "「": '"', "」": '"',  # 「 」
    "『": '"', "』": '"',  # 『 』
    "《": '"', "》": '"',  # 《 》
    "〈": '"', "〉": '"',  # 〈 〉
})


def normalize(text: str, *, case_sensitive: bool = False) -> str:
    """全半角(NFKC)+ 繁→简(OpenCC)，默认再做大小写折叠。

    索引与 FTS 召回始终使用默认的不区分大小写模式；搜索结果的原文验证可传
    ``case_sensitive=True`` 保留原始大小写，因此无需为大小写开关重建第二套索引。
    标点会保留，供 search 做连续短语精确验证。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _to_simplified(text)
    return text if case_sensitive else text.casefold()


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


def build_fts_match_exact(query: str) -> str:
    """整个 query → FTS5 MATCH，**不补 trigram**（仅基础 token 相邻短语 = 子串）。

    用于「没有原文验证兜底」的场景（如跨版本历史搜索 version_pages_fts）：trigram 召回
    本就依赖 search 端原文验证去假阳性，那里无原文可验，用 trigram 会让「2026」误中只含
    「x202y026」碎片的历史页，故改精确相邻短语匹配。
    """
    terms, phrases = parse_query(query)
    clauses = []
    for w in terms + phrases:
        toks = _base_tokens(w)
        if toks:
            clauses.append('"' + " ".join(toks) + '"')  # 相邻短语=子串（精确，无 trigram）
    return " AND ".join(clauses)
