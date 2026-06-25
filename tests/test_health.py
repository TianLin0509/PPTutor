"""库健康体检 health.py：纯计算层（重复/僵尸/诅咒/巨无霸/解析失败）+ 回收编排。

纯函数吃 index.db 的 files 表，时间相关用确定性时间戳注入；
recycle_paths 只测编排（monkeypatch 底层 _shell_recycle，绝不真送回收站）。
"""
from __future__ import annotations

import os
from datetime import datetime

from pptx_finder import db, health


def _conn(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    return conn


def _ts(y, mo, d, h=12):
    return datetime(y, mo, d, h).timestamp()


def _put(conn, name, *, path, size=1000, mtime=None, page_count=5, status="ok", content_hash=""):
    return db.upsert_file(
        conn, path=path, name=name, ext=".pptx", size=size,
        mtime=_ts(2026, 6, 1) if mtime is None else mtime,
        content_hash=content_hash, page_count=page_count,
        status=status, error="", indexed_at=0,
    )


_H1 = "sha256:" + "a" * 64
_H2 = "sha256:" + "b" * 64


# ---------- human_bytes ----------

def test_human_bytes():
    assert health.human_bytes(0) == "0 B"
    assert health.human_bytes(1536) == "1.5 KB"
    assert health.human_bytes(5 * 1024 ** 3) == "5.0 GB"


# ---------- ① 重复组 ----------

def test_find_duplicate_groups_basic(tmp_path):
    conn = _conn(tmp_path)
    _put(conn, "方案.pptx", path="/a.pptx", size=2000, mtime=_ts(2026, 6, 1), content_hash=_H1)
    _put(conn, "方案 副本.pptx", path="/b.pptx", size=2000, mtime=_ts(2026, 6, 2), content_hash=_H1)
    conn.commit()
    groups = health.find_duplicate_groups(conn)
    assert len(groups) == 1
    g = groups[0]
    assert g.copies == 2
    assert g.redundant == 1
    assert g.reclaimable == 2000
    assert g.keep_path == "/b.pptx"          # 同名无关键字 → 取更新时间
    assert g.paths[0] == "/b.pptx"           # 保留项排首位


def test_duplicate_keep_prefers_final_keyword(tmp_path):
    conn = _conn(tmp_path)
    _put(conn, "草稿.pptx", path="/d1.pptx", mtime=_ts(2026, 6, 10), content_hash=_H2)   # 更新
    _put(conn, "方案定稿.pptx", path="/d2.pptx", mtime=_ts(2026, 6, 1), content_hash=_H2)  # 更老但定稿
    conn.commit()
    groups = health.find_duplicate_groups(conn)
    assert len(groups) == 1
    assert groups[0].keep_path == "/d2.pptx"  # 定稿关键字胜过更新时间


def test_find_duplicate_groups_ignores_non_exact_and_singletons(tmp_path):
    conn = _conn(tmp_path)
    _put(conn, "a.pptx", path="/a.pptx", content_hash="md5:short")     # 非 sha256 不算
    _put(conn, "b.pptx", path="/b.pptx", content_hash="md5:short")
    _put(conn, "c.pptx", path="/c.pptx", content_hash=_H1)              # 单份不算
    conn.commit()
    assert health.find_duplicate_groups(conn) == []


# ---------- 体检总报告 ----------

def test_scan_health_empty_is_perfect(tmp_path):
    conn = _conn(tmp_path)
    rep = health.scan_health(conn)
    assert rep.deck_count == 0
    assert rep.score == 100


def test_scan_health_counts_all_ailments(tmp_path):
    conn = _conn(tmp_path)
    now = _ts(2026, 6, 26)
    _put(conn, "方案.pptx", path="/a.pptx", size=2000, mtime=_ts(2026, 6, 1), content_hash=_H1)
    _put(conn, "方案 副本.pptx", path="/b.pptx", size=2000, mtime=_ts(2026, 6, 2), content_hash=_H1)  # 重复
    _put(conn, "老古董.pptx", path="/z.pptx", size=500, mtime=_ts(2023, 1, 1))                       # 僵尸
    _put(conn, "最终版方案.pptx", path="/c.pptx", size=300, mtime=_ts(2026, 6, 1))                    # 诅咒
    _put(conn, "坏文件.pptx", path="/e.pptx", size=100, mtime=_ts(2026, 6, 1), status="encrypted")    # 解析失败
    _put(conn, "巨无霸.pptx", path="/big.pptx", size=99999, page_count=180, mtime=_ts(2026, 6, 1))    # 巨无霸/超页
    conn.commit()
    rep = health.scan_health(conn, now=now)
    assert rep.deck_count == 6
    assert rep.duplicate_groups_count == 1
    assert rep.duplicate_reclaimable == 2000
    assert rep.duplicate_redundant == 1
    assert rep.zombie_count == 1
    assert rep.curse_count == 1
    assert rep.parse_failed == 1
    assert rep.parse_failed_by_status == {"encrypted": 1}
    assert rep.bloat_biggest == ("巨无霸.pptx", 99999)
    assert rep.bloat_longest == ("巨无霸.pptx", 180)
    assert 0 <= rep.score < 100


# ---------- ② 回收编排（monkeypatch 底层，绝不真删） ----------

def test_recycle_paths_sends_to_bin(tmp_path, monkeypatch):
    f1 = tmp_path / "a.pptx"; f1.write_bytes(b"x" * 100)
    f2 = tmp_path / "b.pptx"; f2.write_bytes(b"y" * 200)
    seen = {}

    def fake(paths):
        seen["paths"] = list(paths)
        for p in paths:
            os.remove(p)        # 模拟送回收站
        return (0, False)

    monkeypatch.setattr(health, "_shell_recycle", fake)
    res = health.recycle_paths([str(f1), str(f2)])
    assert res["ok"] is True
    assert res["recycled"] == 2
    assert res["freed_bytes"] == 300
    assert not f1.exists() and not f2.exists()


def test_recycle_paths_handles_shell_error(tmp_path, monkeypatch):
    f1 = tmp_path / "a.pptx"; f1.write_bytes(b"x" * 100)

    def boom(paths):
        raise RuntimeError("no shell")

    monkeypatch.setattr(health, "_shell_recycle", boom)
    res = health.recycle_paths([str(f1)])
    assert res["ok"] is False
    assert "no shell" in res["error"]
    assert f1.exists()           # 失败不应删掉文件


def test_recycle_paths_partial(tmp_path, monkeypatch):
    f1 = tmp_path / "a.pptx"; f1.write_bytes(b"x" * 100)
    f2 = tmp_path / "b.pptx"; f2.write_bytes(b"y" * 200)

    def half(paths):
        os.remove(paths[0])      # 只删第一个
        return (0, False)

    monkeypatch.setattr(health, "_shell_recycle", half)
    res = health.recycle_paths([str(f1), str(f2)])
    assert res["ok"] is False
    assert res["recycled"] == 1
    assert res["freed_bytes"] == 100
    assert [os.path.basename(p) for p in res["failed"]] == ["b.pptx"]


def test_recycle_paths_dedupes_and_skips_missing(tmp_path, monkeypatch):
    f1 = tmp_path / "a.pptx"; f1.write_bytes(b"x" * 100)
    seen = {}

    def fake(paths):
        seen["n"] = len(paths)
        for p in paths:
            os.remove(p)
        return (0, False)

    monkeypatch.setattr(health, "_shell_recycle", fake)
    res = health.recycle_paths([str(f1), str(f1), str(tmp_path / "ghost.pptx")])
    assert seen["n"] == 1         # 去重 + 跳过不存在
    assert res["recycled"] == 1
