# -*- coding: utf-8 -*-
"""「全部文件」范围的搜索：平铺索引 → FileResult。

这一层的职责是让「换了存储引擎」对上层完全不可见：排序口径、命中分级、
大小写规则必须与 SQLite 那条路一致，而且不能踩到下游按 file_id 归组的桶。
"""
from __future__ import annotations

import pytest

from pptx_finder import namestore, search as _search_mod


class search:  # noqa: N801 - 薄封装：默认旁路存在性检查
    ANY_FILE_NAME_LIMIT = _search_mod.ANY_FILE_NAME_LIMIT

    @staticmethod
    def search_names(store, query, **kw):
        kw.setdefault('exists', lambda _p: True)
        return _search_mod.search_names(store, query, **kw)

    @staticmethod
    def search(*a, **kw):
        return _search_mod.search(*a, **kw)

from pptx_finder.ranking import relevance_components
from pptx_finder.ui.result_utils import sort_results


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def _store(entries):
    b = namestore.NameStoreBuilder()
    for path, size, mtime in entries:
        b.add(path, size, mtime)
    return namestore.NameStore(b.write())


BASE_T = 1_700_000_000


def test_returns_filename_only_results_with_no_content(tmp_path):
    with _store([(r"C:\a\预算表.xlsx", 10, BASE_T)]) as store:
        (r,) = search.search_names(store, "预算")
    assert r.path == r"C:\a\预算表.xlsx"
    assert r.name == "预算表.xlsx"
    assert r.ext == ".xlsx"
    assert r.size == 10
    assert r.status == "filename_only"
    assert r.name_hit is True
    assert r.hits == []          # 这个范围没有内容命中，永远是空的
    assert r.page_count == 0


def test_exact_filename_beats_prefix_beats_contains(tmp_path):
    """与内容搜索同一套文件名命中分级：完全匹配 > 前缀 > 普通包含。"""
    with _store([
        (r"C:\a\zzz-report-archive.txt", 1, BASE_T + 300),
        (r"C:\a\report.txt", 1, BASE_T),
        (r"C:\a\report-2026.txt", 1, BASE_T + 100),
    ]) as store:
        got = [r.name for r in search.search_names(store, "report")]
    assert got[0] == "report.txt"
    assert got[1] == "report-2026.txt"
    assert got[2] == "zzz-report-archive.txt"


def test_newer_wins_when_match_quality_ties(tmp_path):
    with _store([
        (r"C:\a\draft-old-notes.txt", 1, BASE_T),
        (r"C:\a\draft-new-notes.txt", 1, BASE_T + 9999),
    ]) as store:
        got = [r.name for r in search.search_names(store, "draft")]
    assert got == ["draft-new-notes.txt", "draft-old-notes.txt"]


def test_multiple_terms_must_all_be_in_the_name(tmp_path):
    with _store([
        (r"C:\a\alpha beta.txt", 1, BASE_T),
        (r"C:\a\alpha only.txt", 1, BASE_T),
        (r"C:\a\beta only.txt", 1, BASE_T),
    ]) as store:
        got = [r.name for r in search.search_names(store, "alpha beta")]
    assert got == ["alpha beta.txt"]


def test_ext_filter_applies(tmp_path):
    with _store([
        (r"C:\a\notes.txt", 1, BASE_T),
        (r"C:\a\notes.md", 1, BASE_T),
    ]) as store:
        got = [r.name for r in search.search_names(store, "notes", exts=(".md",))]
    assert got == ["notes.md"]


def test_scope_limits_to_a_directory_subtree(tmp_path):
    with _store([
        (r"C:\keep\notes.txt", 1, BASE_T),
        (r"C:\drop\notes.txt", 1, BASE_T),
    ]) as store:
        got = [r.path for r in search.search_names(store, "notes", scope=r"C:\keep")]
    assert got == [r"C:\keep\notes.txt"]


def test_limit_truncates_after_ranking_not_before(tmp_path):
    """先排序再截断。反过来的话截出来的是磁盘遍历顺序里靠前的那些，
    跟相关度毫无关系——这正是要留住的行为。"""
    entries = [(rf"C:\a\zz-item{i:03d}.txt", 1, BASE_T + i) for i in range(50)]
    entries.append((r"C:\a\item.txt", 1, BASE_T))       # 完全匹配，但排在最后录入
    with _store(entries) as store:
        got = search.search_names(store, "item", limit=3)
    assert len(got) == 3
    assert got[0].name == "item.txt"


def test_empty_query_returns_nothing(tmp_path):
    with _store([(r"C:\a\x.txt", 1, BASE_T)]) as store:
        assert search.search_names(store, "") == []
        assert search.search_names(store, "   ") == []


# ---------------------------------------------------------------- file_id 契约

def test_file_ids_are_negative_so_db_lookups_miss(tmp_path):
    """下游有两处直接 `WHERE file_id=?`（页标题、复制本页文字）。这些结果没有
    数据库行，id 必须是负数——查空是对的，撞上某个真实 PPT 的行就会把别人的
    内容显示成这个文件的。"""
    with _store([(rf"C:\a\doc{i}.txt", 1, BASE_T + i) for i in range(5)]) as store:
        results = search.search_names(store, "doc")
    assert results
    assert all(r.file_id < 0 for r in results)


def test_file_ids_are_distinct(tmp_path):
    """search.py 和 ui/result_utils.py 都拿 f"s{file_id}" 当归组桶键。全体共用
    一个 id 会让所有结果塌进同一个桶、被整体提到首条位置，排序当场作废。"""
    with _store([(rf"C:\a\doc{i}.txt", 1, BASE_T + i) for i in range(40)]) as store:
        results = search.search_names(store, "doc")
    ids = [r.file_id for r in results]
    assert len(set(ids)) == len(ids)


def test_ui_resort_preserves_order(tmp_path):
    """UI 的相关度二次排序会按 file_id 归组。桶键要是撞了，这里的顺序就会变。"""
    with _store([
        (r"C:\a\report.txt", 1, BASE_T),
        (r"C:\a\report-2026.txt", 1, BASE_T + 100),
        (r"C:\a\zzz-report-archive.txt", 1, BASE_T + 300),
    ]) as store:
        results = search.search_names(store, "report")
    before = [r.name for r in results]
    after = [r.name for r in sort_results(list(results), "relevance")]
    assert after == before


def test_group_id_stays_none(tmp_path):
    """归组只处理 status='ok' 的内容行；这些结果不该带组号，否则会被当成
    某个 PPT 的版本组成员。"""
    with _store([(r"C:\a\x.txt", 1, BASE_T)]) as store:
        (r,) = search.search_names(store, "x.txt")
    assert r.group_id is None
    assert r.is_latest is False


def test_relevance_components_match_the_content_path_shape(tmp_path):
    """排序元组必须能被共用的 relevance_components 正确解读：
    文件名来源恒为第 0 档。"""
    with _store([(r"C:\a\Report.txt", 1, BASE_T)]) as store:
        (r,) = search.search_names(store, "Report")
    tier = relevance_components(r)
    assert tier[0] == 0            # 文件名来源
    assert tier[1] == 0            # 大小写一致
    assert tier[2] == 0            # filename_exact


def test_case_sensitive_signal_demotes_folded_matches(tmp_path):
    """查询里带了大小写信号时，大小写一致的排在前面——与内容搜索同规则。"""
    with _store([
        (r"C:\a\readme.txt", 1, BASE_T + 100),
        (r"C:\a\README.txt", 1, BASE_T),
    ]) as store:
        got = [r.name for r in search.search_names(store, "README")]
    assert got[0] == "README.txt"


def test_full_name_with_extension_is_an_exact_match(tmp_path):
    """打全名（含扩展名）也该算「就是它」。这个范围里什么扩展名都有，
    去扩展名不能只认 .pptx/.ppt。"""
    with _store([
        (r"C:\a\report.txt", 1, BASE_T),
        (r"C:\a\report-2026.txt", 1, BASE_T + 999),
    ]) as store:
        got = [r.name for r in search.search_names(store, "report.txt")]
    assert got[0] == "report.txt"


def test_exact_beats_a_much_newer_partial(tmp_path):
    """「名字就是它」必须压过「更新但只是包含」——否则按名字找文件毫无意义。"""
    with _store([
        (r"C:\a\config.json", 1, BASE_T),
        (r"C:\a\my-config-backup.json", 1, BASE_T + 10_000_000),
    ]) as store:
        got = [r.name for r in search.search_names(store, "config")]
    assert got[0] == "config.json"


# ---------------------------------------------------------------- 文件夹结果

def test_folders_come_back_as_results(tmp_path):
    b = namestore.NameStoreBuilder()
    b.add_dir(r"C:\work\季度汇报", BASE_T)
    b.add(r"C:\work\季度汇报\deck.pptx", 100, BASE_T)
    with namestore.NameStore(b.write()) as store:
        (r,) = search.search_names(store, "季度汇报")
    assert r.is_dir is True
    assert r.path == r"C:\work\季度汇报"
    assert r.ext == ""
    assert r.size == 0
    assert r.page_count == 0


def test_ext_filter_excludes_folders(tmp_path):
    """选了「只看 PDF」还冒出一堆目录会很莫名其妙。"""
    b = namestore.NameStoreBuilder()
    b.add_dir(r"C:\x\notes", BASE_T)
    b.add(r"C:\x\notes.pdf", 10, BASE_T)
    with namestore.NameStore(b.write()) as store:
        got = search.search_names(store, "notes", exts=(".pdf",))
    assert [r.name for r in got] == ["notes.pdf"]
    assert all(r.is_dir is False for r in got)


def test_content_results_are_never_marked_as_folders(tmp_path):
    """内容搜索那条路不该被这个新字段碰到——PPT 相关行为一个字节都不变。"""
    from pptx_finder import db

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(
        conn, path=str(tmp_path / "deck.pptx"), name="deck.pptx", ext=".pptx",
        size=1, mtime=BASE_T, content_hash="stat:1", page_count=1,
        status="ok", error="", indexed_at=BASE_T,
    )
    db.replace_pages(conn, fid, [(1, "产品路线图", "产 品 路 线 图")])
    conn.commit()
    results = search.search(conn, "路线图")
    conn.close()
    assert results
    assert all(r.is_dir is False for r in results)
    assert all(r.file_id > 0 for r in results)      # 内容结果仍是真实正数 id


# ---------------------------------------------------------------- 已删除的文件

def test_deleted_files_disappear_before_the_next_rebuild(tmp_path):
    """平铺索引是整份重建的，两次重建之间删掉的文件仍留在里面。

    与其为删除单独维护一套增量账本，不如在**要显示的那几条**上顺手 stat 一下。
    不做这件事的话，用户会看到一堆点开就报「文件不存在」的结果。
    """
    live = tmp_path / "still-here.txt"
    gone = tmp_path / "deleted-later.txt"
    live.write_text("x", encoding="utf-8")
    gone.write_text("x", encoding="utf-8")

    b = namestore.NameStoreBuilder()
    b.add(str(live), 1, BASE_T)
    b.add(str(gone), 1, BASE_T + 100)
    with namestore.NameStore(b.write()) as store:
        both = _search_mod.search_names(store, "here OR later")
        assert {r.name for r in _search_mod.search_names(store, ".txt")} == {
            "still-here.txt", "deleted-later.txt"}
        gone.unlink()
        after = _search_mod.search_names(store, ".txt")
    assert [r.name for r in after] == ["still-here.txt"]
    assert both is not None


def test_deletion_filter_still_fills_up_to_limit(tmp_path):
    """边过滤边取够 limit，而不是先截断再过滤——后者会平白少给用户几条。"""
    kept = []
    b = namestore.NameStoreBuilder()
    for i in range(10):
        f = tmp_path / f"item{i:02d}.txt"
        if i % 2 == 0:
            f.write_text("x", encoding="utf-8")
            kept.append(f.name)
        b.add(str(f), 1, BASE_T + i)     # 奇数号从来没在磁盘上出现过
    with namestore.NameStore(b.write()) as store:
        got = _search_mod.search_names(store, "item", limit=3)
    assert len(got) == 3
    assert all(r.name in kept for r in got)


def test_folders_that_no_longer_exist_are_filtered_too(tmp_path):
    d = tmp_path / "vanishing"
    d.mkdir()
    b = namestore.NameStoreBuilder()
    b.add_dir(str(d), BASE_T)
    path = b.write()
    with namestore.NameStore(path) as store:
        assert len(_search_mod.search_names(store, "vanishing")) == 1
        d.rmdir()
        assert _search_mod.search_names(store, "vanishing") == []


# ---------------------------------------------------------------- 实时更新（增量层）

def test_new_file_is_findable_via_the_overlay(tmp_path):
    """全量索引之后新建的文件，靠增量层立刻能搜到。

    这就是 Everything 拿不到 NTFS 变更日志时的做法：目录变更通知 → 重列该目录。
    没有这一层的话，新文件要等下一轮全盘扫描（最坏一周）才进得了结果。
    """
    old = tmp_path / "already-indexed.txt"
    old.write_text("x", encoding="utf-8")
    b = namestore.NameStoreBuilder()
    b.add(str(old), 1, BASE_T)
    b.write()

    fresh = tmp_path / "just-created.txt"
    fresh.write_text("x", encoding="utf-8")
    o = namestore.NameStoreBuilder()
    o.add(str(fresh), 1, BASE_T + 900)
    o.write(kind=namestore.OVERLAY)

    stores = [namestore.open_store(k) for k in namestore.KINDS]
    try:
        got = {r.name for r in _search_mod.search_names(stores, ".txt")}
    finally:
        for s in stores:
            s.close()
    assert got == {"already-indexed.txt", "just-created.txt"}


def test_a_file_in_both_layers_appears_once(tmp_path):
    """同一个文件同时在全量和增量层里时只能出一条，且用增量层里的新数据。"""
    f = tmp_path / "edited.txt"
    f.write_text("x", encoding="utf-8")
    b = namestore.NameStoreBuilder()
    b.add(str(f), 111, BASE_T)
    b.write()
    o = namestore.NameStoreBuilder()
    o.add(str(f), 999, BASE_T + 500)
    o.write(kind=namestore.OVERLAY)

    stores = [namestore.open_store(k) for k in namestore.KINDS]
    try:
        got = _search_mod.search_names(stores, "edited")
    finally:
        for s in stores:
            s.close()
    assert len(got) == 1
    assert got[0].size == 999            # 增量层在后，覆盖全量里的旧值
    assert got[0].mtime == BASE_T + 500


def test_overlay_alone_works_when_there_is_no_full_index(tmp_path):
    """还没建过全量索引时，增量层自己也要能用（首次启动的窗口期）。"""
    f = tmp_path / "solo.txt"
    f.write_text("x", encoding="utf-8")
    o = namestore.NameStoreBuilder()
    o.add(str(f), 1, BASE_T)
    o.write(kind=namestore.OVERLAY)
    stores = [s for s in (namestore.open_store(k) for k in namestore.KINDS) if s]
    try:
        assert [r.name for r in _search_mod.search_names(stores, "solo")] == ["solo.txt"]
    finally:
        for s in stores:
            s.close()
