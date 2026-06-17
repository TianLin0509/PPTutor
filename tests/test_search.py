"""search 单元测试：AND / 短语 / 文件名命中 / 排序 / 片段高亮。"""
from __future__ import annotations

import os

import fixtures_gen as fx

from pptx_finder import db, indexer, search


def _build(tmp_path, files: dict[str, list[str]]):
    """files: {filename: [page bodies]}。返回已建索引的 conn。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    for fn, bodies in files.items():
        fx.make_pptx(docs / fn, [{"body": b} for b in bodies])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn, docs


def test_single_term_locates_page(tmp_path):
    conn, _ = _build(tmp_path, {"a.pptx": ["第一页无关", "第二页有 昇腾 关键词"]})
    res = search.search(conn, "昇腾")
    assert len(res) == 1
    assert res[0].hits[0].page_no == 2  # 定位到第 2 页
    assert "【昇腾】" in res[0].hits[0].snippet


def test_multi_term_is_and(tmp_path):
    conn, _ = _build(tmp_path, {
        "both.pptx": ["算力 昇腾 同页出现"],
        "only.pptx": ["只有算力没有另一个词"],
    })
    res = search.search(conn, "算力 昇腾")
    names = {r.name for r in res}
    assert "both.pptx" in names
    assert "only.pptx" not in names  # AND：缺一词不命中


def test_phrase_requires_adjacency(tmp_path):
    conn, _ = _build(tmp_path, {
        "adj.pptx": ["算力集群部署"],
        "apart.pptx": ["算力方案，独立的集群在别处"],
    })
    res = search.search(conn, '"算力集群"')
    names = {r.name for r in res}
    assert "adj.pptx" in names
    assert "apart.pptx" not in names


def test_filename_hit(tmp_path):
    conn, _ = _build(tmp_path, {"预算汇报2026.pptx": ["内容无关词"]})
    res = search.search(conn, "预算")
    assert len(res) == 1
    assert res[0].name_hit is True


def test_ranking_recency(tmp_path):
    """内容相同时，修改时间更新的排前。"""
    conn, docs = _build(tmp_path, {
        "old.pptx": ["完全相同的内容文本"],
        "new.pptx": ["完全相同的内容文本"],
    })
    old = docs / "old.pptx"
    new = docs / "new.pptx"
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    # 重新索引以刷新 mtime
    indexer.update_index(conn, [str(docs)], workers=1)
    res = search.search(conn, "完全相同的内容文本")
    assert [r.name for r in res][0] == "new.pptx"


def test_empty_query(tmp_path):
    conn, _ = _build(tmp_path, {"a.pptx": ["x"]})
    assert search.search(conn, "   ") == []
