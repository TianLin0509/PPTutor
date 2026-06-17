"""集成：版本归组后，search 结果应带 group_id、标最新版、同组聚集相邻。"""
from __future__ import annotations

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
