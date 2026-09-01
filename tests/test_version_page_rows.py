# -*- coding: utf-8 -*-
"""版本库的逐页文本不得按 FTS5 的 UNINDEXED 列过滤 —— 那是全表扫。

`version_pages_fts` 把 `doc_id` / `version_id` / `page_no` 都声明成 UNINDEXED，
而取页和删除又正是按这两列过滤。UNINDEXED 列没有索引，每一次都要扫全表；
`delete_doc` 还是循环调 `delete_version`，N 个版本就是 N 次全表扫。

真机实测（每操作耗时，随 FTS 总行数线性增长）：

    FTS 行数     delete_version   version_pages   delete_doc(40版)   修剪 20 版
    4,000             1.1 ms          0.8 ms           8.5 ms          9.3 ms
    75,000           24.1 ms         19.1 ms            612 ms           399 ms
    360,000         106.4 ms        108.7 ms          4,022 ms         1,965 ms

修掉之后（同一台机器、同样规模）：

    360,000          11.7 ms          0.3 ms            6.1 ms           3.7 ms

`version_pages` 尤其关键：`vault._change_summary` 在**每次保存**都调它来算「改了
几页」，差异视图更是一次调两遍——不是只有清理时才付这笔钱。

代价是写入每版多一条 SQL：30 页一版从 0.13 ms 变成 0.61 ms。同一次保存里省下的
读是 108 ms，净赚。

和 tests/test_db_write_scaling.py 一样，这里断言的是**工作量/机制**而不是墙钟——
时间在别人机器上会飘，机制不会。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pptx_finder.versioning import store


class _SqlTrace:
    def __init__(self, conn):
        self.conn = conn
        self.sql: list[str] = []

    def __enter__(self):
        self.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False

    def matching(self, *needles: str) -> list[str]:
        out = []
        for s in self.sql:
            flat = " ".join(s.lower().split())
            if all(n.lower() in flat for n in needles):
                out.append(flat)
        return out


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(tmp_path / "versions.db")
    store.init_db(c)
    yield c
    c.close()


def _add(conn, doc_id: str, version_id: str, pages: int = 5):
    store.upsert_doc(conn, doc_id, f"C:/x/{doc_id}.pptx", 1.0)
    conn.execute(
        "INSERT OR REPLACE INTO versions(version_id, doc_id, ts) VALUES(?,?,?)",
        (version_id, doc_id, 1.0))
    store.index_pages(conn, doc_id, version_id,
                      [(p, f"算力 集群 第{p}页 {version_id}") for p in range(pages)])


# ---- 侧表与 FTS 必须一一对应 ----

def test_every_indexed_page_gets_a_side_row(conn):
    _add(conn, "d1", "v1", pages=7)
    fts = conn.execute("SELECT COUNT(*) FROM version_pages_fts").fetchone()[0]
    side = conn.execute("SELECT COUNT(*) FROM version_page_rows").fetchone()[0]
    assert fts == side == 7


def test_empty_pages_are_skipped_in_both(conn):
    store.upsert_doc(conn, "d1", "C:/x/d1.pptx", 1.0)
    store.index_pages(conn, "d1", "v1", [(0, "有字"), (1, ""), (2, None or "")])
    assert conn.execute("SELECT COUNT(*) FROM version_pages_fts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM version_page_rows").fetchone()[0] == 1


# ---- 读 ----

def test_version_pages_returns_the_right_pages_in_order(conn):
    _add(conn, "d1", "v1", pages=4)
    _add(conn, "d1", "v2", pages=3)
    got = store.version_pages(conn, "v1")
    assert [r["page_no"] for r in got] == [0, 1, 2, 3]
    assert all("v1" in r["content"] for r in got)


def test_version_pages_never_filters_on_an_unindexed_column(conn):
    _add(conn, "d1", "v1", pages=3)
    with _SqlTrace(conn) as tr:
        store.version_pages(conn, "v1")
    bad = tr.matching("version_pages_fts", "where version_id=")
    assert not bad, f"又按 UNINDEXED 列过滤了（全表扫）：{bad}"


# ---- 删 ----

def test_delete_version_clears_both_tables_and_leaves_siblings(conn):
    _add(conn, "d1", "v1", pages=3)
    _add(conn, "d1", "v2", pages=4)
    store.delete_version(conn, "v1")
    assert store.version_pages(conn, "v1") == []
    assert len(store.version_pages(conn, "v2")) == 4
    assert conn.execute("SELECT COUNT(*) FROM version_pages_fts").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM version_page_rows").fetchone()[0] == 4


def test_delete_version_deletes_by_rowid_not_by_scan(conn):
    _add(conn, "d1", "v1", pages=3)
    with _SqlTrace(conn) as tr:
        store.delete_version(conn, "v1")
    assert not tr.matching("delete from version_pages_fts", "where version_id="), \
        "按 UNINDEXED 列删除 = 全表扫，正是 4 秒那条"
    assert tr.matching("delete from version_pages_fts", "where rowid=")


def test_delete_doc_removes_everything_for_that_doc_only(conn):
    _add(conn, "d1", "v1", pages=3)
    _add(conn, "d1", "v2", pages=3)
    _add(conn, "d2", "w1", pages=5)
    store.delete_doc(conn, "d1")
    assert conn.execute("SELECT COUNT(*) FROM version_pages_fts").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM version_page_rows").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM versions WHERE doc_id='d1'").fetchone()[0] == 0
    assert len(store.version_pages(conn, "w1")) == 5


def test_delete_doc_does_not_loop_per_version(conn):
    """原来是循环调 delete_version：40 个版本 = 40 次全表扫（实测 4 秒）。"""
    for i in range(12):
        _add(conn, "d1", f"v{i}", pages=3)
    with _SqlTrace(conn) as tr:
        store.delete_doc(conn, "d1")
    lookups = tr.matching("select fts_rowid from version_page_rows", "where doc_id=")
    assert len(lookups) == 1, f"整份文档只该查一次 rowid，实际 {len(lookups)} 次"


def test_search_still_finds_pages_after_the_change(conn):
    """改的是删除和取页的路径，全文检索必须原样可用。"""
    _add(conn, "d1", "v1", pages=3)
    hits = store.search_versions(conn, "算力")
    assert len(hits) == 3
    store.delete_version(conn, "v1")
    assert store.search_versions(conn, "算力") == []


# ---- 迁移：v1.5.5 之前建的库 ----

def _legacy_vault(tmp_path: Path, versions: int = 3, pages: int = 4):
    """造一个「只有 FTS、没有侧表」的老库。"""
    db = tmp_path / "legacy.db"
    c = store.connect(db)
    c.executescript(store.SCHEMA)
    for v in range(versions):
        vid = f"old-v{v}"
        c.execute("INSERT INTO versions(version_id, doc_id, ts) VALUES(?,?,?)",
                  (vid, "olddoc", float(v)))
        for p in range(pages):
            c.execute(
                "INSERT INTO version_pages_fts(content, doc_id, version_id, page_no)"
                " VALUES(?,?,?,?)", (f"旧 内容 第{p}页 {vid}", "olddoc", vid, p))
    c.execute("INSERT INTO managed_docs(doc_id, path) VALUES('olddoc','C:/x/old.pptx')")
    c.commit()
    c.close()
    return db


def test_old_vault_is_backfilled_on_open(tmp_path):
    db = _legacy_vault(tmp_path, versions=3, pages=4)
    c = store.connect(db)
    store.init_db(c)
    assert c.execute("SELECT COUNT(*) FROM version_page_rows").fetchone()[0] == 12
    got = store.version_pages(c, "old-v1")
    assert [r["page_no"] for r in got] == [0, 1, 2, 3]
    c.close()


def test_backfilled_vault_can_delete_old_versions(tmp_path):
    """迁移不到位的话，老版本的页永远删不掉——留一堆删不干净的垃圾。"""
    db = _legacy_vault(tmp_path, versions=3, pages=4)
    c = store.connect(db)
    store.init_db(c)
    store.delete_version(c, "old-v0")
    assert c.execute("SELECT COUNT(*) FROM version_pages_fts").fetchone()[0] == 8
    assert store.version_pages(c, "old-v0") == []
    c.close()


def test_backfill_runs_once_not_on_every_open(tmp_path):
    """每次开库都全量重扫会很贵；init_db 在多处被调用（构造/切库/体检）。"""
    db = _legacy_vault(tmp_path, versions=2, pages=3)
    c = store.connect(db)
    assert store._backfill_version_page_rows(c) == 6      # 首次：补了 6 行
    c.commit()
    assert store._backfill_version_page_rows(c) == 0      # 再开：一行不扫
    c.close()


def test_backfill_completes_even_when_half_migrated(tmp_path):
    """判据是 vault_meta 标记，不是「侧表空不空」。

    如果用「侧表非空就当作已迁移」，那么「新版本写了侧表、老版本还没补」的中间态
    会被误判成完成，老版本的页从此既查不到也删不掉。
    """
    db = _legacy_vault(tmp_path, versions=3, pages=4)
    c = store.connect(db)
    # 模拟半迁移：只有一版有侧表记录，且标记未落
    row = c.execute(
        "SELECT rowid FROM version_pages_fts WHERE version_id='old-v2' LIMIT 1").fetchone()
    c.execute("INSERT INTO version_page_rows(fts_rowid, version_id, doc_id, page_no)"
              " VALUES(?,?,?,?)", (row[0], "old-v2", "olddoc", 0))
    c.commit()
    store.init_db(c)
    assert c.execute("SELECT COUNT(*) FROM version_page_rows").fetchone()[0] == 12
    assert len(store.version_pages(c, "old-v0")) == 4
    c.close()


def test_a_fresh_vault_is_marked_migrated_without_scanning(tmp_path):
    c = store.connect(tmp_path / "new.db")
    store.init_db(c)
    assert store.get_meta(c, store._PAGE_ROWS_BACKFILL_KEY, "") == "1"
    c.close()
