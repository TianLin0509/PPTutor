"""cluster 单元测试：多版本归为一组，无关文件不归组。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder import cluster, db, indexer


def _names_to_ids(conn):
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}


def test_versions_grouped(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    base = [
        "封面 Q3 算力方案 演示文稿",
        "背景 昇腾 910B 集群 部署 需求 详细论证 第一第二第三第四第五点 充分说明",
        "方案 扩容 算力 集群 至 目标 规模 预算 人力 时间 三方面 甲乙丙丁戊己庚",
    ]
    fx.make_pptx(docs / "v1.pptx", [{"body": b} for b in base])
    v2 = list(base)
    v2[2] = base[2] + " 本版略有修改补充"
    fx.make_pptx(docs / "v2.pptx", [{"body": b} for b in v2])
    v3 = list(base)
    v3[1] = base[1] + " 终稿定稿"
    fx.make_pptx(docs / "终稿.pptx", [{"body": b} for b in v3])
    fx.make_pptx(docs / "unrelated.pptx", [
        {"body": "完全不同的主题 财务报销 流程 单据 审批 制度 规范 说明 一二三四"}
    ])

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    groups = cluster.compute_groups(conn)

    ids = _names_to_ids(conn)
    g1 = groups.get(ids["v1.pptx"])
    g2 = groups.get(ids["v2.pptx"])
    g3 = groups.get(ids["终稿.pptx"])
    assert g1 is not None and g1 == g2 == g3, "三个版本应归为同一组"
    assert ids["unrelated.pptx"] not in groups, "无关文件不应入组"


def test_distinct_not_grouped(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "a.pptx", [{"body": "主题甲 内容完全独立 苹果 香蕉 橙子 一二三四五"}])
    fx.make_pptx(docs / "b.pptx", [{"body": "主题乙 另一回事 飞机 火车 轮船 六七八九十"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    assert cluster.compute_groups(conn) == {}, "无相似对时不产生任何组"
