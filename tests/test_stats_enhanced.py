"""胶片报告增强统计：文件、内容与真实版本元数据口径。"""
from __future__ import annotations

from datetime import datetime

from pptx_finder import db, report_insights, stats
from pptx_finder.versioning import store


def _ts(y: int, mo: int, d: int, h: int = 10) -> float:
    return datetime(y, mo, d, h).timestamp()


def _put(
    conn,
    path: str,
    *,
    mtime: float,
    pages: list[str],
    size: int = 1000,
):
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    fid = db.upsert_file(
        conn,
        path=path,
        name=name,
        ext=".pptx",
        size=size,
        mtime=mtime,
        content_hash="h-" + name + str(mtime),
        page_count=len(pages),
        status="ok",
        error="",
        indexed_at=mtime + 1,
    )
    db.replace_pages(
        conn,
        fid,
        [(i, text, "token") for i, text in enumerate(pages, 1)],
    )
    return fid


def test_feature_manifest_covers_all_user_selected_statistics():
    assert len(stats.STAT_FEATURE_KEYS) == 37
    assert {
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
    } == set(stats.STAT_FEATURE_KEYS)


def test_build_report_adds_file_content_and_library_fun_stats(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    repeated = "客户价值持续增长，客户价值持续增长。"
    _put(
        conn,
        r"C:\Users\me\work\alpha\演示文稿1.pptx",
        mtime=_ts(2019, 7, 15),
        pages=["AI 战略开场。" + repeated],
        size=100,
    )
    _put(
        conn,
        r"C:\Users\me\work\alpha\AI 战略路线图终版.pptx",
        mtime=_ts(2026, 7, 15),
        pages=["AI 战略开场。" + repeated, "客户价值？AI SP 方案。", "谢谢。"],
        size=5000,
    )
    _put(
        conn,
        r"C:\Users\me\work\beta\AI 战略路线图终版.pptx",
        mtime=_ts(2026, 7, 15, 23),
        pages=["AI 战略开场。" + repeated] * 12,
        size=3000,
    )
    conn.commit()

    report = stats.build_report(conn, now_ts=_ts(2026, 7, 15, 12))

    assert report.hall.longest_filename.name == "AI 战略路线图终版.pptx"
    assert report.hall.shortest_filename.name == "演示文稿1.pptx"
    assert report.hall.oldest.name == "演示文稿1.pptx"
    assert report.hall.newest.name == "AI 战略路线图终版.pptx"
    assert report.hall.busiest_day.value == 2
    assert report.hall.today_memory.name == "演示文稿1.pptx"
    assert report.library.same_name_twin_groups == 1
    assert report.library.generic_name_count == 1
    assert report.library.one_page_count == 1
    assert report.library.shape_bins["6–15 页"] == 1
    assert report.library.meeting_minutes == 32
    assert report.library.paper_height_mm > 0
    assert report.content.sampled_pages == 16
    assert report.content.catchphrases
    assert report.content.topics
    assert report.content.opening_phrase
    assert report.content.ending_phrase
    assert report.content.repeated_sentence_count >= 2
    assert report.content.question_marks >= 1
    assert report.content.keyword_trends
    assert report.achievements
    assert report.one_liner


def _add_version(conn, doc_id: str, vid: str, ts: float, pages: int, size: int, changed: str):
    store.add_version(
        conn,
        vid,
        doc_id,
        ts,
        "s" + datetime.fromtimestamp(ts).strftime("%Y%m%d"),
        pages,
        size,
        "hash-" + vid,
        changed=changed,
    )
    store.set_latest(conn, doc_id, vid)


def test_build_report_uses_real_version_history_not_similarity_groups(tmp_path):
    index = db.connect(tmp_path / "index.db")
    db.init_db(index)
    _put(
        index,
        r"C:\work\真正反复改.pptx",
        mtime=_ts(2026, 7, 10),
        pages=["版本测试"],
    )
    index.commit()

    vpath = tmp_path / "versions.db"
    vault = store.connect(vpath)
    store.init_db(vault)
    store.upsert_doc(vault, "doc-a", r"C:\work\真正反复改.pptx", _ts(2026, 7, 1))
    _add_version(vault, "doc-a", "a1", _ts(2026, 7, 1, 22), 8, 100, "")
    _add_version(vault, "doc-a", "a2", _ts(2026, 7, 2, 1), 12, 160, "改 4 页 · +4 页")
    _add_version(vault, "doc-a", "a3", _ts(2026, 7, 2, 23), 10, 140, "改 6 页 · -2 页")
    _add_version(vault, "doc-a", "a4", _ts(2026, 7, 10, 10), 13, 200, "改 8 页 · +3 页")
    store.record_path(vault, "doc-a", r"C:\old\原名.pptx", _ts(2026, 7, 1), "alias")
    store.record_path(vault, "doc-a", r"D:\new\改名后.pptx", _ts(2026, 7, 10), "alias")

    store.upsert_doc(vault, "doc-deleted", r"C:\work\已删除但可恢复.pptx", _ts(2026, 7, 3))
    _add_version(vault, "doc-deleted", "d1", _ts(2026, 7, 3, 21), 5, 50, "")
    store.set_status(vault, "doc-deleted", "deleted")
    vault.commit()
    vault.close()

    report = stats.build_report(index, version_db_path=vpath, now_ts=_ts(2026, 7, 15))
    v = report.versions

    assert v.available is True
    assert v.most_edited_name == "真正反复改.pptx"
    assert v.most_edited_versions == 4
    assert v.version_count == 5
    assert v.rollback_docs == 1
    assert v.recoverable_deleted_docs == 1
    assert len(v.growth_points) == 4
    assert v.biggest_revision_name == "真正反复改.pptx"
    assert v.biggest_revision_score >= 8
    assert v.save_heatmap[2][22] == 1  # 2026-07-01 Wednesday, 22:00
    assert v.peak_revision_night_count >= 1
    assert v.revision_sprints
    assert v.sleeping_revival_days >= 7
    assert v.most_renamed_count >= 2
    assert v.most_migrated_count >= 2
    assert v.page_flip_flops >= 1
    assert v.growing_docs == 1


def test_missing_version_db_degrades_to_explicit_unavailable(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    report = stats.build_report(conn, version_db_path=tmp_path / "missing.db")
    assert report.versions.available is False
    assert report.versions.version_count == 0


def test_unknown_zero_mtime_is_not_presented_as_a_1970_creation_record():
    unknown = stats.FileStat("unknown.pptx", 0.0, 10, 1, "ok", None, 10, path="C:/unknown.pptx")
    real = stats.FileStat(
        "real.pptx",
        _ts(2024, 5, 20),
        10,
        1,
        "ok",
        None,
        10,
        path="C:/real.pptx",
    )

    activity = stats.activity([unknown, real])
    hall = report_insights.hall_of_fame([unknown, real], now_ts=_ts(2026, 7, 15))
    creation = report_insights.creation_insights([unknown, real])

    assert activity.first_mtime == real.mtime
    assert activity.active_days == 1
    assert hall.oldest.name == "real.pptx"
    assert [item.label for item in creation.yearly_counts] == ["2024"]
