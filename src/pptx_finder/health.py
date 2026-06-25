"""库健康体检：把「健康诊断」从给 App 量血压，升级成给用户的 PPT 库做体检。

纯计算层（吃 index.db 的 files 表）→ 体检 dataclass，便于确定性单测：
- 重复堆积：content_hash 完全相同的多份副本（可一键送回收站回收磁盘）
- 僵尸冷文件：超过一年没动过
- 终版诅咒：文件名命中「最终版/终版/定稿/v99」等命名梗（与胶片报告同一口径）
- 巨无霸 / 超页：最大体积、最长页数
- 解析失败：status≠ok（加密 / 损坏 / 超大跳过）

「治疗」动作 recycle_paths 用 Windows 回收站（FOF_ALLOWUNDO，可撤销），
与纯逻辑隔离（底层 _shell_recycle 单独成函数，便于测试 monkeypatch，绝不在测试里真删）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import stats  # 复用 stats._CURSE（终版诅咒命名梗，与胶片报告同一口径）

_YEAR_SEC = 365 * 24 * 3600


def human_bytes(n: int | float) -> str:
    """字节 → 人类可读（1.2 GB）。"""
    f = float(max(0, n or 0))
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def _is_exact_hash(value: str) -> bool:
    """与 search._is_exact_hash 同口径：sha256:<64 hex> 才算可信的完全相同标记。"""
    return bool(value and value.startswith("sha256:") and len(value) == len("sha256:") + 64)


# ---------- ① 重复堆积 ----------

@dataclass
class DuplicateGroup:
    """一组字节完全相同的副本。paths[0]=建议保留，其余可回收。"""

    content_hash: str
    paths: list[str]       # 全部位置，首个为建议保留项
    keep_path: str         # 建议保留（命名含终稿/定稿优先，否则最新）
    size: int              # 单份大小（组内相同）
    reclaimable: int       # 删冗余可回收字节 = size × (份数-1)

    @property
    def copies(self) -> int:
        return len(self.paths)

    @property
    def redundant(self) -> int:
        return max(0, len(self.paths) - 1)


def _pick_keep(items: list[sqlite3.Row]):
    """建议保留：文件名含 终稿/定稿/final/最终 优先，否则修改时间最新。"""
    def key(r):
        n = (r["name"] or "").lower()
        kw = any(k in n for k in ("终稿", "定稿", "final", "最终"))
        return (kw, float(r["mtime"] or 0))
    return max(items, key=key)


def find_duplicate_groups(conn: sqlite3.Connection) -> list[DuplicateGroup]:
    """全库按 content_hash 找完全相同的多份副本，按可回收空间降序。"""
    rows = conn.execute(
        "SELECT path, name, size, mtime, content_hash FROM files "
        "WHERE content_hash LIKE 'sha256:%'"
    ).fetchall()
    by_hash: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    for r in rows:
        h = r["content_hash"]
        if _is_exact_hash(h):
            by_hash[h][r["path"]] = r   # 按 path 去重，避免同路径重复计数
    groups: list[DuplicateGroup] = []
    for h, by_path in by_hash.items():
        items = list(by_path.values())
        if len(items) < 2:
            continue
        keep = _pick_keep(items)
        paths = [keep["path"]] + [it["path"] for it in items if it["path"] != keep["path"]]
        size = int(keep["size"] or 0)
        groups.append(DuplicateGroup(
            content_hash=h, paths=paths, keep_path=keep["path"],
            size=size, reclaimable=size * (len(paths) - 1),
        ))
    groups.sort(key=lambda g: (g.reclaimable, g.copies), reverse=True)
    return groups


# ---------- 体检总报告 ----------

@dataclass
class HealthReport:
    deck_count: int
    score: int                                   # 0-100 健康分
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    duplicate_reclaimable: int = 0               # 全部重复可回收字节
    duplicate_redundant: int = 0                 # 冗余副本份数
    zombie_count: int = 0
    zombie_bytes: int = 0
    curse_count: int = 0
    bloat_biggest: tuple[str, int] | None = None  # (name, size)
    bloat_longest: tuple[str, int] | None = None  # (name, page_count)
    parse_failed: int = 0
    parse_failed_by_status: dict[str, int] = field(default_factory=dict)

    @property
    def duplicate_groups_count(self) -> int:
        return len(self.duplicate_groups)


def _health_score(deck_count: int, redundant: int, zombie_n: int,
                  curse_n: int, failed_n: int) -> int:
    """0-100 健康分：重复/解析失败扣最狠，僵尸/命名诅咒次之。空库满分。"""
    if deck_count <= 0:
        return 100
    pct = lambda n: n / deck_count  # noqa: E731
    penalty = (
        min(35.0, pct(redundant) * 60.0)   # 冗余副本占比
        + min(22.0, pct(failed_n) * 55.0)  # 解析失败
        + min(18.0, pct(zombie_n) * 22.0)  # 僵尸冷文件
        + min(12.0, pct(curse_n) * 18.0)   # 终版诅咒命名
    )
    return max(0, round(100 - penalty))


def scan_health(conn: sqlite3.Connection, *, now: float | None = None) -> HealthReport:
    """一次扫库出体检报告。now 可注入（确定性单测）。"""
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT name, path, size, mtime, page_count, status, content_hash FROM files"
    ).fetchall()
    deck_count = len(rows)

    dup_groups = find_duplicate_groups(conn)
    dup_reclaim = sum(g.reclaimable for g in dup_groups)
    dup_redundant = sum(g.redundant for g in dup_groups)

    zombies = [r for r in rows if (now - float(r["mtime"] or 0)) > _YEAR_SEC]
    zombie_bytes = sum(int(r["size"] or 0) for r in zombies)

    curse = [r for r in rows if r["name"] and stats._CURSE.search(r["name"])]

    biggest = max(rows, key=lambda r: int(r["size"] or 0), default=None)
    longest = max(rows, key=lambda r: int(r["page_count"] or 0), default=None)
    bloat_biggest = (biggest["name"], int(biggest["size"] or 0)) if biggest and (biggest["size"] or 0) > 0 else None
    bloat_longest = (longest["name"], int(longest["page_count"] or 0)) if longest and (longest["page_count"] or 0) > 0 else None

    failed = [r for r in rows if (r["status"] or "ok") != "ok"]
    by_status = dict(Counter((r["status"] or "ok") for r in failed))

    score = _health_score(deck_count, dup_redundant, len(zombies), len(curse), len(failed))
    return HealthReport(
        deck_count=deck_count,
        score=score,
        duplicate_groups=dup_groups,
        duplicate_reclaimable=dup_reclaim,
        duplicate_redundant=dup_redundant,
        zombie_count=len(zombies),
        zombie_bytes=zombie_bytes,
        curse_count=len(curse),
        bloat_biggest=bloat_biggest,
        bloat_longest=bloat_longest,
        parse_failed=len(failed),
        parse_failed_by_status=by_status,
    )


# ---------- ② 治疗：送回收站（可撤销） ----------

def _shell_recycle(abs_paths: list[str]) -> tuple[int, bool]:
    """底层：用 Windows Shell 把文件送回收站（FOF_ALLOWUNDO）。

    单独成函数便于单测 monkeypatch，绝不在测试里真调。返回 (returncode, aborted)。
    """
    from win32com.shell import shell, shellcon  # type: ignore[import-not-found]

    flags = (
        shellcon.FOF_ALLOWUNDO       # 进回收站而非彻底删 → 可撤销
        | shellcon.FOF_NOCONFIRMATION  # 我们自己弹确认框
        | shellcon.FOF_NOERRORUI
        | shellcon.FOF_SILENT
    )
    src = "\0".join(abs_paths) + "\0\0"  # 双 null 结尾的多路径
    rc, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, src, None, flags, None, None))
    return int(rc), bool(aborted)


def recycle_paths(paths: list[str]) -> dict:
    """把若干文件送系统回收站（可撤销），返回 {ok, recycled, failed, freed_bytes}。

    freed_bytes 在删除前按实际消失的文件统计（删后无法 getsize），保证准确。
    """
    seen: set[str] = set()
    targets: list[str] = []
    for p in paths:
        if not p:
            continue
        ap = os.path.abspath(p)
        key = ap.lower()
        if key in seen:
            continue
        seen.add(key)
        if os.path.exists(ap):
            targets.append(ap)

    sizes: dict[str, int] = {}
    for ap in targets:
        try:
            sizes[ap] = os.path.getsize(ap)
        except OSError:
            sizes[ap] = 0

    if not targets:
        return {"ok": True, "recycled": 0, "failed": [], "freed_bytes": 0, "error": ""}

    try:
        rc, aborted = _shell_recycle(targets)
    except Exception as exc:  # noqa: BLE001 回收失败不能抛进 UI
        return {"ok": False, "recycled": 0, "failed": list(targets),
                "freed_bytes": 0, "error": f"{type(exc).__name__}: {exc}"}

    removed = [p for p in targets if not os.path.exists(p)]
    failed = [p for p in targets if os.path.exists(p)]
    freed = sum(sizes.get(p, 0) for p in removed)
    return {
        "ok": not failed and rc == 0 and not aborted,
        "recycled": len(removed),
        "failed": failed,
        "freed_bytes": freed,
        "error": "" if not failed else f"shell rc={rc} aborted={aborted}",
    }
