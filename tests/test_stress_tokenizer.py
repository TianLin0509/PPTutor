"""分词器 Unicode 修复的压测与病态输入回归（2026-08-14 审查新增）。

覆盖 test_unicode_tokens.py 没覆盖的负载维度：
- 40 万字级混合 Unicode 文本（中日韩 + 希腊 + emoji + 组合音标）不崩、不超时；
- 全分隔符文档、交替字符等回溯诱捕输入下新 _TOKEN_RE 无灾难性回溯；
- 10 万级 token 文档端到端入库 + 搜索；
- 单字（希腊字母/高频汉字）查询的召回完整性与耗时上限；
- 新正则相对旧正则（git HEAD 版 [a-z0-9]+|[一-鿿]）的耗时回归护栏；
- 非 ASCII 数字（٤٥٦）与 compact 验证侧口径一致：逐字成 token、可端到端搜到
  （2026-08-14 修复：_TOKEN_RE 第三分支 [^\W\d_] → [^\W_]）。
"""
from __future__ import annotations

import re
import time
import unicodedata

import pytest

from pptx_finder import db, search
from pptx_finder.text_tokenize import (
    _TOKEN_RE,
    _base_tokens,
    normalize,
    tokenize,
)

# git HEAD（修复前）的切词正则，仅用于耗时对比护栏
_OLD_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")

# 耗时上限取本机实测值的 10~80 倍，只拦「灾难性退化」，不做精细性能断言
MIXED_400K_BUDGET_S = 10.0
HUGE_DOC_BUDGET_S = 25.0
SINGLE_CHAR_QUERY_BUDGET_S = 5.0


def _mixed_unicode_text(n_chars: int) -> str:
    """确定性构造：中日韩 + 希腊 + 假名 + 韩文 + 组合音标 + emoji + 分隔符。"""
    blocks = [
        "".join(chr(0x4E00 + (i % 0x500)) for i in range(40)),   # 基本区汉字
        "".join(chr(0x3400 + (i % 0x100)) for i in range(10)),   # 扩展 A
        "τλμσδπαωΩΣΔ",
        "あいうえおカタカナ",
        "한국어검색",
        "latency throughput gpt4turbo 2026 ",
        "éöåñ" * 3,            # 组合音标序列
        "😀🎉🚀👍🔥💯",
        " -_/.,:;|@# \t\n",
    ]
    out, i = [], 0
    total = 0
    while total < n_chars:
        s = blocks[i % len(blocks)]
        out.append(s)
        total += len(s)
        i += 1
    return "".join(out)[:n_chars]


def _add_page(conn, file_id: int, name: str, text: str, mtime: float = 100.0) -> int:
    fid = db.upsert_file(
        conn, path=f"C:/stress/{file_id}-{name}", name=name, ext=".pptx",
        size=len(text), mtime=mtime, content_hash=f"h{file_id}",
        page_count=1, status="ok", error="", indexed_at=mtime,
    )
    db.replace_pages(conn, fid, [(1, text, tokenize(text))])
    return fid


# --- 病态输入：不崩、不超时 -------------------------------------------------

def test_mixed_unicode_400k_chars_tokenize_bounded():
    """40 万字混合 Unicode：完整 tokenize 链路在预算内跑完且确有产出。"""
    text = _mixed_unicode_text(400_000)
    t0 = time.perf_counter()
    out = tokenize(text)
    dt = time.perf_counter() - t0
    assert dt < MIXED_400K_BUDGET_S, f"40 万字 tokenize 耗时 {dt:.2f}s 超预算"
    tokens = out.split()
    assert len(tokens) > 100_000
    # 各文字体系都必须出 token（修复的核心承诺）
    assert "τ" in tokens and "한" in tokens and "カ" in tokens


def test_all_separator_document_yields_no_tokens_fast():
    """全分隔符/符号/emoji 文档：零 token、快速返回（FTS 不会收到垃圾行）。"""
    text = (" —…·•※★☆😀🎉-_/|@# \t\n" * 20_000)[:400_000]
    t0 = time.perf_counter()
    assert _base_tokens(text) == []
    assert time.perf_counter() - t0 < MIXED_400K_BUDGET_S


def test_alternating_char_classes_no_catastrophic_backtracking():
    """a1 交替 20 万对 + 首尾汉字：交替字符类是交替型正则的经典回溯诱捕。"""
    text = "时延" + ("a1" * 100_000) + "τ" + ("z9" * 100_000)
    t0 = time.perf_counter()
    toks = _base_tokens(text)
    dt = time.perf_counter() - t0
    assert dt < MIXED_400K_BUDGET_S, f"疑似回溯爆炸：{dt:.2f}s"
    assert toks[0] == "时" and toks[1] == "延" and "τ" in toks


def test_pure_greek_400k_all_become_tokens():
    """纯希腊 40 万字：旧版全丢（0 token），新版逐字成 token，耗时仍有界。"""
    text = "τλμσδπαω" * 50_000
    t0 = time.perf_counter()
    toks = _base_tokens(text)
    dt = time.perf_counter() - t0
    assert len(toks) == 400_000
    assert dt < MIXED_400K_BUDGET_S


def test_new_regex_not_much_slower_than_old_on_large_text():
    """新旧正则在 20 万字混合文本上的耗时比护栏（同进程对比，抗机器差异）。"""
    text = normalize(_mixed_unicode_text(200_000))
    # 预热
    _OLD_TOKEN_RE.findall(text)
    _TOKEN_RE.findall(text)
    t0 = time.perf_counter(); _OLD_TOKEN_RE.findall(text); t_old = time.perf_counter() - t0
    t0 = time.perf_counter(); _TOKEN_RE.findall(text); t_new = time.perf_counter() - t0
    assert t_new < max(t_old * 3.0, 1.0), f"new={t_new:.3f}s old={t_old:.3f}s"


# --- 端到端：10 万级 token 文档 + 单字查询 -----------------------------------

@pytest.mark.slow
def test_hundred_k_token_document_end_to_end(tmp_path):
    """10 万+ token 的单页文档：入库、FTS 召回、原文验证全链路不崩。"""
    conn = db.connect(tmp_path / "stress.db")
    db.init_db(conn)
    big = " ".join(f"词{i} τ{i} latency{i} 㐂{i}" for i in range(25_000))
    fid = _add_page(conn, 1, "huge.pptx", big)
    conn.commit()
    tokens = conn.execute(
        "SELECT content FROM pages_fts WHERE file_id=?", (fid,)
    ).fetchone()["content"].split()
    assert len(tokens) > 100_000

    t0 = time.perf_counter()
    res = search.search(conn, "τ12345")
    dt = time.perf_counter() - t0
    assert [r.name for r in res] == ["huge.pptx"]
    assert dt < HUGE_DOC_BUDGET_S, f"10 万 token 文档搜索耗时 {dt:.2f}s"
    # 文档深处的稀有词也能命中（召回不被文档大小截断）
    res = search.search(conn, "latency24999")
    assert [r.name for r in res] == ["huge.pptx"]


def test_single_greek_query_recall_complete_and_precise(tmp_path):
    """单字泛化场景：150 个文件含 τ、150 个不含，召回必须完整且无误中。"""
    conn = db.connect(tmp_path / "stress.db")
    db.init_db(conn)
    for i in range(150):
        _add_page(conn, 2 * i, f"has-{i}.pptx", f"第{i}页 τ 的测量", mtime=100 + i)
        _add_page(conn, 2 * i + 1, f"none-{i}.pptx", f"第{i}页普通时延", mtime=100 + i)
    conn.commit()

    t0 = time.perf_counter()
    res = search.search(conn, "τ")
    dt = time.perf_counter() - t0
    names = {r.name for r in res}
    assert len(res) == 150, f"召回不完整：{len(res)}/150"
    assert all(n.startswith("has-") for n in names), "单字查询误中了不含 τ 的文件"
    assert dt < SINGLE_CHAR_QUERY_BUDGET_S, f"单字查询耗时 {dt:.2f}s"


def test_single_common_hanzi_query_bounded(tmp_path):
    """最高频汉字「的」单字查询：候选上限内不崩、耗时受控、无空查询退化。"""
    conn = db.connect(tmp_path / "stress.db")
    db.init_db(conn)
    for i in range(60):
        _add_page(conn, i, f"f-{i}.pptx", f"这是第{i}页的测试文本", mtime=100 + i)
    conn.commit()
    t0 = time.perf_counter()
    res = search.search(conn, "的")
    dt = time.perf_counter() - t0
    assert len(res) == 60
    assert dt < SINGLE_CHAR_QUERY_BUDGET_S


# --- 修复边界锁档 -------------------------------------------------------------

def test_ext_b_ideograph_reachable_via_letter_branch(tmp_path):
    """扩展 B（U+20000+）不在 CJK_RANGES 里，但被 [^\\W\\d_] 字母分支兜住可搜。"""
    ch = "\U00020000"  # 𠀀
    assert _base_tokens(ch) == [ch]
    conn = db.connect(tmp_path / "stress.db")
    db.init_db(conn)
    _add_page(conn, 1, "ext-b.pptx", f"生僻字 {ch} 存档")
    conn.commit()
    assert [r.name for r in search.search(conn, ch)] == ["ext-b.pptx"]


def test_non_ascii_digits_searchable_gap(tmp_path):
    """非 ASCII 十进制数字（٤٥٦）：[^\W_] 分支逐字成 token，与 compact 口径一致。"""
    conn = db.connect(tmp_path / "stress.db")
    db.init_db(conn)
    _add_page(conn, 1, "room.pptx", "会议室 ٤٥٦ 预订")
    conn.commit()
    assert [r.name for r in search.search(conn, "٤٥٦")] == ["room.pptx"]


def test_combining_marks_and_emoji_do_not_break_adjacent_recall(tmp_path):
    """组合音标与 emoji 夹在内容中间时，两侧 token 的相邻召回不受影响。"""
    conn = db.connect(tmp_path / "stress.db")
    db.init_db(conn)
    _add_page(conn, 1, "mix.pptx", "时延😀τ值é分析")
    conn.commit()
    assert [r.name for r in search.search(conn, "τ值")] == ["mix.pptx"]
