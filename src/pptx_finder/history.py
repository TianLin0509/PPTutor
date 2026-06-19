"""搜索历史：data_dir/search_history.json，最近 N 条去重（最近的在前）。"""
from __future__ import annotations

import json
from pathlib import Path

from .config import data_dir

_FILE = "search_history.json"


def _path(base: Path | None = None) -> Path:
    return (base or data_dir()) / _FILE


def load_history(limit: int = 10, base: Path | None = None) -> list[str]:
    p = _path(base)
    try:
        if p.exists():
            data = json.loads(p.read_text("utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data][:limit]
    except Exception:  # noqa: BLE001
        pass
    return []


def add_history(query: str, limit: int = 20, base: Path | None = None) -> None:
    query = (query or "").strip()
    if not query:
        return
    items = [q for q in load_history(limit=999, base=base) if q != query]
    items.insert(0, query)
    items = items[:limit]
    try:
        _path(base).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
