"""胶片报告增强统计。

所有能力只读取现有索引库与版本元数据，不打开 PPT、不启动 PowerPoint。
内容类统计有明确的页数/字符预算；即使片库很大，也只在报告后台任务中做有界分析。
"""
from __future__ import annotations

import ntpath
import logging
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from .config import PPT_EXTS


# 用户确认的 12 项核心统计 + 25 项趣味增强。这个清单既是实现清单，也是回归测试门禁。
STAT_FEATURE_KEYS = (
    "hall_of_fame",
    "most_edited",
    "catchphrases",
    "growth_story",
    "biggest_revision_night",
    "real_save_clock",
    "rescued_decks",
    "creation_seasons",
    "revision_sprints",
    "shape_distribution",
    "topic_constellation",
    "library_map",
    "filename_extremes",
    "age_extremes",
    "filename_dna",
    "same_name_twins",
    "sleeping_revival",
    "opening_ending",
    "daily_memory",
    "meeting_runtime",
    "growth_balance",
    "common_page_count",
    "deepest_path",
    "peak_day",
    "generic_names",
    "punctuation_personality",
    "most_renamed",
    "most_migrated",
    "page_flip_flop",
    "repeated_sentence",
    "language_persona",
    "light_ending",
    "keyword_trends",
    "anniversaries",
    "paper_stack",
    "achievements",
    "library_one_liner",
)

# 首屏报告的内容分析预算。真实片库实测 2M 字会把首次报告拖到 6 秒以上；
# 40 万字已足够产出稳定的高频主题，同时把一次性 CPU 峰值压到约 1 秒量级。
MAX_CONTENT_PAGES = 2_500
MAX_CONTENT_CHARS = 400_000
MAX_PAGE_SAMPLE_CHARS = 1_500
_MEETING_MINUTES_PER_PAGE = 2
_PAPER_MM_PER_PAGE = 0.1
_MIN_VALID_MTIME = datetime(1980, 1, 1).timestamp()


@dataclass
class NamedMetric:
    name: str | None = None
    value: int | float | str = 0
    path: str | None = None
    detail: str = ""


@dataclass
class CountedItem:
    label: str
    count: int


@dataclass
class KeywordTrend:
    period: str
    terms: tuple[str, ...]


@dataclass
class GrowthPoint:
    ts: float
    page_count: int
    size: int


@dataclass
class RevisionSprint:
    name: str
    count: int
    start_ts: float
    end_ts: float


@dataclass
class HallOfFameStat:
    longest_filename: NamedMetric = field(default_factory=NamedMetric)
    shortest_filename: NamedMetric = field(default_factory=NamedMetric)
    oldest: NamedMetric = field(default_factory=NamedMetric)
    newest: NamedMetric = field(default_factory=NamedMetric)
    most_pages: NamedMetric = field(default_factory=NamedMetric)
    largest: NamedMetric = field(default_factory=NamedMetric)
    deepest_path: NamedMetric = field(default_factory=NamedMetric)
    busiest_day: NamedMetric = field(default_factory=NamedMetric)
    common_page_count: int = 0
    common_page_count_decks: int = 0
    today_memory: NamedMetric = field(default_factory=NamedMetric)
    anniversaries: tuple[NamedMetric, ...] = ()


@dataclass
class CreationInsightsStat:
    monthly_counts: tuple[CountedItem, ...] = ()
    yearly_counts: tuple[CountedItem, ...] = ()
    season_counts: tuple[CountedItem, ...] = ()


@dataclass
class ContentInsightsStat:
    sampled_pages: int = 0
    sampled_decks: int = 0
    sampled_chars: int = 0
    total_pages: int = 0
    sample_truncated: bool = False
    catchphrases: tuple[CountedItem, ...] = ()
    topics: tuple[CountedItem, ...] = ()
    opening_phrase: str = ""
    opening_count: int = 0
    ending_phrase: str = ""
    ending_count: int = 0
    repeated_sentence: str = ""
    repeated_sentence_count: int = 0
    language_persona: str = "叙事派"
    english_ratio: float = 0.0
    digit_ratio: float = 0.0
    question_marks: int = 0
    low_text_ending_count: int = 0
    keyword_trends: tuple[KeywordTrend, ...] = ()


@dataclass
class LibraryInsightsStat:
    shape_bins: dict[str, int] = field(default_factory=dict)
    top_folders: tuple[CountedItem, ...] = ()
    same_name_twin_groups: int = 0
    same_name_twin_files: int = 0
    same_name_examples: tuple[str, ...] = ()
    generic_name_count: int = 0
    generic_name_examples: tuple[str, ...] = ()
    punctuation_label: str = "清爽命名派"
    punctuation_counts: dict[str, int] = field(default_factory=dict)
    filename_terms: tuple[CountedItem, ...] = ()
    meeting_minutes: int = 0
    paper_height_mm: float = 0.0
    one_page_count: int = 0


@dataclass
class VersionInsightsStat:
    available: bool = False
    version_count: int = 0
    protected_docs: int = 0
    rollback_docs: int = 0
    recoverable_deleted_docs: int = 0
    most_edited_name: str | None = None
    most_edited_versions: int = 0
    growth_points: tuple[GrowthPoint, ...] = ()
    biggest_revision_name: str | None = None
    biggest_revision_score: int = 0
    biggest_revision_ts: float = 0.0
    biggest_revision_summary: str = ""
    save_heatmap: list[list[int]] = field(default_factory=lambda: [[0] * 24 for _ in range(7)])
    peak_revision_night: str | None = None
    peak_revision_night_count: int = 0
    revision_sprints: tuple[RevisionSprint, ...] = ()
    sleeping_revival_name: str | None = None
    sleeping_revival_days: int = 0
    growing_docs: int = 0
    slimming_docs: int = 0
    most_renamed_name: str | None = None
    most_renamed_count: int = 0
    most_migrated_name: str | None = None
    most_migrated_count: int = 0
    page_flip_flop_name: str | None = None
    page_flip_flops: int = 0


@dataclass
class EnhancedInsights:
    hall: HallOfFameStat
    creation: CreationInsightsStat
    content: ContentInsightsStat
    library: LibraryInsightsStat
    versions: VersionInsightsStat
    achievements: tuple[str, ...]
    one_liner: str


_STOP_WORDS = {
    "一个", "一些", "以及", "我们", "你们", "他们", "这个", "那个", "这些", "那些",
    "进行", "通过", "可以", "需要", "相关", "项目", "方案", "汇报", "工作", "内容",
    "ppt", "pptx", "final", "终版", "最终版", "版本", "页面", "谢谢", "the", "and",
    "for", "with", "from", "this", "that", "are", "was", "of", "to", "in", "a",
}
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9+._-]{1,20}|[\u4e00-\u9fff]{2,}")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\r\n]+")
_CHANGED_PAGES_RE = re.compile(r"改\s*(\d+)\s*页")
_GENERIC_NAME_RE = re.compile(
    r"^(?:演示文稿|presentation|新建(?:演示文稿)?|未命名|untitled|ppt)[ _-]*\d*$",
    re.IGNORECASE,
)


def _stem(name: str) -> str:
    return ntpath.splitext(str(name or ""))[0].strip()


@lru_cache(maxsize=8192)
def _terms(text: str) -> tuple[str, ...]:
    """轻量分词；仅报告后台使用，并缓存重复页/文件名。"""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    output: list[str] = []
    for chunk in _TERM_RE.findall(text):
        if chunk.isascii():
            token = chunk.strip("._-+")
            if len(token) >= 2 and token not in _STOP_WORDS and not token.isdigit():
                output.append(token)
            continue
        try:
            import jieba

            jieba.setLogLevel(logging.WARNING)
            candidates = jieba.cut(chunk, cut_all=False)
        except Exception:  # noqa: BLE001 - 分词器不可用时仍保证报告可用
            candidates = (chunk,)
        for candidate in candidates:
            token = str(candidate).strip().casefold()
            if len(token) >= 2 and token not in _STOP_WORDS:
                output.append(token)
    return tuple(output)


def _metric(file, value, *, detail: str = "") -> NamedMetric:
    if file is None:
        return NamedMetric()
    return NamedMetric(
        name=str(file.name),
        value=value,
        path=str(getattr(file, "path", "") or "") or None,
        detail=detail,
    )


def _path_depth(path: str) -> int:
    return len([part for part in re.split(r"[\\/]+", str(path or "")) if part and not part.endswith(":" )])


def hall_of_fame(files: list, *, now_ts: float | None = None) -> HallOfFameStat:
    if not files:
        return HallOfFameStat()
    now = datetime.fromtimestamp(now_ts if now_ts is not None else datetime.now().timestamp())
    longest = max(files, key=lambda f: (len(_stem(f.name)), f.name))
    shortest = min(files, key=lambda f: (len(_stem(f.name)), f.name))
    dated = [f for f in files if float(f.mtime or 0) >= _MIN_VALID_MTIME]
    oldest = min(dated, key=lambda f: (float(f.mtime), f.name)) if dated else None
    newest = max(dated, key=lambda f: (float(f.mtime), f.name)) if dated else None
    most_pages = max(files, key=lambda f: (int(f.page_count or 0), f.name))
    largest = max(files, key=lambda f: (int(f.size or 0), f.name))
    deepest = max(files, key=lambda f: (_path_depth(getattr(f, "path", "")), f.name))

    day_counts = Counter(datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d") for f in dated)
    busy_day, busy_count = (
        max(day_counts.items(), key=lambda item: (item[1], item[0])) if day_counts else (None, 0)
    )
    page_counts = Counter(int(f.page_count or 0) for f in files if int(f.page_count or 0) > 0)
    common_pages, common_count = (
        max(page_counts.items(), key=lambda item: (item[1], -item[0])) if page_counts else (0, 0)
    )

    anniversary_files = [
        f for f in dated
        if datetime.fromtimestamp(f.mtime).month == now.month
        and datetime.fromtimestamp(f.mtime).day == now.day
        and datetime.fromtimestamp(f.mtime).year < now.year
    ]
    anniversary_files.sort(key=lambda f: f.mtime, reverse=True)
    anniversaries = tuple(
        _metric(
            f,
            now.year - datetime.fromtimestamp(f.mtime).year,
            detail=datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d"),
        )
        for f in anniversary_files[:5]
    )
    return HallOfFameStat(
        longest_filename=_metric(longest, len(_stem(longest.name))),
        shortest_filename=_metric(shortest, len(_stem(shortest.name))),
        oldest=_metric(oldest, oldest.mtime if oldest else 0.0),
        newest=_metric(newest, newest.mtime if newest else 0.0),
        most_pages=_metric(most_pages, int(most_pages.page_count or 0)),
        largest=_metric(largest, int(largest.size or 0)),
        deepest_path=_metric(deepest, _path_depth(getattr(deepest, "path", ""))),
        busiest_day=NamedMetric(name=busy_day, value=busy_count, detail="份胶片"),
        common_page_count=common_pages,
        common_page_count_decks=common_count,
        today_memory=anniversaries[0] if anniversaries else NamedMetric(),
        anniversaries=anniversaries,
    )


def creation_insights(files: list) -> CreationInsightsStat:
    dated = [f for f in files if float(f.mtime or 0) >= _MIN_VALID_MTIME]
    months = Counter(datetime.fromtimestamp(f.mtime).strftime("%Y-%m") for f in dated)
    years = Counter(datetime.fromtimestamp(f.mtime).strftime("%Y") for f in dated)
    seasons = Counter(
        f"Q{(datetime.fromtimestamp(f.mtime).month - 1) // 3 + 1}"
        for f in dated
    )
    return CreationInsightsStat(
        monthly_counts=tuple(CountedItem(k, v) for k, v in sorted(months.items())),
        yearly_counts=tuple(CountedItem(k, v) for k, v in sorted(years.items())),
        season_counts=tuple(CountedItem(k, seasons.get(k, 0)) for k in ("Q1", "Q2", "Q3", "Q4")),
    )


def library_insights(files: list) -> LibraryInsightsStat:
    bins = {
        "未解析": 0,
        "1 页": 0,
        "2–5 页": 0,
        "6–15 页": 0,
        "16–30 页": 0,
        "31–50 页": 0,
        "50+ 页": 0,
    }
    names: dict[str, list] = defaultdict(list)
    folders = Counter()
    generic: list[str] = []
    filename_terms = Counter()
    punctuation = Counter({"下划线派": 0, "横杠派": 0, "括号派": 0, "版本号派": 0, "空格派": 0})
    total_pages = 0
    for f in files:
        pages = max(0, int(f.page_count or 0))
        total_pages += pages
        if pages <= 0:
            bins["未解析"] += 1
        elif pages == 1:
            bins["1 页"] += 1
        elif pages <= 5:
            bins["2–5 页"] += 1
        elif pages <= 15:
            bins["6–15 页"] += 1
        elif pages <= 30:
            bins["16–30 页"] += 1
        elif pages <= 50:
            bins["31–50 页"] += 1
        else:
            bins["50+ 页"] += 1
        names[str(f.name).casefold()].append(f)
        parent = ntpath.dirname(str(getattr(f, "path", "") or ""))
        if parent:
            folders[parent] += 1
        stem = _stem(f.name)
        if _GENERIC_NAME_RE.fullmatch(stem):
            generic.append(str(f.name))
        filename_terms.update(_terms(stem))
        punctuation["下划线派"] += stem.count("_")
        punctuation["横杠派"] += stem.count("-")
        punctuation["括号派"] += sum(stem.count(ch) for ch in "()（）[]【】")
        punctuation["版本号派"] += len(re.findall(r"(?i)(?:^|[^a-z])v\d+|版本\d+", stem))
        punctuation["空格派"] += len(re.findall(r"\s+", stem))

    twins = [(group[0].name, len(group)) for group in names.values() if len(group) >= 2]
    twins.sort(key=lambda item: (-item[1], item[0]))
    dominant, dominant_count = max(punctuation.items(), key=lambda item: (item[1], item[0]))
    punctuation_label = dominant if dominant_count else "清爽命名派"
    return LibraryInsightsStat(
        shape_bins=bins,
        top_folders=tuple(CountedItem(k, v) for k, v in folders.most_common(8)),
        same_name_twin_groups=len(twins),
        same_name_twin_files=sum(count for _name, count in twins),
        same_name_examples=tuple(name for name, _count in twins[:5]),
        generic_name_count=len(generic),
        generic_name_examples=tuple(sorted(generic)[:5]),
        punctuation_label=punctuation_label,
        punctuation_counts=dict(punctuation),
        filename_terms=tuple(CountedItem(k, v) for k, v in filename_terms.most_common(10)),
        meeting_minutes=total_pages * _MEETING_MINUTES_PER_PAGE,
        paper_height_mm=total_pages * _PAPER_MM_PER_PAGE,
        one_page_count=bins["1 页"],
    )


def _scope_sql(year: int | None, since_ts: float | None, until_ts: float | None):
    ph = ",".join("?" * len(PPT_EXTS))
    predicates = [f"lower(f.ext) IN ({ph})"]
    params: list[object] = [e.lower() for e in PPT_EXTS]
    if year is not None:
        predicates.extend(("f.mtime>=?", "f.mtime<?"))
        params.extend((datetime(year, 1, 1).timestamp(), datetime(year + 1, 1, 1).timestamp()))
    if since_ts is not None:
        predicates.append("f.mtime>=?")
        params.append(float(since_ts))
    if until_ts is not None:
        predicates.append("f.mtime<?")
        params.append(float(until_ts))
    return " AND ".join(predicates), params


def content_insights(
    conn: sqlite3.Connection,
    *,
    year: int | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> ContentInsightsStat:
    where, params = _scope_sql(year, since_ts, until_ts)
    total_row = conn.execute(
        f"""SELECT COUNT(*) AS n
            FROM pages_raw AS p JOIN files AS f ON f.id=p.file_id
            WHERE {where}""",
        tuple(params),
    ).fetchone()
    total_pages = int(total_row["n"] if total_row else 0)
    rows = conn.execute(
        f"""SELECT p.file_id, p.page_no,
                   substr(COALESCE(p.raw_text,''),1,{MAX_PAGE_SAMPLE_CHARS}) AS raw_text,
                   f.page_count, f.mtime
            FROM pages_raw AS p JOIN files AS f ON f.id=p.file_id
            WHERE {where}
            ORDER BY f.mtime DESC, p.file_id, p.page_no
            LIMIT {MAX_CONTENT_PAGES}""",
        tuple(params),
    ).fetchall()

    selected = []
    sampled_chars = 0
    for row in rows:
        text = str(row["raw_text"] or "")
        if selected and sampled_chars + len(text) > MAX_CONTENT_CHARS:
            break
        selected.append(row)
        sampled_chars += len(text)

    word_counts = Counter()
    doc_terms: dict[int, set[str]] = defaultdict(set)
    opening_counts = Counter()
    ending_counts = Counter()
    sentence_counts = Counter()
    trend_counts: dict[str, Counter] = defaultdict(Counter)
    ascii_letters = digits = questions = visible = 0
    low_text_endings = 0
    for row in selected:
        text = str(row["raw_text"] or "")
        terms = _terms(text)
        word_counts.update(terms)
        doc_terms[int(row["file_id"])].update(terms)
        row_mtime = float(row["mtime"] or 0)
        if row_mtime >= _MIN_VALID_MTIME:
            period = datetime.fromtimestamp(row_mtime).strftime("%Y-%m")
            trend_counts[period].update(terms)
        if int(row["page_no"] or 0) == 1 and terms:
            opening_counts[" · ".join(terms[:3])] += 1
        if int(row["page_no"] or 0) == int(row["page_count"] or 0):
            if terms:
                ending_counts[" · ".join(terms[-3:])] += 1
            if len(re.sub(r"\s+", "", text)) <= 30:
                low_text_endings += 1
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            cleaned = re.sub(r"\s+", " ", sentence).strip()
            if 6 <= len(cleaned) <= 80:
                sentence_counts[cleaned] += 1
        ascii_letters += sum(ch.isascii() and ch.isalpha() for ch in text)
        digits += sum(ch.isdigit() for ch in text)
        questions += text.count("?") + text.count("？")
        visible += sum(not ch.isspace() for ch in text)

    topic_counts = Counter()
    for unique_terms in doc_terms.values():
        topic_counts.update(unique_terms)
    opening, opening_n = opening_counts.most_common(1)[0] if opening_counts else ("", 0)
    ending, ending_n = ending_counts.most_common(1)[0] if ending_counts else ("", 0)
    repeated, repeated_n = sentence_counts.most_common(1)[0] if sentence_counts else ("", 0)
    english_ratio = ascii_letters / visible if visible else 0.0
    digit_ratio = digits / visible if visible else 0.0
    if digit_ratio >= 0.12:
        language_persona = "数据派"
    elif english_ratio >= 0.22:
        language_persona = "国际范"
    elif questions >= max(3, len(selected) // 5):
        language_persona = "追问型"
    else:
        language_persona = "叙事派"
    trends = tuple(
        KeywordTrend(period, tuple(term for term, _count in trend_counts[period].most_common(3)))
        for period in sorted(trend_counts)[-8:]
        if trend_counts[period]
    )
    return ContentInsightsStat(
        sampled_pages=len(selected),
        sampled_decks=len(doc_terms),
        sampled_chars=sampled_chars,
        total_pages=total_pages,
        sample_truncated=len(selected) < total_pages,
        catchphrases=tuple(CountedItem(k, v) for k, v in word_counts.most_common(10)),
        topics=tuple(CountedItem(k, v) for k, v in topic_counts.most_common(10)),
        opening_phrase=opening,
        opening_count=opening_n,
        ending_phrase=ending,
        ending_count=ending_n,
        repeated_sentence=repeated,
        repeated_sentence_count=repeated_n,
        language_persona=language_persona,
        english_ratio=english_ratio,
        digit_ratio=digit_ratio,
        question_marks=questions,
        low_text_ending_count=low_text_endings,
        keyword_trends=trends,
    )


def _version_scope(year: int | None, since_ts: float | None, until_ts: float | None):
    predicates = ["COALESCE(v.health,'ok')='ok'"]
    params: list[object] = []
    if year is not None:
        predicates.extend(("v.ts>=?", "v.ts<?"))
        params.extend((datetime(year, 1, 1).timestamp(), datetime(year + 1, 1, 1).timestamp()))
    if since_ts is not None:
        predicates.append("v.ts>=?")
        params.append(float(since_ts))
    if until_ts is not None:
        predicates.append("v.ts<?")
        params.append(float(until_ts))
    return " AND ".join(predicates), params


def _open_version_db_ro(path: str | Path) -> sqlite3.Connection | None:
    path = Path(path)
    if not path.is_file():
        return None
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:" ) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _revision_score(row, previous) -> int:
    summary = str(row["changed"] or "")
    match = _CHANGED_PAGES_RE.search(summary)
    changed_pages = int(match.group(1)) if match else 0
    page_delta = abs(int(row["page_count"] or 0) - int(previous["page_count"] or 0)) if previous else 0
    size_delta = abs(int(row["size"] or 0) - int(previous["size"] or 0)) if previous else 0
    size_signal = min(20, size_delta // (5 * 1024 * 1024))
    return changed_pages + page_delta + size_signal


def _best_sprint(name: str, rows: list[sqlite3.Row]) -> RevisionSprint | None:
    if len(rows) < 2:
        return None
    best = (0, 0, 0)
    left = 0
    window = 72 * 3600
    for right, row in enumerate(rows):
        while float(row["ts"]) - float(rows[left]["ts"]) > window:
            left += 1
        candidate = (right - left + 1, left, right)
        if candidate[0] > best[0]:
            best = candidate
    count, start, end = best
    if count < 2:
        return None
    return RevisionSprint(name, count, float(rows[start]["ts"]), float(rows[end]["ts"]))


def version_insights(
    version_db_path: str | Path | None,
    *,
    year: int | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> VersionInsightsStat:
    if not version_db_path:
        return VersionInsightsStat()
    try:
        conn = _open_version_db_ro(version_db_path)
    except (OSError, sqlite3.Error):
        return VersionInsightsStat()
    if conn is None:
        return VersionInsightsStat()
    try:
        where, params = _version_scope(year, since_ts, until_ts)
        rows = conn.execute(
            f"""SELECT v.*, d.path AS doc_path, d.status AS doc_status
                FROM versions AS v
                JOIN managed_docs AS d ON d.doc_id=v.doc_id
                WHERE {where}
                ORDER BY v.doc_id, v.ts, v.version_id""",
            tuple(params),
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            groups[str(row["doc_id"])].append(row)
        names = {
            doc_id: ntpath.basename(str(doc_rows[-1]["doc_path"] or "")) or doc_id
            for doc_id, doc_rows in groups.items()
        }

        save_heatmap = [[0] * 24 for _ in range(7)]
        night_counts = Counter()
        biggest = (0, None, None)
        revival = (0.0, None)
        growing = slimming = 0
        flip_best = (0, None)
        sprints: list[RevisionSprint] = []
        for doc_id, doc_rows in groups.items():
            previous = None
            signs: list[int] = []
            for row in doc_rows:
                dt = datetime.fromtimestamp(float(row["ts"] or 0))
                save_heatmap[dt.weekday()][dt.hour] += 1
                if dt.hour >= 18 or dt.hour < 6:
                    night_date = dt.date() - timedelta(days=1) if dt.hour < 6 else dt.date()
                    night_counts[night_date.isoformat()] += 1
                score = _revision_score(row, previous)
                if score > biggest[0]:
                    biggest = (score, row, previous)
                if previous is not None:
                    gap = float(row["ts"] or 0) - float(previous["ts"] or 0)
                    if gap > revival[0]:
                        revival = (gap, doc_id)
                    delta = int(row["page_count"] or 0) - int(previous["page_count"] or 0)
                    if delta:
                        signs.append(1 if delta > 0 else -1)
                previous = row
            first_pages = int(doc_rows[0]["page_count"] or 0)
            last_pages = int(doc_rows[-1]["page_count"] or 0)
            if last_pages > first_pages:
                growing += 1
            elif last_pages < first_pages:
                slimming += 1
            flips = sum(a != b for a, b in zip(signs, signs[1:]))
            if flips > flip_best[0]:
                flip_best = (flips, doc_id)
            sprint = _best_sprint(names[doc_id], doc_rows)
            if sprint:
                sprints.append(sprint)

        top_doc_id, top_rows = (None, [])
        if groups:
            top_doc_id, top_rows = max(groups.items(), key=lambda item: (len(item[1]), names[item[0]]))
        growth_points = tuple(
            GrowthPoint(float(r["ts"] or 0), int(r["page_count"] or 0), int(r["size"] or 0))
            for r in top_rows[-100:]
        )
        peak_night, peak_night_count = (
            max(night_counts.items(), key=lambda item: (item[1], item[0])) if night_counts else (None, 0)
        )

        path_rows = []
        try:
            path_rows = conn.execute("SELECT doc_id, path FROM doc_paths").fetchall()
        except sqlite3.Error:
            pass
        aliases: dict[str, list[str]] = defaultdict(list)
        for row in path_rows:
            if str(row["doc_id"]) in groups:
                aliases[str(row["doc_id"])].append(str(row["path"] or ""))
        renamed = (0, None)
        migrated = (0, None)
        for doc_id in groups:
            paths = aliases.get(doc_id) or [str(groups[doc_id][-1]["doc_path"] or "")]
            name_count = len({ntpath.basename(path).casefold() for path in paths if path})
            folder_count = len({ntpath.normcase(ntpath.dirname(path)) for path in paths if path})
            if name_count > renamed[0]:
                renamed = (name_count, doc_id)
            if folder_count > migrated[0]:
                migrated = (folder_count, doc_id)

        biggest_row = biggest[1]
        return VersionInsightsStat(
            available=True,
            version_count=len(rows),
            protected_docs=sum(bool(doc_rows) for doc_rows in groups.values()),
            rollback_docs=sum(len(doc_rows) >= 2 for doc_rows in groups.values()),
            recoverable_deleted_docs=sum(
                bool(doc_rows) and str(doc_rows[-1]["doc_status"] or "") == "deleted"
                for doc_rows in groups.values()
            ),
            most_edited_name=names.get(top_doc_id) if top_doc_id else None,
            most_edited_versions=len(top_rows),
            growth_points=growth_points,
            biggest_revision_name=(names.get(str(biggest_row["doc_id"])) if biggest_row else None),
            biggest_revision_score=int(biggest[0]),
            biggest_revision_ts=float(biggest_row["ts"] or 0) if biggest_row else 0.0,
            biggest_revision_summary=str(biggest_row["changed"] or "") if biggest_row else "",
            save_heatmap=save_heatmap,
            peak_revision_night=peak_night,
            peak_revision_night_count=int(peak_night_count),
            revision_sprints=tuple(sorted(sprints, key=lambda s: (-s.count, s.start_ts))[:5]),
            sleeping_revival_name=names.get(revival[1]) if revival[1] else None,
            sleeping_revival_days=int(revival[0] // 86400),
            growing_docs=growing,
            slimming_docs=slimming,
            most_renamed_name=names.get(renamed[1]) if renamed[1] else None,
            most_renamed_count=renamed[0],
            most_migrated_name=names.get(migrated[1]) if migrated[1] else None,
            most_migrated_count=migrated[0],
            page_flip_flop_name=names.get(flip_best[1]) if flip_best[1] else None,
            page_flip_flops=flip_best[0],
        )
    except sqlite3.Error:
        return VersionInsightsStat()
    finally:
        conn.close()


def _achievements(files: list, hall, content, library, versions) -> tuple[str, ...]:
    badges: list[str] = []
    total_pages = sum(max(0, int(f.page_count or 0)) for f in files)
    if files:
        badges.append("胶片建档人")
    if len(files) >= 100:
        badges.append("百片馆长")
    if total_pages >= 1000:
        badges.append("千页导演")
    if versions.version_count >= 10:
        badges.append("时光机常客")
    if versions.recoverable_deleted_docs:
        badges.append("失而复得守护者")
    if content.question_marks >= 20:
        badges.append("灵魂提问官")
    if hall.busiest_day.value and int(hall.busiest_day.value) >= 10:
        badges.append("一日十片")
    if library.generic_name_count == 0 and files:
        badges.append("命名清醒者")
    return tuple(badges or ("等待第一卷胶片",))


def build_enhanced_insights(
    conn: sqlite3.Connection,
    files: list,
    *,
    year: int | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
    version_db_path: str | Path | None = None,
    now_ts: float | None = None,
    persona_role: str = "胶片创作者",
) -> EnhancedInsights:
    hall = hall_of_fame(files, now_ts=now_ts)
    creation = creation_insights(files)
    library = library_insights(files)
    content = content_insights(conn, year=year, since_ts=since_ts, until_ts=until_ts)
    versions = version_insights(
        version_db_path,
        year=year,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    badges = _achievements(files, hall, content, library, versions)
    one_liner = (
        f"你是一位{persona_role or '胶片创作者'}：片库里有 {len(files)} 份 PPT、"
        f"{sum(max(0, int(f.page_count or 0)) for f in files):,} 页，"
        f"时光机留下 {versions.version_count:,} 个可读版本点。"
    )
    return EnhancedInsights(hall, creation, content, library, versions, badges, one_liner)
