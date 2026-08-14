"""非 ASCII / 非常用汉字字符的搜索回归（2026-07-28）。

起因：用户报「τ 搜不到」。排查发现 `_TOKEN_RE` 只认 `[a-z0-9]+` 与基本汉字区
(U+4E00–U+9FFF)，其余字符一律当分隔符丢弃 —— 希腊字母、带音标拉丁、假名、
韩文、CJK 扩展 A 生僻字全部切不出 token，`build_fts_match` 直接返回空串，
搜索在进 SQL 之前就废了。

本文件锁三件事：
1. 这些字符现在能切出 token、能端到端搜到；
2. 中文逐字 / 英数 trigram 两条老路径没有被改坏；
3. 「非内容字符」的定义只有一份（`SEPARATOR_CLASS`），search 层复用而不是
   再抄一遍 —— 同一假设散落多处正是本次 bug 的成因。
"""
from __future__ import annotations

import pytest

import fixtures_gen as fx

from pptx_finder import db, indexer, search
from pptx_finder.text_tokenize import (
    SEPARATOR_CLASS,
    _base_tokens,
    build_fts_match,
)


def _build(tmp_path, files):
    docs = tmp_path / "d"
    docs.mkdir(exist_ok=True)
    for fn, bodies in files.items():
        fx.make_pptx(docs / fn, [{"body": b} for b in bodies])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


def _names(res):
    return [r.name for r in res]


# --- 1. 切词层：这些字符必须切得出 token -------------------------------------

@pytest.mark.parametrize(
    "char",
    [
        "τ", "λ", "μ", "σ", "β", "π", "α", "ω",  # 希腊小写（无线/信号处理高频）
        "é", "ü", "ñ",                            # 带音标拉丁（欧洲人名/术语）
        "ひ", "カ",                                # 日文假名
        "한",                                      # 韩文
        "㐂", "䶮",                                # CJK 扩展 A（生僻姓氏用字）
    ],
)
def test_single_char_yields_token(char):
    assert _base_tokens(char) == [char.casefold()], f"{char} 被当成分隔符丢弃了"
    assert build_fts_match(char), f"{char} 的 FTS MATCH 为空 —— 搜索进不了 SQL"


def test_uppercase_greek_folds_to_lowercase():
    """Δ 与 δ 必须落到同一个 token，否则大小写写法不同就互相搜不到。"""
    assert _base_tokens("Δ") == _base_tokens("δ") == ["δ"]


def test_mixed_greek_and_chinese_keeps_both():
    """「τ值」原先只切出「值」—— 静默退化成搜「值」，用户不会察觉 τ 被吃了。"""
    assert _base_tokens("τ值") == ["τ", "值"]


# --- 2. 端到端：真 pptx 建索引后能搜到 ---------------------------------------

def test_greek_in_content_is_searchable(tmp_path):
    conn = _build(tmp_path, {"latency.pptx": ["时延 τ 等于 3ms"]})
    assert _names(search.search(conn, "τ")) == ["latency.pptx"]


def test_greek_in_filename_is_searchable(tmp_path):
    conn = _build(tmp_path, {"τ-analysis.pptx": ["占位"]})
    assert "τ-analysis.pptx" in _names(search.search(conn, "τ"))


def test_greek_combined_with_chinese(tmp_path):
    conn = _build(tmp_path, {"a.pptx": ["τ值分析"], "b.pptx": ["其他内容"]})
    assert _names(search.search(conn, "τ值")) == ["a.pptx"]


def test_cjk_ext_a_rare_char_searchable(tmp_path):
    """扩展 A 的生僻字（U+3400–U+4DBF）原先整条丢失 —— 生僻姓氏文件搜不到。"""
    conn = _build(tmp_path, {"name.pptx": ["㐂多方案"]})
    assert _names(search.search(conn, "㐂")) == ["name.pptx"]


def test_accented_latin_searchable(tmp_path):
    conn = _build(tmp_path, {"eu.pptx": ["Müller 的报告"]})
    assert _names(search.search(conn, "Müller")) == ["eu.pptx"]


def test_kana_searchable(tmp_path):
    conn = _build(tmp_path, {"jp.pptx": ["カタカナ 説明"]})
    assert _names(search.search(conn, "カタカナ")) == ["jp.pptx"]


def test_greek_query_does_not_match_unrelated(tmp_path):
    """能搜到不等于乱搜到：不含 τ 的文件不能被 τ 命中。"""
    conn = _build(tmp_path, {"has.pptx": ["τ 时延"], "none.pptx": ["普通时延"]})
    assert _names(search.search(conn, "τ")) == ["has.pptx"]


# --- 3. 回归：两条老路径不能被改坏 -------------------------------------------

def test_chinese_still_char_level(tmp_path):
    """中文仍是逐字 token + 相邻短语（子串召回），不能退化成整句一个词。"""
    assert _base_tokens("时延分析") == ["时", "延", "分", "析"]
    conn = _build(tmp_path, {"cn.pptx": ["时延分析报告"]})
    assert _names(search.search(conn, "延分")) == ["cn.pptx"]


def test_ascii_word_still_whole_token_with_trigram():
    """英数仍整词成 token，长词补 trigram 供子串召回（GPT4 命中 GPT4Turbo）。"""
    assert _base_tokens("latency") == ["latency"]
    assert '"lat"' in build_fts_match("latency")


def test_ascii_substring_recall_intact(tmp_path):
    conn = _build(tmp_path, {"m.pptx": ["GPT4Turbo 模型"]})
    assert _names(search.search(conn, "GPT4")) == ["m.pptx"]


def test_fullwidth_and_circled_still_normalized():
    """NFKC 归一化路径不受影响：① → 1、Ⅱ → ii、全角 → 半角。"""
    assert _base_tokens("①") == ["1"]
    assert _base_tokens("Ⅱ") == ["ii"]
    assert _base_tokens("ＡＩ") == ["ai"]


# --- 4. 结构约束：字符类定义只能有一份 ---------------------------------------

def test_search_layer_reuses_separator_class():
    """search 不许再抄一份「非内容字符」的定义。

    本次 bug 的成因就是 text_tokenize 和 search 各写了一套 `[^0-9a-z 汉字]`，
    只改一处等于没改。这条测试盯住复用关系，防止将来又被复制回去。
    """
    import inspect

    from pptx_finder import search as search_mod

    src = inspect.getsource(search_mod)
    assert "SEPARATOR_CLASS" in src, "search 应复用 text_tokenize.SEPARATOR_CLASS"
    # 旧的硬编码字符类不允许再出现（basic-CJK 加英数的否定类）
    assert "[^0-9a-z一-鿿]" not in src
    assert "[^0-9A-Za-z一-鿿]" not in src


def test_separator_class_treats_letters_as_content():
    """SEPARATOR_CLASS 只匹配标点/空白/下划线，不许吃掉任何字母。"""
    import re

    sep = re.compile(SEPARATOR_CLASS)
    for ch in "τλμΔaZ9中㐂éひ한":
        assert not sep.match(ch), f"{ch} 被误判成分隔符"
    for ch in " -_/.,":
        assert sep.match(ch), f"{ch} 应当是分隔符"
