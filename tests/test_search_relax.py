from __future__ import annotations

import time

import pytest

from pptx_finder import db, namequery, namestore, search, search_relax
from pptx_finder.models import FileResult
from pptx_finder.ranking import relevance_components, sort_results
from pptx_finder.text_tokenize import tokenize


BASE_T = 1_700_000_000


def _store(tmp_path, entries):
    builder = namestore.NameStoreBuilder()
    for path, size, mtime, is_dir in entries:
        if is_dir:
            builder.add_dir(path, mtime)
        else:
            builder.add(path, size, mtime)
    return namestore.NameStore(builder.write(tmp_path / "names.idx"))


def _add(conn, name: str, text: str, mtime: float):
    fid = db.upsert_file(
        conn,
        path=f"C:/relax/{name}",
        name=name,
        ext=".pptx",
        size=100,
        mtime=mtime,
        content_hash=f"h:{name}",
        page_count=1,
        status="ok",
        error="",
        indexed_at=mtime,
    )
    db.replace_pages(conn, fid, [(1, text, tokenize(text))])
    return fid


def test_alias_catalog_maps_baidu_cloud_to_baidu_netdisk():
    assert "百度网盘" in search_relax.alias_expansions("百度云")


def test_alias_catalog_substitutes_inside_a_compound_plain_query():
    assert "百度网盘 工作资料" in search_relax.alias_expansions("百度云 工作资料")


def test_short_cjk_query_uses_aliases_but_not_broad_corpus_suggestions():
    values = search_relax.automatic_relaxations("百度云", ["百度", "百度文心"])
    assert "百度网盘" in [value.value for value in values]
    assert "百度文心" not in [value.value for value in values]


def test_any_strict_content_result_beats_a_relaxed_filename_result():
    strict_content = FileResult(
        1, "C:/strict.pptx", "strict.pptx", ".pptx", BASE_T, 1, 1,
        "ok", 0.1, False,
    )
    relaxed_name = FileResult(
        -1, "C:/fuzzy.txt", "fuzzy.txt", ".txt", BASE_T, 1, 0,
        "filename_only", 99.0, True, relaxed=True, relaxed_kind="fuzzy",
    )
    assert relevance_components(strict_content) < relevance_components(relaxed_name)


@pytest.mark.parametrize("descending", [False, True])
def test_column_sort_never_moves_relaxed_result_above_literal_result(descending):
    strict = FileResult(
        1, "C:/百度云.txt", "百度云.txt", ".txt", BASE_T, 1, 0,
        "filename_only", 0.1, True,
    )
    relaxed = FileResult(
        2, "C:/百度网盘.iso", "百度网盘.iso", ".iso", BASE_T + 100, 10_000, 0,
        "filename_only", 99.0, True, relaxed=True, relaxed_kind="alias",
    )

    rows = sort_results([relaxed, strict], ("size",), descending=descending)

    assert rows == [strict, relaxed]


def test_all_file_alias_is_automatic_but_strict_result_stays_first(tmp_path):
    with _store(tmp_path, [
        (r"C:\x\百度云.txt", 1, BASE_T, False),
        (r"C:\x\百度网盘.lnk", 2, BASE_T + 100, False),
    ]) as store:
        rows = search.search_names(
            store, "百度云", exists=lambda _path: True)

    assert [row.name for row in rows[:2]] == ["百度云.txt", "百度网盘.lnk"]
    assert rows[0].relaxed is False
    assert rows[1].relaxed is True
    assert rows[1].relaxed_kind == "alias"
    assert rows[1].relaxed_query == "百度网盘"


def test_all_file_alias_fills_a_zero_result_query(tmp_path):
    with _store(tmp_path, [
        (r"C:\x\百度网盘.lnk", 2, BASE_T, False),
    ]) as store:
        rows = search.search_names(
            store, "百度云", exists=lambda _path: True)
    assert [row.name for row in rows] == ["百度网盘.lnk"]
    assert rows[0].relaxed is True


def test_all_file_generic_typo_uses_ngram_candidates(tmp_path):
    with _store(tmp_path, [
        (r"C:\x\resume.md", 2, BASE_T, False),
        (r"C:\x\unrelated.md", 2, BASE_T, False),
    ]) as store:
        rows = search.search_names(
            store, "resmue", exists=lambda _path: True)
    assert rows and rows[0].name == "resume.md"
    assert rows[0].relaxed_kind == "fuzzy"


def test_fuzzy_gate_rejects_shared_prefix_with_opposite_long_tail():
    assert search_relax.fuzzy_name_score(
        "重复清理待删除项", "重复清理保留项.pptx") == 0.0


def test_explicit_all_file_syntax_is_never_relaxed(tmp_path):
    with _store(tmp_path, [
        (r"C:\x\resume.md", 2, BASE_T, False),
    ]) as store:
        rows = search.search_names(
            store, "ext:md", exists=lambda _path: True)
    assert rows and all(not row.relaxed for row in rows)


def test_ppt_alias_searches_filename_and_content_but_strict_stays_first(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _add(conn, "百度云.pptx", "literal strict file", BASE_T)
    _add(conn, "项目说明.pptx", "请上传到百度网盘共享目录", BASE_T + 100)
    conn.commit()

    rows = search.search(conn, "百度云")
    conn.close()

    assert rows[0].name == "百度云.pptx"
    assert rows[0].relaxed is False
    linked = next(row for row in rows if row.name == "项目说明.pptx")
    assert linked.relaxed is True
    assert linked.relaxed_kind == "alias"
    assert linked.hits and "百度网盘" in linked.hits[0].snippet


def test_ppt_typo_uses_full_index_ngram_fallback_even_without_suggester(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "typo.db")
    db.init_db(conn)
    _add(conn, "算力方案.pptx", "正常正文", BASE_T)
    conn.commit()
    monkeypatch.setattr(search, "suggest_queries", lambda *_a, **_k: [])

    rows = search.search(conn, "算力方按")
    conn.close()

    assert rows and rows[0].name == "算力方案.pptx"
    assert rows[0].relaxed is True
    assert rows[0].relaxed_kind == "fuzzy"


def test_ppt_content_typo_uses_full_index_ngram_fallback(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "content-typo.db")
    db.init_db(conn)
    _add(conn, "普通项目.pptx", "本页介绍算力方案和部署步骤", BASE_T)
    conn.commit()
    monkeypatch.setattr(search, "suggest_queries", lambda *_a, **_k: [])

    rows = search.search(conn, "算力方按")
    conn.close()

    assert rows and rows[0].name == "普通项目.pptx"
    assert rows[0].relaxed_kind == "fuzzy"
    assert rows[0].hits


def test_size_sort_is_global_before_the_200_row_cut(tmp_path):
    entries = [
        (rf"C:\x\report-{i:03d}.bin", 1, BASE_T, False)
        for i in range(200)
    ]
    entries.append((r"C:\x\report-zzz.bin", 4_000_000_000, BASE_T, False))
    with _store(tmp_path, entries) as store:
        rows = search.search_names(
            store, "report", sort_keys=("size",), exists=lambda _path: True)
    assert rows[0].name == "report-zzz.bin"
    assert rows[0].size == 4_000_000_000


def test_u64_size_filter_finds_files_larger_than_4gb(tmp_path):
    size = 10 * 1024 ** 3
    with _store(tmp_path, [(r"C:\x\huge.iso", size, BASE_T, False)]) as store:
        hits = store.search(namequery.parse("size:>4gb"))
        assert hits == [0]
        assert store.entry(0)[2] == size


def test_empty_size_does_not_treat_every_folder_as_an_empty_file(tmp_path):
    with _store(tmp_path, [
        (r"C:\x\folder", 0, BASE_T, True),
        (r"C:\x\empty.txt", 0, BASE_T, False),
    ]) as store:
        names = [store.entry(i)[1] for i in store.search(namequery.parse("size:empty"))]
    assert names == ["empty.txt"]


def test_unicode_regex_prefilter_never_loses_string_matches(tmp_path):
    with _store(tmp_path, [
        (r"C:\x\Ärger.txt", 1, BASE_T, False),
        (r"C:\x\中文.txt", 1, BASE_T, False),
        (r"C:\x\abc.txt", 1, BASE_T, False),
    ]) as store:
        assert [store.entry(i)[1] for i in store.search(namequery.parse("regex:ä"))] == ["Ärger.txt"]
        names = [store.entry(i)[1] for i in store.search(namequery.parse(r"regex:^\w+\.txt$"))]
    assert names == ["Ärger.txt", "中文.txt", "abc.txt"]


def test_catastrophic_regex_is_bounded_per_record():
    record = namequery.Record(
        name="a" * 80 + "X",
        name_norm="a" * 80 + "x",
        path="x",
        size=1,
        mtime=BASE_T,
        is_dir=False,
    )
    query = namequery.parse(r"regex:^(a|aa)+$")
    started = time.perf_counter()
    try:
        query.match(record)
    except namequery.QueryError as exc:
        assert "超时" in str(exc)
    assert time.perf_counter() - started < 0.5


def test_all_file_runtime_regex_timeout_is_not_silently_reported_as_zero_results():
    class TimedOutStore:
        def search(self, *_args, **_kwargs):
            raise namequery.QueryError("正则执行超时，请简化表达式")

    with pytest.raises(namequery.QueryError, match="超时"):
        search.search_names(TimedOutStore(), "regex:unsafe")
