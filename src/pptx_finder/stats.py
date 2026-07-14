"""趣味统计「我的胶片报告」：纯计算层 + db 薄访问层。

设计：纯函数吃 FileStat 列表 → 统计 dataclass，便于确定性单测；
db 访问单独封装。全部基于现有字段（mtime/size/page_count/group_id/页文本），
不读 PPTX 内部 docProps（见设计 Q1A）。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import PPT_EXTS
from .report_insights import (
    STAT_FEATURE_KEYS,
    ContentInsightsStat,
    CreationInsightsStat,
    HallOfFameStat,
    LibraryInsightsStat,
    VersionInsightsStat,
    build_enhanced_insights,
)

_MIN_VALID_MTIME = datetime(1980, 1, 1).timestamp()


def _has_real_mtime(f: "FileStat") -> bool:
    return float(f.mtime or 0) >= _MIN_VALID_MTIME


@dataclass
class FileStat:
    """统计用的精简文件记录（一份 pptx）。"""

    name: str
    mtime: float
    size: int
    page_count: int
    status: str
    group_id: int | None
    char_count: int
    path: str = ""
    file_id: int = 0
    indexed_at: float = 0.0


@dataclass
class NightOwlStat:
    """① 肝度：深夜 / 周末 / 最晚一次。"""

    night_count: int
    night_ratio: float
    weekend_count: int
    weekend_ratio: float
    latest_name: str | None
    latest_hour: int | None


def _hour(f: FileStat) -> int:
    return datetime.fromtimestamp(f.mtime).hour


def _weekday(f: FileStat) -> int:
    return datetime.fromtimestamp(f.mtime).weekday()  # Mon=0 .. Sun=6


def _is_night(hour: int) -> bool:
    """深夜定义 [22:00, 06:00)。"""
    return hour >= 22 or hour < 6


def night_owl(files: list[FileStat]) -> NightOwlStat:
    """深夜/周末肝度。最晚一次 = 深夜序里最靠后的（凌晨越深越狠）。"""
    files = [f for f in files if _has_real_mtime(f)]
    total = len(files)
    night = [f for f in files if _is_night(_hour(f))]
    weekend = [f for f in files if _weekday(f) >= 5]
    if night:
        latest = max(night, key=lambda f: (_hour(f) - 22) % 24)
        latest_name, latest_hour = latest.name, _hour(latest)
    else:
        latest_name, latest_hour = None, None
    return NightOwlStat(
        night_count=len(night),
        night_ratio=len(night) / total if total else 0.0,
        weekend_count=len(weekend),
        weekend_ratio=len(weekend) / total if total else 0.0,
        latest_name=latest_name,
        latest_hour=latest_hour,
    )


def heatmap(files: list[FileStat]) -> list[list[int]]:
    """7×24 修改频次矩阵：行=weekday(Mon=0..Sun=6)，列=hour(0..23)。"""
    m = [[0] * 24 for _ in range(7)]
    for f in files:
        if not _has_real_mtime(f):
            continue
        dt = datetime.fromtimestamp(f.mtime)
        m[dt.weekday()][dt.hour] += 1
    return m


# 「终版诅咒」命名梗：保守集合，避免误伤正常名（不收过宽的单字「改」）
_CURSE = re.compile(r"最终|final|终版|定稿|修订|修改|最新版|v\d+", re.IGNORECASE)


@dataclass
class VersionDramaStat:
    """③ 改版名场面：最能改奖 / 终版诅咒 / 僵尸胶片。"""

    top_group_name: str | None
    top_group_versions: int
    final_curse_count: int
    final_curse_ratio: float
    zombie_name: str | None
    zombie_mtime: float


def version_drama(files: list[FileStat]) -> VersionDramaStat:
    # 最能改奖：成员最多的版本组（≥2 才算改过多版），代表名取组内最新一版
    groups: dict[int, list[FileStat]] = {}
    for f in files:
        if f.group_id is not None:
            groups.setdefault(f.group_id, []).append(f)
    top_name, top_versions = None, 0
    if groups:
        biggest = max(groups.values(), key=len)
        if len(biggest) >= 2:
            newest = max(biggest, key=lambda f: f.mtime)
            top_name, top_versions = newest.name, len(biggest)
    # 终版诅咒：文件名命中命名梗
    curse = [f for f in files if _CURSE.search(f.name)]
    total = len(files)
    # 僵尸胶片：最老的一份
    dated_files = [f for f in files if _has_real_mtime(f)]
    zombie = min(dated_files, key=lambda f: f.mtime) if dated_files else None
    return VersionDramaStat(
        top_group_name=top_name,
        top_group_versions=top_versions,
        final_curse_count=len(curse),
        final_curse_ratio=len(curse) / total if total else 0.0,
        zombie_name=zombie.name if zombie else None,
        zombie_mtime=zombie.mtime if zombie else 0.0,
    )


@dataclass
class ScaleStat:
    """⑤ 规模仓鼠：最长 / 巨无霸 / 累计码字 / 磁盘占用。"""

    longest_name: str | None
    longest_pages: int
    biggest_name: str | None
    biggest_bytes: int
    total_chars: int
    total_bytes: int
    deck_count: int


def scale(files: list[FileStat]) -> ScaleStat:
    if not files:
        return ScaleStat(None, 0, None, 0, 0, 0, 0)
    longest = max(files, key=lambda f: f.page_count)
    biggest = max(files, key=lambda f: f.size)
    return ScaleStat(
        longest_name=longest.name,
        longest_pages=longest.page_count,
        biggest_name=biggest.name,
        biggest_bytes=biggest.size,
        total_chars=sum(f.char_count for f in files),
        total_bytes=sum(f.size for f in files),
        deck_count=len(files),
    )


@dataclass
class ActivityStat:
    """按当前文件最后修改时间推导的创作足迹。"""

    active_days: int
    longest_streak_days: int
    peak_month: str | None
    peak_month_count: int
    first_mtime: float
    latest_mtime: float


def activity(files: list[FileStat]) -> ActivityStat:
    """活跃天数、连续活跃期与最忙月份；同一天多份胶片只算一个活跃日。"""
    files = [f for f in files if _has_real_mtime(f)]
    if not files:
        return ActivityStat(0, 0, None, 0, 0.0, 0.0)

    days = sorted({datetime.fromtimestamp(f.mtime).date() for f in files})
    longest = current = 1
    for previous, day in zip(days, days[1:]):
        current = current + 1 if (day - previous).days == 1 else 1
        longest = max(longest, current)

    months: dict[str, int] = {}
    for f in files:
        key = datetime.fromtimestamp(f.mtime).strftime("%Y-%m")
        months[key] = months.get(key, 0) + 1
    peak_month, peak_count = max(months.items(), key=lambda item: (item[1], item[0]))
    mtimes = [float(f.mtime) for f in files]
    return ActivityStat(
        active_days=len(days),
        longest_streak_days=longest,
        peak_month=peak_month,
        peak_month_count=peak_count,
        first_mtime=min(mtimes),
        latest_mtime=max(mtimes),
    )


@dataclass
class LibraryDNAStat:
    """胶片形态、内容密度、同源复用与正文索引健康度。"""

    avg_pages: float
    avg_chars_per_page: float
    brief_count: int
    epic_count: int
    family_count: int
    family_deck_count: int
    family_ratio: float
    content_ready_count: int
    content_ready_ratio: float


def library_dna(files: list[FileStat]) -> LibraryDNAStat:
    """复用现有 FileStat 一次线性计算，不触碰 PPT 文件或额外查询数据库。"""
    total = len(files)
    if not total:
        return LibraryDNAStat(0.0, 0.0, 0, 0, 0, 0, 0.0, 0, 0.0)

    total_pages = sum(max(0, int(f.page_count or 0)) for f in files)
    total_chars = sum(max(0, int(f.char_count or 0)) for f in files)
    groups: dict[int, int] = {}
    for f in files:
        if f.group_id is not None:
            groups[f.group_id] = groups.get(f.group_id, 0) + 1
    family_sizes = [count for count in groups.values() if count >= 2]
    family_decks = sum(family_sizes)
    content_ready = sum(1 for f in files if f.status == "ok")
    return LibraryDNAStat(
        avg_pages=total_pages / total,
        avg_chars_per_page=total_chars / total_pages if total_pages else 0.0,
        brief_count=sum(1 for f in files if 0 < int(f.page_count or 0) <= 5),
        epic_count=sum(1 for f in files if int(f.page_count or 0) >= 50),
        family_count=len(family_sizes),
        family_deck_count=family_decks,
        family_ratio=family_decks / total,
        content_ready_count=content_ready,
        content_ready_ratio=content_ready / total,
    )


@dataclass
class PersonaStat:
    """⑥ 人格称号：主称号 + 副标签 + 作息×产出 矩阵定位。"""

    title: str
    badges: list[str]
    rhythm: str = ""   # 作息维度：夜猫子 / 周末战士 / 正常作息
    output: str = ""   # 产出维度：囤积型 / 字海型 / 高产型 / 精修型
    role: str = ""     # (作息×产出) 派生的角色定位


# (作息, 产出) → 角色定位；未列中的组合用默认「全能型选手」
_ROLE = {
    ("夜猫子", "高产型"): "夜间作战参谋",
    ("夜猫子", "字海型"): "深夜笔杆子",
    ("夜猫子", "囤积型"): "午夜仓库管理员",
    ("夜猫子", "精修型"): "挑灯夜战的工匠",
    ("周末战士", "高产型"): "周末加班发动机",
    ("周末战士", "囤积型"): "周末囤货狂",
    ("周末战士", "精修型"): "周末细节控",
    ("正常作息", "高产型"): "高效流水线",
    ("正常作息", "字海型"): "正经码字机",
    ("正常作息", "囤积型"): "稳健仓鼠",
    ("正常作息", "精修型"): "细节控匠人",
}


def persona(night: NightOwlStat, drama: VersionDramaStat, sc: ScaleStat) -> PersonaStat:
    """按阈值贴标签（首个为主称号，其余副标签）；并给「作息×产出」矩阵定位 + 角色。"""
    avg_chars = sc.total_chars / sc.deck_count if sc.deck_count else 0
    candidates = [
        ("深夜画师", night.night_ratio >= 0.3),
        ("周末战士", night.weekend_ratio >= 0.3),
        ("终版收割机", drama.final_curse_ratio >= 0.4),
        ("改版狂魔", drama.top_group_versions >= 10),
        ("字海狂魔", sc.total_chars >= 500_000),
        ("仓鼠囤积者", sc.deck_count >= 200),
        ("极简主义者", sc.deck_count > 0 and avg_chars < 200),
    ]
    hits = [name for name, ok in candidates if ok]
    title = hits[0] if hits else "佛系做图人"
    badges = hits[1:] if hits else []
    # 作息维度（夜 > 周末 > 正常）
    if night.night_ratio >= 0.3:
        rhythm = "夜猫子"
    elif night.weekend_ratio >= 0.3:
        rhythm = "周末战士"
    else:
        rhythm = "正常作息"
    # 产出维度（囤积 > 字海 > 高产 > 精修）
    if sc.deck_count >= 200:
        output = "囤积型"
    elif avg_chars >= 800:
        output = "字海型"
    elif sc.deck_count >= 50:
        output = "高产型"
    else:
        output = "精修型"
    role = _ROLE.get((rhythm, output), "全能型选手")
    return PersonaStat(title=title, badges=badges, rhythm=rhythm, output=output, role=role)


@dataclass
class Report:
    """组装后的完整胶片报告。"""

    scope_year: int | None
    deck_count: int
    night: NightOwlStat
    heatmap: list[list[int]]
    drama: VersionDramaStat
    scale: ScaleStat
    activity: ActivityStat
    library_dna: LibraryDNAStat
    persona: PersonaStat
    hall: HallOfFameStat
    creation: CreationInsightsStat
    content: ContentInsightsStat
    library: LibraryInsightsStat
    versions: VersionInsightsStat
    achievements: tuple[str, ...]
    one_liner: str


def fetch_file_stats(
    conn: sqlite3.Connection,
    *,
    year: int | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> list[FileStat]:
    """从 SQLite 取每份 PPT(pptx/ppt) 的统计字段：join 版本组 + 聚合页文本字数。
    刻意只统计 PPT——「胶片报告」是 PPT 习惯分析，不混入多文档搜索引入的 docx/xlsx/txt/pdf。"""
    ph = ",".join("?" * len(PPT_EXTS))
    predicates = [f"lower(f.ext) IN ({ph})"]
    params: list[object] = [e.lower() for e in PPT_EXTS]
    if year is not None:
        predicates.extend(["f.mtime >= ?", "f.mtime < ?"])
        params.extend([
            datetime(year, 1, 1).timestamp(),
            datetime(year + 1, 1, 1).timestamp(),
        ])
    if since_ts is not None:
        predicates.append("f.mtime >= ?")
        params.append(float(since_ts))
    if until_ts is not None:
        predicates.append("f.mtime < ?")
        params.append(float(until_ts))
    rows = conn.execute(
        f"""
        WITH scoped_files AS (
            SELECT f.id, f.path, f.name, f.mtime, f.size, f.page_count, f.status,
                   f.indexed_at
            FROM files f
            WHERE {' AND '.join(predicates)}
        ),
        char_counts AS (
            SELECT p.file_id, SUM(LENGTH(p.raw_text)) AS chars
            FROM pages_raw p
            JOIN scoped_files sf2 ON sf2.id = p.file_id
            GROUP BY p.file_id
        )
        SELECT f.id, f.path, f.name, f.mtime, f.size, f.page_count, f.status,
               f.indexed_at,
               m.group_id,
               COALESCE(c.chars, 0) AS char_count
        FROM scoped_files f
        LEFT JOIN minhash m ON m.file_id = f.id
        LEFT JOIN char_counts c ON c.file_id = f.id
        """,
        tuple(params),
    ).fetchall()
    return [
        FileStat(
            name=r["name"], mtime=r["mtime"], size=r["size"],
            page_count=r["page_count"], status=r["status"],
            group_id=r["group_id"], char_count=r["char_count"] or 0,
            path=r["path"], file_id=r["id"], indexed_at=r["indexed_at"] or 0.0,
        )
        for r in rows
    ]


def build_report(
    conn: sqlite3.Connection,
    *,
    year: int | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
    version_db_path: str | Path | None = None,
    now_ts: float | None = None,
) -> Report:
    """组装完整报告。

    year 给定则只统计该自然年修改的文件；since_ts / until_ts 用于本月、本周等滚动时间窗。
    until_ts 按半开区间处理，避免边界文件被相邻窗口重复统计。
    """
    files = fetch_file_stats(
        conn,
        year=year,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    night = night_owl(files)
    sc = scale(files)
    drama = version_drama(files)
    base_persona = persona(night, drama, sc)
    enhanced = build_enhanced_insights(
        conn,
        files,
        year=year,
        since_ts=since_ts,
        until_ts=until_ts,
        version_db_path=version_db_path,
        now_ts=now_ts,
        persona_role=base_persona.role or base_persona.title,
    )
    return Report(
        scope_year=year,
        deck_count=len(files),
        night=night,
        heatmap=heatmap(files),
        drama=drama,
        scale=sc,
        activity=activity(files),
        library_dna=library_dna(files),
        persona=base_persona,
        hall=enhanced.hall,
        creation=enhanced.creation,
        content=enhanced.content,
        library=enhanced.library,
        versions=enhanced.versions,
        achievements=enhanced.achievements,
        one_liner=enhanced.one_liner,
    )
