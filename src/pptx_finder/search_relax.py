"""Bounded query relaxation shared by PPT and all-file search.

The contract is deliberately asymmetric:

1. strict search always runs first;
2. aliases and fuzzy matches are only supplemental candidates;
3. a relaxed candidate can never outrank any strict candidate.

This mirrors the useful parts of a search-engine query analyzer without adding
an online service or a second heavyweight index.  Alias expansion handles
multi-token/product-name equivalence (``百度云`` -> ``百度网盘``); character
n-grams only generate a small candidate set; Unicode edit similarity verifies
those candidates afterwards.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .text_tokenize import SEPARATOR_CLASS, normalize

#: 整个联想阶段的总预算。联想只是「严格搜不到时的兜底」，它多花的每一毫秒都是
#: 用户在等一个他本来就没指望的结果，所以宁可少召回也不能拖住搜索框。
#: 真机实测（1,649 份 PPT 的小库）：没有预算时 `zzqqxx-nonexistent` 要 51 秒，
#: `汇报.pptx` 要 817 毫秒——而严格路径分别只要 0.1 / 0.2 毫秒。
RELAX_TIME_BUDGET_SEC = 0.25
#: 允许做多少次编辑距离计算。时间预算之外再加一道，好让行为可复现、可测试
#: （墙钟受机器快慢影响，工作量不受）。
RELAX_OPS_BUDGET = 30_000


class RelaxBudget:
    """联想阶段的总预算：墙钟 + 工作量 + 取消，任一耗尽就收工。

    刻意做成「显式传递的对象」而不是全局状态：一次搜索里 PPT 名字、PPT 正文、
    全盘文件名三条打分路径共用同一份预算，否则每条各自限一次，合起来还是超。
    """

    __slots__ = ("_deadline", "_ops", "_cancel", "_tick", "exhausted")

    def __init__(self, *, seconds: float = RELAX_TIME_BUDGET_SEC,
                 ops: int = RELAX_OPS_BUDGET, cancel=None) -> None:
        self._deadline = time.monotonic() + max(0.0, float(seconds))
        self._ops = max(1, int(ops))
        self._cancel = cancel
        self._tick = 0
        self.exhausted = False

    def spend(self) -> bool:
        """扣一次预算。返回 False = 没预算了，调用方必须停下。"""
        if self.exhausted:
            return False
        self._ops -= 1
        if self._ops <= 0:
            self.exhausted = True
            return False
        self._tick += 1
        # 每 256 次才看一次表：time.monotonic() 本身不便宜，而这个循环最内层
        # 每秒要跑几十万次。
        if not (self._tick & 0xFF):
            if time.monotonic() > self._deadline:
                self.exhausted = True
                return False
            if self._cancel is not None and self._cancel():
                self.exhausted = True
                return False
        return True


@dataclass(frozen=True)
class Relaxation:
    value: str
    kind: str                    # alias | fuzzy


# Search-time aliases, not index-time rewriting: historical indexes immediately
# benefit and an alias can never erase the user's literal query.  Keep groups
# narrow and product-name-like; generic words such as "云" are intentionally not
# synonyms of "网盘" by themselves.
_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("百度网盘", "百度云", "百度云盘", "Baidu Netdisk"),
    ("阿里云盘", "阿里网盘", "Aliyun Drive", "Alibaba Cloud Drive"),
    ("夸克网盘", "夸克云盘", "Quark Drive"),
    ("OneDrive", "微软云盘", "Microsoft OneDrive"),
    ("Google Drive", "谷歌云盘"),
)

_ALIAS_LOOKUP: dict[str, tuple[str, ...]] = {}
for _group in _ALIAS_GROUPS:
    for _value in _group:
        _ALIAS_LOOKUP[normalize(_value).strip()] = _group

_COMPACT_RE = re.compile(rf"{SEPARATOR_CLASS}+")
_PART_RE = re.compile(rf"{SEPARATOR_CLASS}+")
_ALL_FILE_SYNTAX_RE = re.compile(r'''[*!|<>()?:"/\\]''')


def compact(text: str) -> str:
    return _COMPACT_RE.sub("", _fold(text or "")).strip()


def _fold(text: str) -> str:
    base = normalize(text or "")
    if base.isascii():
        return base
    decomposed = unicodedata.normalize("NFD", base)
    stripped = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped)


def is_relaxable_query(query: str, *, all_files: bool = False) -> bool:
    """Only relax a plain lexical query; never reinterpret explicit syntax."""
    raw = (query or "").strip()
    if not raw or "\n" in raw or "\r" in raw:
        return False
    if all_files and _ALL_FILE_SYNTAX_RE.search(raw):
        return False
    # PPT quotes/multi-clause input has explicit AND/phrase semantics.  Do not
    # silently turn it into an OR-like fuzzy query.
    if '"' in raw or len(raw.split()) > 3:
        return False
    value = compact(raw)
    return len(value) >= (4 if value.isascii() else 3)


def alias_expansions(query: str) -> list[str]:
    raw = normalize((query or "").strip())
    out: list[str] = []
    # Match longest aliases first so ``百度云盘资料`` substitutes 百度云盘 as
    # one token instead of producing ``百度网盘盘资料`` from its shorter prefix.
    matched_spans: list[tuple[int, int]] = []
    for source in sorted(_ALIAS_LOOKUP, key=len, reverse=True):
        start = raw.find(source)
        if start < 0:
            continue
        end = start + len(source)
        if any(start >= left and end <= right for left, right in matched_spans):
            continue
        # ASCII product names need token boundaries; ``onedrive`` inside a
        # longer identifier should not be silently rewritten.
        if source.isascii():
            if start and raw[start - 1].isalnum():
                continue
            if end < len(raw) and raw[end].isalnum():
                continue
        matched_spans.append((start, end))
        for value in _ALIAS_LOOKUP[source]:
            target = normalize(value).strip()
            if target == source:
                continue
            candidate = raw[:start] + target + raw[end:]
            if candidate and candidate != raw and candidate not in out:
                out.append(candidate)
    return out


def automatic_relaxations(query: str, suggestions=()) -> list[Relaxation]:
    """Aliases first, then bounded corpus-derived typo suggestions."""
    out: list[Relaxation] = []
    seen = {normalize(query).strip()}
    for value in alias_expansions(query):
        norm = normalize(value).strip()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(Relaxation(value, "alias"))
    target_compact = compact(query)
    for value in suggestions or ():
        if not target_compact.isascii() and len(target_compact) <= 3:
            break
        value = str(value or "").strip()
        norm = normalize(value).strip()
        candidate_compact = compact(value)
        # A clickable "try fewer words" suggestion is useful, but automatically
        # replacing 百度云 with the shorter 百度 floods results and defeats the
        # user's intent.  Automatic correction must retain most of the term.
        if (
            not candidate_compact
            or len(candidate_compact) < max(3, int(len(target_compact) * 0.8 + 0.999))
            or (candidate_compact in target_compact
                and len(candidate_compact) < len(target_compact))
            or not fuzzy_name_score(query, value)
        ):
            continue
        if norm and norm not in seen:
            seen.add(norm)
            out.append(Relaxation(value, "fuzzy"))
    return out


def fuzzy_anchors(query: str, *, max_anchors: int = 6) -> list[str]:
    """Return overlapping n-grams used only to generate fuzzy candidates."""
    value = compact(query)
    # Three-character CJK terms are too easy to match through one common bigram
    # (百度云 would otherwise pull in every 百度* name).  Product aliases cover
    # meaningful short equivalences; generic typo recall starts at four chars.
    if len(value) < 4:
        return []
    width = 3 if value.isascii() else 2
    grams = [value[i:i + width] for i in range(len(value) - width + 1)]
    # Prefer boundary and evenly spread grams.  Candidate generation is OR, so a
    # transposition/insertion can damage one gram without hiding the whole term.
    if len(grams) > max_anchors:
        picks = {0, len(grams) - 1}
        for i in range(1, max_anchors - 1):
            picks.add(round(i * (len(grams) - 1) / (max_anchors - 1)))
        grams = [grams[i] for i in sorted(picks)]
    return list(dict.fromkeys(g for g in grams if g))


def _candidate_forms(value: str) -> list[str]:
    norm = _fold(os.path.splitext(value or "")[0])
    compact_full = _COMPACT_RE.sub("", norm)
    forms = [compact_full] if compact_full else []
    for part in _PART_RE.split(norm):
        part = _COMPACT_RE.sub("", part)
        if part and part not in forms:
            forms.append(part)
    return forms


def _max_fuzzy_edits(value: str) -> int:
    """Search-engine style AUTO budget, deliberately capped at two edits."""
    length = len(value)
    if length <= 2:
        return 0
    if length <= 5:
        return 1
    return 2


def _osa_distance(left: str, right: str) -> int:
    """Optimal-string-alignment distance (edit distance + adjacent swaps)."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for i, lhs in enumerate(left, start=1):
        current = [i]
        for j, rhs in enumerate(right, start=1):
            value = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (lhs != rhs),
            )
            if (
                previous_previous is not None
                and i > 1
                and j > 1
                and lhs == right[j - 2]
                and left[i - 2] == rhs
            ):
                value = min(value, previous_previous[j - 2] + 1)
            current.append(value)
        previous_previous, previous = previous, current
    return previous[-1]


def _edit_similarity(target: str, value: str) -> float:
    # Model numbers, years and versions are high-signal identifiers.  Allowing
    # an edit to turn ``gpt4`` into the nearby window ``gptv`` recreates the
    # exact alphanumeric false positive that strict search deliberately avoids.
    target_digits = "".join(ch for ch in target if ch.isdigit())
    if target_digits and "".join(ch for ch in value if ch.isdigit()) != target_digits:
        return 0.0
    budget = _max_fuzzy_edits(target)
    if abs(len(target) - len(value)) > budget:
        return 0.0
    distance = _osa_distance(target, value)
    if distance > budget:
        return 0.0
    ratio = SequenceMatcher(None, target, value, autojunk=False).ratio()
    if target[:1] == value[:1]:
        ratio += 0.025
    return min(ratio, 1.0)


def _windowed_best(target: str, source: str, anchors, budget: RelaxBudget,
                   *, max_positions: int = 24) -> float:
    """Score only the windows that sit on an n-gram hit.

    早先这里是「把 source 上所有宽度接近 target 的窗口全试一遍」。那是
    O(len(source) × len(target)²) 的纯 Python 动态规划，而候选可能有上万个：
    真机 profile 显示 `_osa_distance` 被调 458,054 次、`min()` 一亿六千万次，
    一条查询 51 秒。

    改成只在锚点命中处开窗。锚点本来就是 target 的 n-gram，编辑距离 ≤2 的相似串
    必然保留其中至少一个（这也是候选生成本身的前提），所以召回几乎不变，
    而窗口数从「几百个」降到「命中处附近的十几个」。
    """
    if not target or not source or not anchors:
        return 0.0
    best = 0.0
    edit_budget = _max_fuzzy_edits(target)
    visited: set[tuple[int, int]] = set()
    positions = 0
    for anchor in anchors:
        start = 0
        while positions < max_positions:
            pos = source.find(anchor, start)
            if pos < 0:
                break
            positions += 1
            start = pos + 1
            for width in range(max(2, len(target) - edit_budget),
                               len(target) + edit_budget + 1):
                left_min = max(0, pos - max(0, width - len(anchor)))
                left_max = min(pos, max(0, len(source) - width))
                for left in range(left_min, left_max + 1):
                    key = (left, width)
                    if key in visited:
                        continue
                    visited.add(key)
                    if not budget.spend():
                        return best
                    best = max(best, _edit_similarity(target, source[left:left + width]))
                    if best >= 1.0:
                        return best
        if budget.exhausted:
            break
    return best


def fuzzy_name_score(query: str, candidate: str, *,
                     budget: RelaxBudget | None = None, anchors=None) -> float:
    """Unicode-aware bounded similarity.  Zero means "do not recall"."""
    target = compact(query)
    if not target:
        return 0.0
    budget = budget if budget is not None else RelaxBudget()
    if anchors is None:
        anchors = fuzzy_anchors(query)
    best = 0.0
    for form in _candidate_forms(candidate):
        if not form:
            continue
        # 整体比一次：短名字（`resmue` vs `resume`）根本没有锚点可言，靠的就是这一步。
        if not budget.spend():
            return best
        best = max(best, _edit_similarity(target, form))
        if best >= 1.0:
            return best
        # 长名字里「某一个词打错了」则靠锚点开窗，不再全量滑窗。
        if len(form) > len(target):
            best = max(best, _windowed_best(target, form, anchors, budget))
            if best >= 1.0 or budget.exhausted:
                return best
    return best


def fuzzy_text_score(query: str, text: str, *, max_positions: int = 80,
                     budget: RelaxBudget | None = None, anchors=None) -> float:
    """Score short windows around n-gram hits in a potentially long slide body."""
    target = compact(query)
    source = compact(text)
    if anchors is None:
        anchors = fuzzy_anchors(query)
    budget = budget if budget is not None else RelaxBudget()
    return _windowed_best(target, source, anchors, budget,
                          max_positions=max_positions)
