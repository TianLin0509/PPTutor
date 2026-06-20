"""搜索边缘场景回归——Codex 压测发现的召回/精度/UI 状态 bug 修复后锁定。

覆盖：文件名繁简&全半角归一化(B01) / 热门词召回不截断(B02) / 弯引号短语(B04) /
LIKE 通配符字面化(B05) / facet 筛空的 UI 空状态(B06)。
"""
from __future__ import annotations

import fixtures_gen as fx
from test_ui import StubRender

from pptx_finder import db, indexer, search
from pptx_finder.ui.main_window import MainWindow


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


# B01 文件名全半角归一化：半角 query 命中全角文件名
def test_filename_fullwidth_normalized(tmp_path):
    conn = _build(tmp_path, {"ＡＩ－２０２６.pptx": ["占位"]})
    assert "ＡＩ－２０２６.pptx" in _names(search.search(conn, "AI-2026"))


# B01 文件名繁简归一化：简体 query 命中繁体文件名（開發→开发）
def test_filename_traditional_normalized(tmp_path):
    conn = _build(tmp_path, {"軟體開發.pptx": ["占位"]})
    assert "軟體開發.pptx" in _names(search.search(conn, "开发"))


# B05 LIKE 通配符当字面：% _ 不再误命中全部文件
def test_filename_wildcards_literal(tmp_path):
    conn = _build(tmp_path, {"alpha.pptx": ["甲"], "beta.pptx": ["乙"]})
    assert _names(search.search(conn, "%")) == []
    assert _names(search.search(conn, "_")) == []
    assert _names(search.search(conn, "a_")) == []


# B02 热门词召回不截断：850 文件都含公共词、仅末个含稀有词，多词 AND 必须命中
def test_common_plus_rare_no_recall_cap(tmp_path):
    files = {
        f"f{i:04d}.pptx": ["公共词通用内容" + (" 稀有词罕见标记" if i == 849 else "")]
        for i in range(850)
    }
    conn = _build(tmp_path, files)
    assert "f0849.pptx" in _names(search.search(conn, "公共词通用内容 稀有词罕见标记"))


# B04 中文弯引号/书名号短语：各种引号包裹都能命中
def test_fancy_quote_phrase(tmp_path):
    conn = _build(tmp_path, {"p.pptx": ["我们建设算力集群方案"]})
    for q in ['"算力集群"', "“算力集群”", "「算力集群」", "《算力集群》"]:
        assert "p.pptx" in _names(search.search(conn, q)), q


# B03 英文/数字连写子串：≥3 字符片段经 trigram 召回 + 原文验证命中
def test_alnum_substring_trigram(tmp_path):
    conn = _build(tmp_path, {"m.pptx": ["发布 GPT4Turbo 与 FY2026Q1 规格"]})
    for q in ["GPT4", "Turbo", "2026", "FY2026"]:
        assert "m.pptx" in _names(search.search(conn, q)), q


# B03 精度：gpt 与 4 被分开 → 搜 gpt4 不该误中（trigram 召回但原文验证拦下）
def test_alnum_substring_precision(tmp_path):
    conn = _build(tmp_path, {"sep.pptx": ["gpt version 4 方案"]})
    assert "sep.pptx" not in _names(search.search(conn, "gpt4"))


# B06 facet 筛到 0：计数不留陈旧值 + 显示空状态提示
def test_facet_zero_empty_state(qtbot, tmp_path):
    conn = _build(tmp_path, {"甲.pptx": ["通用词内容"], "乙.pptx": ["通用词内容"]})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    win.search_box.setText("通用词内容")
    win._do_search()
    assert win.result_list.count() == 2
    win._apply_facet({"folder": {"不存在的文件夹XYZ"}})  # 筛到 0
    assert win.result_list.count() == 0
    assert "命中 2" not in win.result_count.text()   # 不留「命中 2 个」陈旧计数
    assert not win.empty_hint.isHidden()             # 有空状态提示
