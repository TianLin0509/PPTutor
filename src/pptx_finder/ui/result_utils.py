"""Pure result-list helpers used by MainWindow and tests."""
from __future__ import annotations

import datetime
import os

from ..query_explain import suggestion_keys


def mode_key_from_text(mode: str) -> str:
    if mode in {"filename", "仅文件名"} or "文件名" in mode:
        return "filename"
    if mode in {"content", "仅内容"} or "内容" in mode:
        return "content"
    return "all"


def empty_suggestions(query: str, mode: str) -> list[str]:
    return suggestion_keys(query, mode_key_from_text(mode))


def sort_results(results: list, key: str) -> list:
    if key == "recent":
        return sorted(results, key=lambda r: r.mtime, reverse=True)
    if key == "name":
        return sorted(results, key=lambda r: r.name.lower())
    return list(results)


def time_bucket(mtime: float, now_ts: float) -> str:
    now = datetime.datetime.fromtimestamp(now_ts)
    try:
        dt = datetime.datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return "更早"
    d = (now.date() - dt.date()).days
    if d <= 0:
        return "今天"
    if d == 1:
        return "昨天"
    if d < 7:
        return "本周"
    if d < 30:
        return "本月"
    return "更早"


def group_by_time(results: list, now_ts: float) -> list:
    buckets: dict[str, list] = {}
    order: list[str] = []
    for r in results:
        label = time_bucket(r.mtime, now_ts)
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(r)
    return [(label, buckets[label]) for label in order]


def page_bucket(pc: int) -> str:
    if pc <= 10:
        return "1-10"
    if pc <= 30:
        return "10-30"
    return "30+"


def folder_of(path: str) -> str:
    d = os.path.basename(os.path.dirname(path))
    return d or path


def facet_type(r) -> str:
    return "pptx" if (r.ext or "").lower() == ".pptx" else "ppt"


def facet_counts(results: list, now_ts: float) -> dict:
    dims: dict[str, dict] = {"time": {}, "type": {}, "page": {}, "folder": {}}

    def bump(d, k):
        d[k] = d.get(k, 0) + 1

    for r in results:
        bump(dims["time"], time_bucket(r.mtime, now_ts))
        bump(dims["type"], facet_type(r))
        bump(dims["page"], page_bucket(r.page_count or 0))
        bump(dims["folder"], folder_of(r.path))
    return {k: list(v.items()) for k, v in dims.items()}


def facet_filter(results: list, filters: dict, now_ts: float) -> list:
    def ok(r):
        if filters.get("time") and time_bucket(r.mtime, now_ts) not in filters["time"]:
            return False
        if filters.get("type") and facet_type(r) not in filters["type"]:
            return False
        if filters.get("page") and page_bucket(r.page_count or 0) not in filters["page"]:
            return False
        if filters.get("folder") and folder_of(r.path) not in filters["folder"]:
            return False
        return True

    return [r for r in results if ok(r)]
