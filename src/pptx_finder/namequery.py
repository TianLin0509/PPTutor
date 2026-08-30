# -*- coding: utf-8 -*-
"""「全部文件」范围的查询语法：照搬 Everything。

## 为什么单开一个解析器

PPT 内容搜索那套语法（`parse_query`）是为「搜幻灯片里写过的字」设计的：空格分词、
引号短语、字级 FTS 召回。它**一个字都不能动**——动了就影响 PPT 相关功能。

而按名字找文件是另一套习惯，用户从 Everything 带过来的肌肉记忆：`*.pdf`、
`ext:docx`、`size:>10mb`、`a|b`、`!temp`。本模块只服务「全部文件」范围。

## 支持的语法

    空格          与（都要满足）
    |             或
    !x            非
    < >  ( )      分组
    "..."         按字面匹配，里面的 * ? 不当通配符
    * ?           通配符；**一旦用了通配符，就变成匹配整个名字**（与 Everything 一致：
                  `abc` 是包含，`abc*` 是以 abc 开头）
    ext:pdf;docx  扩展名（分号分隔多个）
    size:>10mb    大小，支持 > >= < <= = 、a..b 区间、以及 empty/tiny/small/
                  medium/large/huge/gigantic 这些命名档
    dm:today      修改时间，支持 today/yesterday/thisweek/lastweek/thismonth/
                  lastmonth/thisyear/lastyear、YYYY[-MM[-DD]]、比较与区间
    path:foo      拿完整路径匹配（查询里出现 \\ 或 / 时自动对该词启用）
    file: folder: 只要文件 / 只要文件夹
    regex:^a.*z$  正则
    case:x        该词区分大小写
    ww:x          全词匹配
    empty:        空文件（大小为 0）

未实现的 Everything 功能在 KNOWN_GAPS 里逐条列着，不装作支持。
"""
from __future__ import annotations

import calendar
import datetime as _dt
import fnmatch
import re
import unicodedata
from dataclasses import dataclass, field

from .db import sqlite_safe_text
from .text_tokenize import normalize

#: 明确没做、也不假装做了的 Everything 功能。写在这里是为了别让人以为漏了。
KNOWN_GAPS = (
    "dupe: / attrib: / content: —— 需要额外索引，超出「按名字找」的范畴",
    "parents: / child: / infolder: —— 目录层级函数，用 path: 可覆盖绝大多数场景",
)

_MAX_REGEX_LEN = 500

#: 变音符号（Unicode 的 Mn 类：组合用重音、变音点等）
_COMBINING = "Mn"


def fold(text: str, *, case_sensitive: bool = False) -> str:
    """按名字找文件专用的归一化：在通用 normalize 之上再去掉变音符号。

    Everything 有「Ignore Diacritics」，打 `resume` 要能找到 `résumé`。通用的
    `normalize`（NFKC + 繁简 + 大小写）**不能**加这一步——它同时服务 PPT 内容
    搜索，改了就会改变 PPT 的搜索行为，而那是不能碰的。所以只在这一层加。

    索引与查询必须调同一个函数，否则两边口径不一致就等于搜不到。

    case_sensitive=True 保留大小写但**照样折变音符号**：排序时要判断「用户打的
    大小写是否与文件名一致」，那一步如果拿没折过的名字去比，`resume` 会被判成
    「与 résumé.pdf 大小写不符」，于是明明是完全匹配却被降档压到后面。
    """
    base = normalize(sqlite_safe_text(text), case_sensitive=case_sensitive)
    if base.isascii():
        return base                      # 绝大多数文件名走这条捷径，不进 NFD
    decomposed = unicodedata.normalize("NFD", base)
    stripped = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != _COMBINING)
    return unicodedata.normalize("NFC", stripped)

# 命名大小档，取值与 Everything 一致（字节）
_NAMED_SIZES = {
    "empty": (0, 0),
    "tiny": (0, 10 * 1024),
    "small": (10 * 1024, 100 * 1024),
    "medium": (100 * 1024, 1024 * 1024),
    "large": (1024 * 1024, 16 * 1024 * 1024),
    "huge": (16 * 1024 * 1024, 128 * 1024 * 1024),
    "gigantic": (128 * 1024 * 1024, 1 << 62),
}
_SIZE_UNITS = {
    "": 1, "b": 1, "kb": 1024, "k": 1024, "mb": 1024 ** 2, "m": 1024 ** 2,
    "gb": 1024 ** 3, "g": 1024 ** 3, "tb": 1024 ** 4, "t": 1024 ** 4,
}
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]*)$")


class QueryError(ValueError):
    """查询写错了。调用方应当把它当「零结果 + 提示」处理，而不是崩溃。"""


# ---------------------------------------------------------------- 词法

_FUNCTIONS = {
    "ext", "size", "dm", "datemodified", "path", "file", "files", "folder",
    "folders", "regex", "case", "nocase", "ww", "wholeword", "empty",
}


def _split_tokens(text: str) -> list[str]:
    """切成词。引号内原样保留，`<> ()` 和 `|` 单独成词。"""
    out: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
            continue
        if quoted:
            buf.append(ch)
            continue
        if ch in "<>" and buf and ":" in "".join(buf):
            # `size:>10mb` / `dm:<2026` 里的 < > 是比较符，不是分组括号。
            # 判据是「当前这个词已经写过冒号」＝正在写某个函数的取值。
            buf.append(ch)
            continue
        if ch in "<>()|":
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
            continue
        if ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


# ---------------------------------------------------------------- 节点

@dataclass
class _Node:
    def match(self, rec: "Record") -> bool:      # pragma: no cover - 抽象
        raise NotImplementedError

    def literals(self) -> list[list[str]] | None:
        """每个 OR 分支必须命中的字面串。None = 这一支没法预筛，只能全扫。"""
        raise NotImplementedError                # pragma: no cover

    def needs(self) -> frozenset[str]:
        """这条查询会读哪些字段。

        全扫时靠它决定能不能走「只读 20 字节定长记录」的快路：只看 size/mtime/
        是否目录的查询（size: dm: folder:）根本不必解码任何字符串，真机 200 万条
        实测 6.4 秒 → 0.3 秒。
        """
        raise NotImplementedError                # pragma: no cover


@dataclass
class Record:
    """一条待判定的记录。字段都是判定所需的最小集合。"""

    name: str          # 原始名字
    name_norm: str     # 归一化名字（与索引里存的同口径）
    path: str          # 完整路径
    size: int
    mtime: int
    is_dir: bool
    _path_norm: str | None = None

    @property
    def path_norm(self) -> str:
        if self._path_norm is None:
            self._path_norm = fold(self.path)
        return self._path_norm


@dataclass
class _And(_Node):
    parts: list[_Node]

    def match(self, rec):
        return all(p.match(rec) for p in self.parts)

    def literals(self):
        """与：任意一项的必要条件都能拿来预筛，所以能用几项就用几项。

        内嵌 OR（`got` 有多支）只能整支用或整支不用，混进 merged 会把「或」
        错当成「与」。所以跳过它——但**不能因此放弃整条查询**：
        `<a|b> ext:png` 里 ext 那项照样是必要条件，早期版本在这里直接 return None，
        结果这条查询退化成全扫，真机 200 万条要 3.8 秒。
        """
        merged: list[str] = []
        fallback: list[list[str]] | None = None
        for p in self.parts:
            got = p.literals()
            if got is None:
                continue
            if len(got) == 1:
                merged.extend(got[0])
                continue
            if fallback is None or len(got) < len(fallback):
                fallback = got
        if merged:
            return [merged]
        return fallback

    def needs(self):
        return frozenset().union(*(p.needs() for p in self.parts))


@dataclass
class _Or(_Node):
    parts: list[_Node]

    def match(self, rec):
        return any(p.match(rec) for p in self.parts)

    def literals(self):
        branches: list[list[str]] = []
        for p in self.parts:
            got = p.literals()
            if not got:
                return None              # 只要有一支没法预筛，整条查询就得全扫
            branches.extend(got)
        return branches

    def needs(self):
        return frozenset().union(*(p.needs() for p in self.parts))


@dataclass
class _Not(_Node):
    part: _Node

    def match(self, rec):
        return not self.part.match(rec)

    def literals(self):
        return None                      # 取反之后字面串不再是必要条件

    def needs(self):
        return self.part.needs()


@dataclass
class _Term(_Node):
    """一个名字/路径匹配项。"""

    raw: str
    on_path: bool = False
    case_sensitive: bool = False
    whole_word: bool = False
    literal: bool = False                # 引号里的：* ? 不当通配符
    _rx: re.Pattern | None = field(default=None, repr=False)
    _needle: str = field(default="", repr=False)
    _anchored: bool = field(default=False, repr=False)

    def __post_init__(self):
        text = _unify_seps(self.raw) if self.on_path else self.raw
        wild = (not self.literal) and ("*" in text or "?" in text)
        if wild:
            # Everything 的规则：一旦用了通配符，就是匹配**整个名字**，
            # 所以 abc 是包含、abc* 是以 abc 开头。fnmatch.translate 只在尾部
            # 加了 \Z，起点要靠 match() 来锚——用 search() 的话 `report*` 会
            # 命中 my-report.pdf，「以…开头」就废了。
            pattern = fnmatch.translate(
                text if self.case_sensitive else fold(text))
            self._rx = re.compile(pattern)
            self._anchored = True
        elif self.whole_word:
            body = re.escape(text if self.case_sensitive else fold(text))
            self._rx = re.compile(rf"(?<![0-9A-Za-z]){body}(?![0-9A-Za-z])")
        else:
            self._needle = text if self.case_sensitive else fold(text)

    def _subject(self, rec: Record) -> str:
        if self.on_path:
            # 两边统一成 `/`：用户打 `work\汇报` 还是 `work/汇报` 都该命中同一批
            return _unify_seps(rec.path if self.case_sensitive else rec.path_norm)
        return rec.name if self.case_sensitive else rec.name_norm

    def match(self, rec):
        subject = self._subject(rec)
        if self._rx is not None:
            if self._anchored:
                return self._rx.match(subject) is not None
            return self._rx.search(subject) is not None
        return self._needle in subject

    def literals(self):
        if self._needle:
            # 路径匹配的字面串同样出现在名字块之外，不能拿来预筛名字段
            if self.on_path:
                return None
            # 预筛扫的是**归一化后**的名字块（小写、折过变音符号）。区分大小写的
            # 词若拿原样大小写去那里找，必然一条都找不到——`case:README` 因此
            # 恒返回 0 条，而且不报错。预筛只需要是**必要条件**，而
            # 「原样命中」蕴含「折过之后也命中」，所以这里改用折过的针；
            # 真正的大小写判定仍由下面的 match() 逐条做。
            needle = self._needle if not self.case_sensitive else fold(self._needle)
            return [[needle]] if needle else None
        if self._rx is not None and not self.on_path:
            # 通配符里夹着的固定片段仍然是必要条件：`*report*.pdf` 必含 report 和 .pdf
            # 同上，片段也必须按 fold 折过再拿去预筛。原来这里用的是 normalize()，
            # 它不折变音符号，而名字块折了——于是 `*café*` 拿 café 去一堆 cafe 里找，
            # 恒 0 条且不报错。区分大小写的通配符同理可以预筛，折过就行。
            chunks = [fold(c) for c in re.split(r"[*?]+", self.raw) if len(c) >= 2]
            chunks = [c for c in chunks if c]
            if chunks:
                return [chunks]
        return None

    def needs(self):
        return frozenset({"path" if self.on_path else "name"})


@dataclass
class _Regex(_Node):
    pattern: str
    _rx: re.Pattern | None = field(default=None, repr=False)

    def __post_init__(self):
        if len(self.pattern) > _MAX_REGEX_LEN:
            raise QueryError("正则太长")
        try:
            self._rx = re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise QueryError(f"正则写错了：{exc}") from exc

    def match(self, rec):
        return self._rx.search(rec.name) is not None

    def literals(self):
        return None

    def needs(self):
        return frozenset({"name"})


@dataclass
class _Ext(_Node):
    exts: tuple[str, ...]

    def match(self, rec):
        if rec.is_dir:
            return False
        dot = rec.name.rfind(".")
        return dot >= 0 and rec.name[dot + 1:].casefold() in self.exts

    def literals(self):
        # `.pdf` 一定出现在名字里，可以拿来预筛；多个扩展名就是多支 OR
        return [["." + e] for e in self.exts if e]

    def needs(self):
        return frozenset({"name", "is_dir"})


@dataclass
class _Kind(_Node):
    want_dir: bool

    def match(self, rec):
        return rec.is_dir is self.want_dir

    def literals(self):
        return None

    def needs(self):
        return frozenset({"is_dir"})


@dataclass
class _Range(_Node):
    """数值区间判定，size 与 dm 共用。lo/hi 都是闭区间。"""

    lo: int
    hi: int
    field_name: str

    def match(self, rec):
        value = rec.size if self.field_name == "size" else rec.mtime
        return self.lo <= value <= self.hi

    def literals(self):
        return None

    def needs(self):
        return frozenset({self.field_name})


@dataclass
class Query:
    root: _Node | None
    text: str

    def match(self, rec: Record) -> bool:
        return True if self.root is None else self.root.match(rec)

    def prefilter(self) -> list[list[str]] | None:
        """预筛用的字面串。返回 [[分支1的必含串...], [分支2...]]；None = 只能全扫。"""
        return None if self.root is None else self.root.literals()

    def needs(self) -> frozenset[str]:
        """这条查询读哪些字段。全扫时据此选快路。"""
        return frozenset() if self.root is None else self.root.needs()

    def regex_candidates(self):
        """「与」链上那个正则，用于在名字块上一次性扫出候选。

        正则得逐条拿名字比，200 万条要 2 秒。但名字块本身是 '\\n' 分隔的，用
        MULTILINE 在整块上跑一遍，`^`/`$` 的含义与「对单个名字跑」完全一致
        （`.` 本来就不跨行），于是可以一次扫完再回查是第几条。
        扫出来的是候选，仍会逐条复核，所以即使某个模式跨行匹配也不会出错。
        """
        node = self.root
        if isinstance(node, _And):
            node = next((p for p in node.parts if isinstance(p, _Regex)), None)
        if not isinstance(node, _Regex):
            return None
        try:
            return re.compile(node.pattern.encode("utf-8", "surrogatepass"),
                              re.IGNORECASE | re.MULTILINE)
        except (re.error, UnicodeEncodeError):
            return None

    def path_literals(self) -> list[str]:
        """「与」链上那些拿完整路径匹配的字面串。

        路径字面串不在名字块里，普通预筛用不上它们；但目录只有 25 万个且被大量
        共享，拿它们先把目录筛一遍，就能把候选从 200 万压到很小一撮。
        取反、通配符、正则一律不参与——它们不是必要条件。
        """
        out: list[str] = []
        _collect_path_literals(self.root, out)
        return out

    #: 只读这些字段的查询可以完全不解码字符串
    METADATA_ONLY = frozenset({"size", "mtime", "is_dir"})

    def __bool__(self) -> bool:
        return self.root is not None


# ---------------------------------------------------------------- 解析

def _parse_size(value: str) -> _Node:
    v = value.strip().casefold()
    if not v:
        raise QueryError("size: 后面是空的")
    if v in _NAMED_SIZES:
        lo, hi = _NAMED_SIZES[v]
        return _Range(lo, hi if v == "empty" else max(lo, hi - 1), "size")
    return _numeric_filter(v, "size", _size_to_bytes)


def _size_to_bytes(token: str) -> int:
    m = _SIZE_RE.match(token.strip())
    if not m:
        raise QueryError(f"看不懂的大小：{token}")
    number, unit = m.group(1), m.group(2)
    if unit not in _SIZE_UNITS:
        raise QueryError(f"看不懂的单位：{unit}")
    return int(float(number) * _SIZE_UNITS[unit])


def _numeric_filter(v: str, field_name: str, to_value) -> _Node:
    biggest = 1 << 62
    if ".." in v:
        lo_s, hi_s = v.split("..", 1)
        lo = to_value(lo_s) if lo_s.strip() else 0
        hi = to_value(hi_s) if hi_s.strip() else biggest
        return _Range(lo, hi, field_name)
    for op in (">=", "<=", ">", "<", "="):
        if v.startswith(op):
            value = to_value(v[len(op):])
            if op == ">=":
                return _Range(value, biggest, field_name)
            if op == ">":
                return _Range(value + 1, biggest, field_name)
            if op == "<=":
                return _Range(0, value, field_name)
            if op == "<":
                return _Range(0, max(0, value - 1), field_name)
            return _Range(value, value, field_name)
    value = to_value(v)
    return _Range(value, value, field_name)


def _day_bounds(day: _dt.date) -> tuple[int, int]:
    start = _dt.datetime(day.year, day.month, day.day)
    return int(start.timestamp()), int(start.timestamp()) + 86399


def _parse_date(value: str, *, now: _dt.datetime | None = None) -> _Node:
    v = value.strip().casefold()
    if not v:
        raise QueryError("dm: 后面是空的")
    now = now or _dt.datetime.now()
    today = now.date()
    named = {
        "today": (today, today),
        "yesterday": (today - _dt.timedelta(days=1), today - _dt.timedelta(days=1)),
        "thisweek": (today - _dt.timedelta(days=today.weekday()), today),
        "lastweek": (today - _dt.timedelta(days=today.weekday() + 7),
                     today - _dt.timedelta(days=today.weekday() + 1)),
        "thismonth": (today.replace(day=1), today),
        "thisyear": (today.replace(month=1, day=1), today),
    }
    if v == "lastmonth":
        first_this = today.replace(day=1)
        last_prev = first_this - _dt.timedelta(days=1)
        named["lastmonth"] = (last_prev.replace(day=1), last_prev)
    if v == "lastyear":
        named["lastyear"] = (today.replace(year=today.year - 1, month=1, day=1),
                             today.replace(year=today.year - 1, month=12, day=31))
    if v in named:
        lo, hi = named[v]
        return _Range(_day_bounds(lo)[0], _day_bounds(hi)[1], "mtime")
    for op in (">=", "<=", ">", "<", "="):
        if v.startswith(op):
            rest = v[len(op):]
            if op in (">=", "="):
                lo = _date_to_epoch(rest)
                return _Range(lo, 1 << 62, "mtime") if op == ">=" else _Range(
                    lo, _date_span_end(rest), "mtime")
            if op == ">":
                return _Range(_date_span_end(rest) + 1, 1 << 62, "mtime")
            if op == "<=":
                return _Range(0, _date_span_end(rest), "mtime")
            return _Range(0, max(0, _date_to_epoch(rest) - 1), "mtime")
    # 裸写一个日期是「落在这一段里」，不是「恰好等于这一秒」：
    # dm:2026 要覆盖整个 2026 年，dm:2026-05-20 要覆盖那一整天。
    return _Range(_date_to_epoch(v), _date_span_end(v), "mtime")


def _date_to_epoch(token: str) -> int:
    """把 YYYY / YYYY-MM / YYYY-MM-DD 解析成那一段的**起点**。"""
    t = token.strip().replace("/", "-")
    parts = t.split("-")
    try:
        if len(parts) == 1:
            return _day_bounds(_dt.date(int(parts[0]), 1, 1))[0]
        if len(parts) == 2:
            return _day_bounds(_dt.date(int(parts[0]), int(parts[1]), 1))[0]
        return _day_bounds(_dt.date(int(parts[0]), int(parts[1]), int(parts[2])))[0]
    except (ValueError, IndexError) as exc:
        raise QueryError(f"看不懂的日期：{token}") from exc


def _date_span_end(token: str) -> int:
    """区间右端要取那一段的**终点**，否则 dm:2026-01..2026-06 会漏掉六月。"""
    t = token.strip().replace("/", "-")
    parts = t.split("-")
    try:
        if len(parts) == 1:
            return _day_bounds(_dt.date(int(parts[0]), 12, 31))[1]
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            return _day_bounds(_dt.date(year, month,
                                        calendar.monthrange(year, month)[1]))[1]
        return _day_bounds(_dt.date(int(parts[0]), int(parts[1]), int(parts[2])))[1]
    except (ValueError, IndexError) as exc:
        raise QueryError(f"看不懂的日期：{token}") from exc


def _parse_function(name: str, value: str, *, now=None) -> _Node | None:
    fn = name.casefold()
    if fn == "ext":
        exts = tuple(e.strip().lstrip(".").casefold()
                     for e in re.split(r"[;,]", value) if e.strip())
        if not exts:
            raise QueryError("ext: 后面是空的")
        return _Ext(exts)
    if fn == "size":
        return _parse_size(value)
    if fn in ("dm", "datemodified"):
        if ".." in value:
            lo_s, hi_s = value.split("..", 1)
            lo = _date_to_epoch(lo_s) if lo_s.strip() else 0
            hi = _date_span_end(hi_s) if hi_s.strip() else (1 << 62)
            return _Range(lo, hi, "mtime")
        return _parse_date(value, now=now)
    if fn == "path":
        return _Term(value, on_path=True)
    if fn == "regex":
        return _Regex(value)
    if fn in ("file", "files"):
        return _Kind(False)
    if fn in ("folder", "folders"):
        return _Kind(True)
    if fn == "empty":
        return _Range(0, 0, "size")
    if fn == "case":
        return _Term(value, case_sensitive=True)
    if fn == "nocase":
        return _Term(value)
    if fn in ("ww", "wholeword"):
        return _Term(value, whole_word=True)
    return None


_HAS_SEP_RE = re.compile(r"[\\/]")


def _unify_seps(text: str) -> str:
    return text.replace("\\", "/")


def _make_term(text: str, *, match_path: bool) -> _Node:
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return _Term(text[1:-1], literal=True, on_path=match_path)
    # 查询里带路径分隔符时，该词自动改成拿完整路径匹配——与 Everything 一致，
    # 因为打 `ui\search` 的人显然是在描述位置而不是文件名
    return _Term(text, on_path=match_path or bool(_HAS_SEP_RE.search(text)))


def _parse_tokens(tokens: list[str], pos: int, *, now, match_path: bool):
    """递归下降：or := and ('|' and)* ; and := unary+ ; unary := '!'? primary"""
    branches: list[_Node] = []
    current: list[_Node] = []
    while pos < len(tokens):
        tok = tokens[pos]
        if tok in (">", ")"):
            break
        if tok == "|":
            if not current:
                raise QueryError("| 前面没有内容")
            branches.append(_And(current) if len(current) > 1 else current[0])
            current = []
            pos += 1
            continue
        if tok in ("<", "("):
            node, pos = _parse_tokens(tokens, pos + 1, now=now, match_path=match_path)
            closing = ">" if tok == "<" else ")"
            if pos >= len(tokens) or tokens[pos] != closing:
                raise QueryError(f"缺少 {closing}")
            pos += 1
            if node is not None:
                current.append(node)
            continue
        negate = False
        body = tok
        while body.startswith("!") and len(body) > 1:
            negate = not negate
            body = body[1:]
        node = _parse_atom(body, now=now, match_path=match_path)
        if node is not None:
            current.append(_Not(node) if negate else node)
        pos += 1
    if not current and not branches:
        return None, pos
    if current:
        branches.append(_And(current) if len(current) > 1 else current[0])
    root = branches[0] if len(branches) == 1 else _Or(branches)
    return root, pos


def _parse_atom(body: str, *, now, match_path: bool) -> _Node | None:
    if not body:
        return None
    # 函数名后面跟冒号；冒号在引号里不算
    if not body.startswith('"'):
        head, sep, tail = body.partition(":")
        if sep and head.casefold() in _FUNCTIONS:
            node = _parse_function(head, tail, now=now)
            if node is not None:
                return node
    return _make_term(body, match_path=match_path)


def parse(text: str, *, now: _dt.datetime | None = None,
          match_path: bool = False) -> Query:
    """解析一条「全部文件」范围的查询。语法错误抛 QueryError。"""
    raw = (text or "").strip()
    if not raw:
        return Query(None, raw)
    tokens = _split_tokens(raw)
    root, pos = _parse_tokens(tokens, 0, now=now, match_path=match_path)
    if pos < len(tokens):
        raise QueryError(f"多余的 {tokens[pos]}")
    return Query(root, raw)


def _collect_path_literals(node, out: list[str]) -> None:
    """只走「与」链：或的分支不是必要条件，非更不是。"""
    if isinstance(node, _And):
        for p in node.parts:
            _collect_path_literals(p, out)
    elif isinstance(node, _Term) and node.on_path and node._needle:
        out.append(node._needle)


def from_terms(terms) -> Query:
    """把一串**字面**词组成「与」查询：不解释任何语法。

    留给内部调用（以及不想让用户输入被当成语法的地方）：给什么就搜什么。
    """
    parts = [_Term(t, literal=True) for t in terms if t and t.strip()]
    if not parts:
        return Query(None, "")
    return Query(_And(parts) if len(parts) > 1 else parts[0], " ".join(terms))
