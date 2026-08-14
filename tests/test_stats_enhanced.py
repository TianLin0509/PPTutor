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
    assert len(stats.STAT_FEATURE_KEYS) == 38
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
        "career_chronicle",
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


def test_yearly_chronicle_buckets_metrics_keywords_and_version_saves(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put(conn, r"C:\work\2019 星云观测.pptx", mtime=_ts(2019, 3, 5), pages=["nebula 观测记录"], size=100)
    _put(conn, r"C:\work\2026 类星体.pptx", mtime=_ts(2026, 2, 1), pages=["quasar 能谱"] * 2, size=9000)
    _put(conn, r"C:\work\2026 类星体复盘.pptx", mtime=_ts(2026, 8, 2), pages=["quasar 复盘", "谢谢"], size=3000)
    conn.commit()

    vpath = tmp_path / "versions.db"
    vault = store.connect(vpath)
    store.init_db(vault)
    store.upsert_doc(vault, "doc-old", r"C:\work\2019 星云观测.pptx", _ts(2019, 3, 5))
    _add_version(vault, "doc-old", "o1", _ts(2019, 3, 5, 21), 1, 100, "")
    store.upsert_doc(vault, "doc-new", r"C:\work\2026 类星体.pptx", _ts(2026, 2, 1))
    _add_version(vault, "doc-new", "n1", _ts(2026, 2, 2, 9), 2, 200, "")
    _add_version(vault, "doc-new", "n2", _ts(2026, 2, 3, 9), 2, 220, "改 1 页")
    vault.commit()
    vault.close()

    files = stats.fetch_file_stats(conn)
    chronicle = report_insights.yearly_chronicle(conn, files, version_db_path=vpath)

    assert [c.year for c in chronicle] == [2019, 2026]
    c2019, c2026 = chronicle
    assert (c2019.deck_count, c2019.page_count) == (1, 1)
    assert (c2026.deck_count, c2026.page_count) == (2, 4)
    assert c2019.char_count == len("nebula 观测记录")
    assert c2026.char_count == 2 * len("quasar 能谱") + len("quasar 复盘") + len("谢谢")
    assert (c2019.total_size, c2026.total_size) == (100, 12000)
    labels2019 = [k.label for k in c2019.top_keywords]
    labels2026 = [k.label for k in c2026.top_keywords]
    assert "nebula" in labels2019 and "nebula" not in labels2026
    assert "quasar" in labels2026 and "quasar" not in labels2019
    assert (c2019.version_saves, c2026.version_saves) == (1, 2)
    assert c2019.top_files[0].name == "2019 星云观测.pptx"
    assert c2019.top_files[0].path == r"C:\work\2019 星云观测.pptx"
    assert c2026.top_files[0].name == "2026 类星体.pptx"  # 页数并列时体积大的在前

    # build_report 直接把生涯履历挂上 Report
    report = stats.build_report(conn, version_db_path=vpath)
    assert [c.year for c in report.chronicle] == [2019, 2026]
    assert report.chronicle[1].version_saves == 2


def test_yearly_chronicle_still_samples_early_year_when_recent_year_is_huge(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put(conn, r"C:\work\2010 老胶片.pptx", mtime=_ts(2010, 6, 1), pages=["oldschool 开场"], size=100)
    _put(
        conn,
        r"C:\work\2026 大部头.pptx",
        mtime=_ts(2026, 6, 1),
        pages=[f"newwave 第{i}页" for i in range(1, 601)],
        size=100,
    )
    conn.commit()

    files = stats.fetch_file_stats(conn)
    by_year = {c.year: c for c in report_insights.yearly_chronicle(conn, files)}

    # 全局 ORDER BY mtime DESC LIMIT 的抽法下 2010 年一页都轮不到；按年分层则每年各有额度
    assert "oldschool" in [k.label for k in by_year[2010].top_keywords]
    assert by_year[2026].top_keywords


def test_yearly_chronicle_skips_files_without_real_mtime(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put(conn, r"C:\work\无时间.pptx", mtime=0.0, pages=["nebula"], size=100)
    conn.commit()

    files = stats.fetch_file_stats(conn)

    assert report_insights.yearly_chronicle(conn, files) == ()


def test_yearly_chronicle_version_saves_follow_report_scope(tmp_path):
    """月/周 scope 下年卡留版数必须按 scope 过滤，而不是全年口径（M1 回归）。

    2026 年共 12 次健康留版，其中只有 1 次落在 6 月；「全部」「本年」scope
    下年卡留版数与改动前一致（12），「本月」scope 下必须是 1。
    """
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put(conn, r"C:\work\2026 六月甲.pptx", mtime=_ts(2026, 6, 3), pages=["june 复盘"], size=100)
    _put(conn, r"C:\work\2026 六月乙.pptx", mtime=_ts(2026, 6, 20), pages=["june 周报"], size=100)
    conn.commit()

    vpath = tmp_path / "versions.db"
    vault = store.connect(vpath)
    store.init_db(vault)
    store.upsert_doc(vault, "doc-j", r"C:\work\2026 六月甲.pptx", _ts(2026, 6, 3))
    _add_version(vault, "doc-j", "j1", _ts(2026, 6, 5, 20), 2, 200, "")  # 唯一落在 6 月的留版
    for k in range(11):  # 其余 11 次散落在 1~5 月
        _add_version(vault, "doc-j", f"x{k}", _ts(2026, 1 + (k % 5), 10 + k, 9), 2, 200, "")
    vault.commit()
    vault.close()

    # 「全部」scope：全年 12 次（与改动前口径一致）
    report_all = stats.build_report(conn, version_db_path=vpath)
    assert [c.year for c in report_all.chronicle] == [2026]
    assert report_all.chronicle[0].version_saves == 12
    # 「本年」scope：同样 12 次（year 边界本就正确）
    report_year = stats.build_report(conn, year=2026, version_db_path=vpath)
    assert report_year.chronicle[0].version_saves == 12
    # 「本月」scope（2026-06）：文件与留版都按月过滤
    report_month = stats.build_report(
        conn,
        since_ts=_ts(2026, 6, 1, 0),
        until_ts=_ts(2026, 7, 1, 0),
        version_db_path=vpath,
    )
    assert [c.year for c in report_month.chronicle] == [2026]
    assert report_month.chronicle[0].deck_count == 2
    assert report_month.chronicle[0].version_saves == 1


def test_yearly_chronicle_version_saves_none_when_version_db_unavailable(tmp_path):
    """版本库缺失/不可读时留版数为 None（未知），不是 0（n1 降级口径）。"""
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put(conn, r"C:\work\2024 星云.pptx", mtime=_ts(2024, 5, 1), pages=["nebula"], size=100)
    conn.commit()
    files = stats.fetch_file_stats(conn)

    # 未传 version_db_path
    assert report_insights.yearly_chronicle(conn, files)[0].version_saves is None
    # 路径不存在
    missing = report_insights.yearly_chronicle(conn, files, version_db_path=tmp_path / "no-such.db")
    assert missing[0].version_saves is None
    # 文件存在但不是合法 SQLite 库
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite db")
    corrupt = report_insights.yearly_chronicle(conn, files, version_db_path=bad)
    assert corrupt[0].version_saves is None
