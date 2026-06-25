"""集成：版本归组后，search 结果应带 group_id、标最新版、同组聚集相邻。"""
from __future__ import annotations

import shutil
import zipfile

import fixtures_gen as fx

from pptx_finder import cluster, db, indexer, search


def test_version_grouping_in_results(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    base = [
        "封面 项目方案 演示",
        "背景 详细论证 第一第二第三第四第五点 充分说明 abcdefghij",
        "方案 实施 步骤 预算 时间 人力 三方面 甲乙丙丁戊己庚辛",
    ]
    fx.make_pptx(docs / "方案_v1.pptx", [{"body": b} for b in base])
    fx.make_pptx(docs / "方案_终稿.pptx",
                 [{"body": b + (" 终稿补充" if i == 2 else "")} for i, b in enumerate(base)])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    cluster.compute_groups(conn)

    res = search.search(conn, "方案")
    assert len(res) == 2
    # 两个版本归同一组
    assert res[0].group_id is not None
    assert res[0].group_id == res[1].group_id
    # 恰一个被标为最新版，且为文件名含“终稿”者
    latest = [r for r in res if r.is_latest]
    assert len(latest) == 1
    assert "终稿" in latest[0].name


def test_exact_duplicate_copies_are_collapsed_in_search_results(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    src = docs / "AI方案.pptx"
    copy = docs / "备份" / "AI方案-copy.pptx"
    copy.parent.mkdir()
    fx.make_pptx(src, [{"body": "AI 精准搜索 副本聚合"}])
    shutil.copyfile(src, copy)

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)

    res = search.search(conn, "副本聚合")

    assert len(res) == 1
    assert set(res[0].duplicate_paths) == {str(src), str(copy)}
    assert res[0].content_hash.startswith("sha256:")


def test_same_text_but_different_pptx_bytes_are_not_collapsed(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    a = docs / "AI方案A.pptx"
    b = docs / "AI方案B.pptx"
    fx.make_pptx(a, [{"body": "AI 精准搜索 相同文本"}])
    fx.make_pptx(b, [{"body": "AI 精准搜索 相同文本"}])
    with zipfile.ZipFile(b, "a") as zf:
        zf.comment = b"different bytes"

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)

    res = search.search(conn, "相同文本")

    assert len(res) == 2
    assert all(not r.duplicate_paths for r in res)
