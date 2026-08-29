# -*- coding: utf-8 -*-
"""「全部文件」范围的查询语法（照搬 Everything）。

这套语法**只服务全文件搜索**。PPT 内容搜索用的是 text_tokenize.parse_query，
一个字都没动——两者的用户习惯完全不同，混用只会互相拖累。
"""
from __future__ import annotations

import datetime as dt

import pytest

from pptx_finder import namequery
from pptx_finder.namequery import QueryError, Record, parse

NOW = dt.datetime(2026, 8, 28, 12, 0, 0)
DAY = 86400


def _epoch(y, m, d):
    return int(dt.datetime(y, m, d).timestamp())


def rec(name, *, path=None, size=100, mtime=None, is_dir=False):
    from pptx_finder.text_tokenize import normalize
    path = path or rf"C:\work\{name}"
    return Record(name=name, name_norm=normalize(name), path=path,
                  size=size, mtime=mtime if mtime is not None else _epoch(2026, 8, 1),
                  is_dir=is_dir)


def hit(query, record, *, now=NOW):
    return parse(query, now=now).match(record)


# ---------------------------------------------------------------- 基本

def test_bare_term_is_a_substring_match():
    assert hit("report", rec("Q1-report-final.pdf"))
    assert not hit("budget", rec("Q1-report-final.pdf"))


def test_space_is_and():
    assert hit("q1 final", rec("Q1-report-final.pdf"))
    assert not hit("q1 draft", rec("Q1-report-final.pdf"))


def test_case_insensitive_by_default():
    assert hit("REPORT", rec("Q1-report-final.pdf"))


def test_quoted_phrase_is_literal():
    assert hit('"a b"', rec("x a b y.txt"))
    assert not hit('"a b"', rec("a-b.txt"))


def test_quoted_wildcards_are_literal_not_patterns():
    r"""引号里的 * 就是星号本身。Everything 也是这么约定的。"""
    assert hit('"a*b"', rec("a*b.txt"))
    assert not hit('"a*b"', rec("axxb.txt"))


# ---------------------------------------------------------------- 通配符

def test_star_matches_the_whole_name():
    """用了通配符就是匹配**整个名字**——所以 abc 是包含，abc* 是以 abc 开头。"""
    assert hit("*.pdf", rec("report.pdf"))
    assert not hit("*.pdf", rec("report.pdf.bak"))
    assert hit("report*", rec("report-2026.pdf"))
    assert not hit("report*", rec("my-report.pdf"))


def test_question_mark_matches_exactly_one_char():
    assert hit("a?c.txt", rec("abc.txt"))
    assert not hit("a?c.txt", rec("ac.txt"))
    assert not hit("a?c.txt", rec("abbc.txt"))


def test_wildcard_in_the_middle():
    assert hit("q1*final*", rec("Q1-report-final.pdf"))
    assert not hit("q1*draft*", rec("Q1-report-final.pdf"))


def test_wildcard_is_case_insensitive():
    assert hit("*.PDF", rec("report.pdf"))
    assert hit("*.pdf", rec("REPORT.PDF"))


# ---------------------------------------------------------------- 布尔

def test_or_operator():
    assert hit("alpha|beta", rec("alpha.txt"))
    assert hit("alpha|beta", rec("beta.txt"))
    assert not hit("alpha|beta", rec("gamma.txt"))


def test_not_operator():
    assert hit("report !draft", rec("report-final.pdf"))
    assert not hit("report !draft", rec("report-draft.pdf"))


def test_grouping_with_angle_brackets():
    assert hit("<alpha|beta> report", rec("alpha-report.txt"))
    assert not hit("<alpha|beta> report", rec("gamma-report.txt"))


def test_grouping_with_parentheses():
    assert hit("(alpha|beta) report", rec("beta-report.txt"))


def test_and_binds_tighter_than_or():
    """`a b|c` 应当读作 `(a AND b) OR c`，与 Everything 一致。"""
    assert hit("alpha beta|gamma", rec("alpha-beta.txt"))
    assert hit("alpha beta|gamma", rec("gamma.txt"))
    assert not hit("alpha beta|gamma", rec("alpha.txt"))


def test_double_negation():
    assert hit("!!report", rec("report.txt"))


# ---------------------------------------------------------------- ext:

def test_ext_filter():
    assert hit("ext:pdf", rec("report.pdf"))
    assert not hit("ext:pdf", rec("report.pdfx"))
    assert not hit("ext:pdf", rec("report.txt"))


def test_ext_accepts_multiple_and_a_leading_dot():
    assert hit("ext:pdf;docx", rec("a.docx"))
    assert hit("ext:.pdf", rec("a.pdf"))
    assert not hit("ext:pdf;docx", rec("a.txt"))


def test_ext_never_matches_a_folder():
    """文件夹没有扩展名。`ext:pdf` 冒出一堆目录会很莫名其妙。"""
    assert not hit("ext:pdf", rec("something.pdf", is_dir=True))


def test_ext_combines_with_a_name_term():
    assert hit("report ext:pdf", rec("q1-report.pdf"))
    assert not hit("report ext:pdf", rec("q1-budget.pdf"))


# ---------------------------------------------------------------- size:

@pytest.mark.parametrize("query,size,want", [
    ("size:>10mb", 20 * 1024 ** 2, True),
    ("size:>10mb", 5 * 1024 ** 2, False),
    ("size:<1kb", 500, True),
    ("size:<1kb", 5000, False),
    ("size:>=1024", 1024, True),
    ("size:<=1024", 1024, True),
    ("size:=0", 0, True),
    ("size:0", 0, True),
    ("size:1kb..10kb", 5 * 1024, True),
    ("size:1kb..10kb", 20 * 1024, False),
    ("size:empty", 0, True),
    ("size:empty", 1, False),
    ("size:tiny", 5 * 1024, True),
    ("size:gigantic", 200 * 1024 ** 2, True),
    ("size:gigantic", 1024, False),
])
def test_size_filter(query, size, want):
    assert hit(query, rec("x.bin", size=size)) is want


def test_empty_function():
    assert hit("empty:", rec("x.bin", size=0))
    assert not hit("empty:", rec("x.bin", size=10))


def test_size_units_are_case_insensitive():
    assert hit("size:>1MB", rec("x", size=2 * 1024 ** 2))
    assert hit("size:>1mb", rec("x", size=2 * 1024 ** 2))


def test_bad_size_is_a_query_error_not_a_crash():
    with pytest.raises(QueryError):
        parse("size:banana")


# ---------------------------------------------------------------- dm:

def test_dm_today():
    assert hit("dm:today", rec("x", mtime=_epoch(2026, 8, 28) + 3600))
    assert not hit("dm:today", rec("x", mtime=_epoch(2026, 8, 27) + 3600))


def test_dm_yesterday():
    assert hit("dm:yesterday", rec("x", mtime=_epoch(2026, 8, 27) + 60))


def test_dm_named_ranges():
    assert hit("dm:thisyear", rec("x", mtime=_epoch(2026, 3, 3)))
    assert not hit("dm:thisyear", rec("x", mtime=_epoch(2025, 3, 3)))
    assert hit("dm:lastyear", rec("x", mtime=_epoch(2025, 3, 3)))
    assert hit("dm:thismonth", rec("x", mtime=_epoch(2026, 8, 2)))
    assert not hit("dm:thismonth", rec("x", mtime=_epoch(2026, 7, 30)))
    assert hit("dm:lastmonth", rec("x", mtime=_epoch(2026, 7, 30)))


def test_dm_absolute_dates_and_comparisons():
    assert hit("dm:2026", rec("x", mtime=_epoch(2026, 5, 5)))
    assert not hit("dm:2026", rec("x", mtime=_epoch(2025, 5, 5)))
    assert hit("dm:2026-05", rec("x", mtime=_epoch(2026, 5, 20)))
    assert hit("dm:2026-05-20", rec("x", mtime=_epoch(2026, 5, 20) + 100))
    assert hit("dm:>2026-01-01", rec("x", mtime=_epoch(2026, 5, 5)))
    assert not hit("dm:>2026-01-01", rec("x", mtime=_epoch(2025, 5, 5)))


def test_dm_range_covers_the_whole_end_period():
    """`dm:2026-01..2026-06` 必须**包含**整个六月，不能停在 6 月 1 日零点。"""
    assert hit("dm:2026-01..2026-06", rec("x", mtime=_epoch(2026, 6, 28)))
    assert not hit("dm:2026-01..2026-06", rec("x", mtime=_epoch(2026, 7, 1)))


def test_datemodified_is_an_alias_for_dm():
    assert hit("datemodified:2026", rec("x", mtime=_epoch(2026, 5, 5)))


def test_bad_date_is_a_query_error():
    with pytest.raises(QueryError):
        parse("dm:notadate")


# ---------------------------------------------------------------- 路径

def test_path_function_matches_the_full_path():
    r = rec("deck.pptx", path=r"C:\work\季度汇报\deck.pptx")
    assert hit("path:季度汇报", r)
    assert not hit("季度汇报", r)          # 不带 path: 时只看文件名


def test_a_separator_in_the_query_turns_on_path_matching():
    r"""打 `工作\汇报` 的人显然在描述位置。Everything 也是这么自动切换的。"""
    r = rec("deck.pptx", path=r"C:\work\汇报\deck.pptx")
    assert hit(r"work\汇报", r)
    assert hit("work/汇报", r)
    r2 = rec("deck.pptx", path=r"C:\other\place\deck.pptx")
    assert not hit(r"work\汇报", r2)


def test_path_matching_does_not_leak_into_plain_terms():
    """一个叫 reports 的目录不能把里面每个文件都拖成命中。"""
    r = rec("deck.pptx", path=r"C:\reports\deck.pptx")
    assert not hit("reports", r)


# ---------------------------------------------------------------- 类型 / 大小写 / 全词

def test_file_and_folder_functions():
    assert hit("folder:", rec("bin", is_dir=True))
    assert not hit("folder:", rec("bin.txt"))
    assert hit("file:", rec("bin.txt"))
    assert not hit("file:", rec("bin", is_dir=True))


def test_case_function_forces_case_sensitivity():
    assert hit("case:README", rec("README.md"))
    assert not hit("case:README", rec("readme.md"))
    assert hit("readme", rec("README.md"))     # 默认仍不区分


def test_whole_word_function():
    assert hit("ww:log", rec("build log.txt"))
    assert not hit("ww:log", rec("catalogue.txt"))
    assert hit("log", rec("catalogue.txt"))    # 默认仍是子串


def test_regex_function():
    assert hit(r"regex:^\d{4}-report", rec("2026-report.pdf"))
    assert not hit(r"regex:^\d{4}-report", rec("report-2026.pdf"))


def test_bad_regex_is_a_query_error():
    with pytest.raises(QueryError):
        parse("regex:[unclosed")


# ---------------------------------------------------------------- 组合

def test_realistic_combined_query():
    r = rec("Q3-财报-final.pdf", path=r"C:\work\2026\Q3-财报-final.pdf",
            size=3 * 1024 ** 2, mtime=_epoch(2026, 8, 20))
    assert hit("财报 ext:pdf size:>1mb dm:2026 !draft", r)
    assert not hit("财报 ext:pdf size:>10mb dm:2026", r)
    assert not hit("财报 ext:docx", r)


def test_unknown_function_name_is_treated_as_plain_text():
    """`版本:3` 里的冒号不是函数——不能因为写了冒号就当语法错误。"""
    assert hit("版本:3", rec("版本:3 的稿子.pptx"))


def test_empty_query_matches_nothing_usable():
    q = parse("")
    assert not q
    assert q.prefilter() is None


def test_unbalanced_group_is_a_query_error():
    with pytest.raises(QueryError):
        parse("<alpha")
    with pytest.raises(QueryError):
        parse("alpha>")


def test_dangling_or_is_a_query_error():
    with pytest.raises(QueryError):
        parse("|alpha")


# ---------------------------------------------------------------- 预筛计划

def test_prefilter_extracts_required_literals():
    """预筛决定扫得快不快：能抠出字面串就只扫候选，抠不出就得逐条全扫。"""
    assert parse("report").prefilter() == [["report"]]
    got = parse("q1 final").prefilter()
    assert got is not None and sorted(got[0]) == ["final", "q1"]


def test_prefilter_for_wildcards_uses_the_fixed_chunks():
    got = parse("*report*.pdf").prefilter()
    assert got is not None
    assert "report" in got[0] and ".pdf" in got[0]


def test_prefilter_for_ext_uses_the_dotted_extension():
    assert parse("ext:pdf").prefilter() == [[".pdf"]]
    assert parse("ext:pdf;docx").prefilter() == [[".pdf"], [".docx"]]


def test_prefilter_gives_up_for_pure_metadata_queries():
    """只写 size:/dm: 时抠不出任何名字片段，只能全扫——如实返回 None。"""
    assert parse("size:>1gb").prefilter() is None
    assert parse("dm:today").prefilter() is None


def test_prefilter_gives_up_under_negation():
    assert parse("!report").prefilter() is None


def test_prefilter_gives_up_for_path_terms():
    """路径字面串不出现在名字块里，拿它预筛会漏掉真命中。"""
    assert parse("path:work").prefilter() is None


def test_prefilter_or_branches_are_all_required():
    got = parse("alpha|beta").prefilter()
    assert got == [["alpha"], ["beta"]]


def test_prefilter_or_with_one_unfilterable_branch_falls_back():
    """只要有一支没法预筛，整条查询就得全扫，否则那一支的结果会丢。"""
    assert parse("alpha|size:>1gb").prefilter() is None


def test_from_terms_never_interprets_syntax():
    """内部调用要的是「给什么搜什么」，不能把用户名字里的 * 当通配符。"""
    q = namequery.from_terms(["a*b", "c|d"])
    assert q.match(rec("xa*bx c|d y.txt"))
    assert not q.match(rec("axxb cd.txt"))


def test_and_keeps_a_usable_prefilter_next_to_an_inner_or():
    """`<a|b> ext:png` 里 ext 仍是必要条件。早期版本在这里直接放弃预筛，
    真机 200 万条上这条查询要 3.8 秒。"""
    got = parse("<alpha|beta> ext:png").prefilter()
    assert got == [[".png"]]


def test_and_falls_back_to_the_narrowest_or_branch_set():
    """所有项都是「或」时，挑分支最少的那组当预筛——分支越少候选越小。"""
    got = parse("<a|b|c> <d|e>").prefilter()
    assert got is not None and len(got) == 2


def test_path_literals_only_walks_the_and_chain():
    assert parse("path:work").path_literals() == ["work"]
    assert parse("path:work report").path_literals() == ["work"]
    # 或 / 非 里的路径串不是必要条件，不能拿来筛
    assert parse("path:work|report").path_literals() == []
    assert parse("!path:work").path_literals() == []
    assert parse("report").path_literals() == []
