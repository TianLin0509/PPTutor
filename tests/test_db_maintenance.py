# -*- coding: utf-8 -*-
"""索引库维护：空闲页回收与 WAL 截断。

背景：这两条原本都不会在真实机器上触发。
  · VACUUM 的两个门槛是「与」的关系，于是一个 132 MB 的库无论怎么膨胀都够不到
    「空闲 256 MB」——真机实测 86%（114 MB）是空闲页，白占着磁盘。
  · WAL 只有 TRUNCATE 才把文件还给磁盘，而 TRUNCATE 原来只在 VACUUM 之后跑，
    于是 WAL 一直长着（真机 32 MB）。
"""
from __future__ import annotations

import os
import sqlite3


from pptx_finder import db


def _bloated(path, rows=4000, payload=4096):
    """造一个「删掉大半、空闲页占比很高」的库，模拟索引格式迁移之后的状态。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE blob_rows(id INTEGER PRIMARY KEY, body BLOB)")
    conn.executemany("INSERT INTO blob_rows(body) VALUES(?)",
                     [(os.urandom(payload),) for _ in range(rows)])
    conn.commit()
    conn.execute("DELETE FROM blob_rows WHERE id % 10 != 0")
    conn.commit()
    return conn


def test_vacuum_runs_on_a_high_ratio_freelist_even_when_small(tmp_path):
    """占比达标就该整理，不必再等绝对量——绝对量那条在中小库上永远够不到。"""
    path = tmp_path / "idx.db"
    conn = _bloated(path)
    free_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert free_before > 0
    result = db.maintain(conn, min_free_bytes=1 << 40,      # 绝对量故意设到不可能
                         min_free_ratio=0.25, floor_bytes=1024)
    conn.close()
    assert result["vacuumed"] is True
    assert result["free_pages_after"] == 0
    assert result["page_count_after"] < result["page_count_before"]


def test_vacuum_skipped_for_a_trivially_small_freelist(tmp_path):
    """刚建好的小库不值得为了几百 KB 去 VACUUM。"""
    path = tmp_path / "idx.db"
    conn = _bloated(path, rows=40, payload=256)
    result = db.maintain(conn, min_free_bytes=1 << 40,
                         min_free_ratio=0.25, floor_bytes=32 * 1024 * 1024)
    conn.close()
    assert result["vacuumed"] is False


def test_vacuum_runs_on_a_large_absolute_freelist(tmp_path):
    """大库上占比可能很低，但绝对量够大也该整理。"""
    path = tmp_path / "idx.db"
    conn = _bloated(path)
    result = db.maintain(conn, min_free_bytes=1024, min_free_ratio=1.5)
    conn.close()
    assert result["vacuumed"] is True


def test_wal_is_truncated_once_it_grows(tmp_path):
    """WAL 只有 TRUNCATE 才把文件还给磁盘；PASSIVE 只把内容搬进主库。"""
    path = tmp_path / "idx.db"
    conn = _bloated(path)
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    conn.executemany("INSERT INTO blob_rows(body) VALUES(?)",
                     [(os.urandom(4096),) for _ in range(400)])
    conn.commit()
    wal = str(path) + "-wal"
    assert os.path.getsize(wal) > 0
    result = db.maintain(conn, min_free_bytes=1 << 40, min_free_ratio=2.0,
                         floor_bytes=1 << 40, wal_truncate_bytes=1024)
    # 必须在关连接之前量：最后一个连接干净关闭时 SQLite 会直接删掉 -wal 文件，
    # 关完再量就分不清「截断生效」和「文件本来就没了」。
    after = os.path.getsize(wal)
    conn.close()
    assert result["vacuumed"] is False          # 这条用例只验 WAL，不该顺带 VACUUM
    assert result["wal_truncated"] is True
    assert result["wal_bytes_after"] < result["wal_bytes_before"]
    assert after == 0


def test_small_wal_is_left_alone(tmp_path):
    """WAL 还小的时候不折腾——TRUNCATE 要等所有读者退出，不该为几 KB 去抢。"""
    path = tmp_path / "idx.db"
    conn = _bloated(path, rows=40, payload=256)
    result = db.maintain(conn, min_free_bytes=1 << 40, min_free_ratio=2.0,
                         floor_bytes=1 << 40, wal_truncate_bytes=64 * 1024 * 1024)
    conn.close()
    assert result["wal_truncated"] is False


def test_maintain_reports_wal_size_without_crashing_on_memory_db():
    """内存库没有 -wal 文件，取大小不能炸。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    result = db.maintain(conn)
    conn.close()
    assert result["error"] == ""
    assert result["wal_bytes_before"] == 0
