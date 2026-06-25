"""Synthetic benchmark for PPTutor render scheduling policies.

This does not automate PowerPoint. It models the queueing cost we can control:
task ordering, render resolution, and background prefetch eagerness. Real COM
latency still varies by deck, but these numbers make each scheduling change
comparable on the same workload.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    kind: str
    duration_ms: int
    priority: int
    created_order: int


def _run(tasks: list[Task]) -> dict[str, int]:
    now = 0
    remaining = list(tasks)
    done: dict[str, int] = {}
    while remaining:
        remaining.sort(key=lambda t: (t.priority, t.created_order))
        task = remaining.pop(0)
        now += task.duration_ms
        done[task.name] = now
    return done


def _metrics(done: dict[str, int]) -> dict[str, int]:
    thumb_times = [done[f"thumb-{i:02d}"] for i in range(1, 31) if f"thumb-{i:02d}" in done]
    return {
        "right_preview_ms": done["right-preview"],
        "top10_thumbs_done_ms": max(done[f"thumb-{i:02d}"] for i in range(1, 11)),
        "top20_thumbs_done_ms": max(done[f"thumb-{i:02d}"] for i in range(1, 21)),
        "top30_thumbs_done_ms": max(thumb_times),
        "first_neighbor_prefetch_ms": min(done[f"neighbor-{i}"] for i in range(1, 7)),
        "all_done_ms": max(done.values()),
    }


def legacy_policy() -> dict[str, int]:
    tasks: list[Task] = []
    order = 0
    tasks.append(Task("right-preview", "preview", 1800, 0, order)); order += 1
    # Legacy behavior lets eager high-res neighbor prefetch compete before the user-visible thumbnail backlog.
    for i in range(1, 7):
        tasks.append(Task(f"neighbor-{i}", "prefetch", 900, 10, order)); order += 1
    for i in range(1, 31):
        tasks.append(Task(f"thumb-{i:02d}", "thumb", 260, 20, order)); order += 1
    return _metrics(_run(tasks))


def optimized_policy() -> dict[str, int]:
    tasks: list[Task] = []
    order = 0
    tasks.append(Task("right-preview", "preview", 850, 0, order)); order += 1
    for i in range(1, 31):
        band = (i - 1) // 10
        tasks.append(Task(f"thumb-{i:02d}", "thumb", 220, 10 + band, order)); order += 1
    for i in range(1, 7):
        tasks.append(Task(f"neighbor-{i}", "prefetch", 420, 200, order)); order += 1
    return _metrics(_run(tasks))


def main() -> None:
    legacy = legacy_policy()
    optimized = optimized_policy()
    speedup = {
        key: round(legacy[key] / optimized[key], 2) if optimized[key] else None
        for key in legacy
    }
    print(json.dumps({"legacy": legacy, "optimized": optimized, "speedup": speedup}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
