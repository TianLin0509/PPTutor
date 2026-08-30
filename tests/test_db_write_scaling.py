# -*- coding: utf-8 -*-
"""首次建库不得发无谓的 FTS DELETE —— 那一句会让写入塌成 O(n²)。

file_id 是 FTS5 的 UNINDEXED 列，`DELETE ... WHERE file_id=?` 每次都是全表扫。
盘点那条路（indexer._write_filename_only_batch）当年为此专门改过批量写法；内容类
文件走的 db.upsert_file / db.replace_pages 则一直逐行发 DELETE，只是库里只有一两千
份 PPT 时看不出来。2026-08-29 实测（本机，真实代码路径）：

    只写文件行     连写 24,000 行   4,805 → 392 行/秒（末/首 0.08）  30.4s
    一份 PPT 全量   连写  6,000 份     406 →  29 份/秒（末/首 0.07）  111.7s

修掉之后分别是 1.9s / 5.5s，曲线拉平（0.77 / 0.99）。

这里不断言墙钟时间（跑在别人机器上会飘），而是直接断言「新插入的那一趟一句
DELETE 都不发」——这才是不变量本身，时间只是它的后果。
"""
from __future__ import annotations

import pytest

from pptx_finder import db
from pptx_finder.text_tokenize import tokenize


class _SqlTrace:
    """收集这条连接上执行过的 SQL，用来断言某类语句压根没发出去。"""

    def __init__(self, conn):
        self.conn = conn
        self.sql: list[str] = []

    def __enter__(self):
        self.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False

    def count(self, needle: str) -> int:
        low = needle.lower()
        return sum(1 for s in self.sql if low in " ".join(s.lower().split()))


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "idx.db")
    db.init_db(c)
    yield c
    c.close()


def _add(c, i, *, pages=0):
    fid = db.upsert_file(
        c, path=f"D:\\x\\{i}\\汇报_{i}.pptx", name=f"汇报_{i}.pptx", ext=".pptx",
        size=1024, mtime=1.0, content_hash=f"h{i}", page_count=pages,
        status="ok", error="", indexed_at=1.0)
    if pages:
        db.replace_pages(c, fid, [(k, f"第{k}页 算力集群", f"第{k}页 算力集群")
                                  for k in range(1, pages + 1)])
    return fid


def test_first_time_insert_issues_no_filename_fts_delete(conn):
    with _SqlTrace(conn) as tr:
        for i in range(50):
            _add(conn, i)
    assert tr.count("delete from file_names_fts") == 0
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == 50


def test_first_time_insert_issues_no_pages_fts_delete(conn):
    with _SqlTrace(conn) as tr:
        for i in range(30):
            _add(conn, i, pages=3)
    assert tr.count("delete from pages_fts") == 0
    assert tr.count("delete from pages_raw") == 0
    assert conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0] == 90


def test_reindexing_the_same_file_still_purges_the_old_rows(conn):
    """跳过 DELETE 只能发生在新插入那一趟；重扫同一个文件必须照旧清干净。"""
    _add(conn, 1, pages=4)
    with _SqlTrace(conn) as tr:
        _add(conn, 1, pages=2)
    assert tr.count("delete from file_names_fts") == 1
    assert tr.count("delete from pages_fts") == 1
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM pages_raw").fetchone()[0] == 2


def test_rename_does_not_leave_a_stale_filename_fts_row(conn):
    fid = _add(conn, 7)
    db.upsert_file(
        conn, path="D:\\x\\7\\汇报_7.pptx", name="改名后的稿子.pptx", ext=".pptx",
        size=1024, mtime=2.0, content_hash="h7b", page_count=0,
        status="ok", error="", indexed_at=2.0)
    rows = conn.execute(
        "SELECT content FROM file_names_fts WHERE file_id=?", (fid,)).fetchall()
    assert len(rows) == 1
    # 中文按字切，比对切过词的形式
    assert tokenize("改名后的稿子.pptx") == rows[0][0]
    # 旧名字不能还留在文件名 FTS 里，否则搜旧名还能搜到这份稿子
    assert "汇" not in rows[0][0]


def test_name_norm_still_written_by_upsert(conn):
    """_update_filename_index 里那句冗余的 UPDATE 被拿掉了，name_norm 必须仍然对。"""
    _add(conn, 3)
    got = conn.execute(
        "SELECT name_norm FROM files WHERE path='D:\\x\\3\\汇报_3.pptx'").fetchone()[0]
    assert got and got.startswith("汇报")


def test_pages_stay_consistent_when_a_file_loses_all_pages(conn):
    fid = _add(conn, 9, pages=5)
    db.replace_pages(conn, fid, [])
    assert conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pages_raw").fetchone()[0] == 0


def test_ids_are_never_reused_so_a_new_id_cannot_have_stale_fts(conn):
    """跳过 DELETE 的前提：AUTOINCREMENT 不回收 id。这条塌了整个优化就不成立。"""
    fid = _add(conn, 11, pages=2)
    db.delete_file(conn, "D:\\x\\11\\汇报_11.pptx")
    new_fid = _add(conn, 12, pages=2)
    assert new_fid > fid
    assert conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == 1
