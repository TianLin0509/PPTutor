"""manager 编排测试（不起 watchdog 实时监听，单测逻辑；实时监听走 E2E）。

新架构：谁变管谁——任何 .pptx 保存即 snapshot_now，第一次见到该文件就建 v1。
"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder.parser import parse_pptx
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


def _mgr():
    return VersionManager()


def test_snapshot_any_pptx_builds_v1(tmp_path):
    """谁变管谁：任何 .pptx 保存都建版本，无需预先登记目录（第一次=v1）。"""
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "算力 集群"}])
    mgr = _mgr()
    assert mgr.snapshot_now(str(p))            # 第一次见 → v1
    assert len(mgr.list_versions(str(p))) == 1
    assert mgr.snapshot_now(str(p)) is None     # 没变 → 跳过


def test_catch_up_root_builds_versions(tmp_path):
    """手动补录：把目录现存 .pptx 各建一版（测试 / 补录用，生产靠 watcher 不自动调）。"""
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "a.pptx", [{"body": "算力"}])
    fx.make_pptx(docs / "b.pptx", [{"body": "周报"}])
    mgr = _mgr()
    assert mgr.catch_up_root(str(docs)) == 2
    assert len(mgr.list_versions(str(docs / "a.pptx"))) == 1
    assert len(mgr.list_versions(str(docs / "b.pptx"))) == 1


def test_save_creates_version_and_skips_unchanged(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = _mgr()
    assert mgr.snapshot_now(str(p))             # 首版
    assert mgr.snapshot_now(str(p)) is None      # 没变 → 跳过
    fx.make_pptx(p, [{"body": "v2 改了"}])
    assert mgr.snapshot_now(str(p))              # 变了 → 记
    assert len(mgr.list_versions(str(p))) == 2


def test_restore_old_keeps_current(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "原始 OLDX"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))
    v1 = mgr.list_versions(str(p))[0]["version_id"]
    fx.make_pptx(p, [{"body": "改后 NEWX"}])
    mgr.snapshot_now(str(p))
    # 恢复 v1（覆盖原文件）→ 恢复前会先把当前(NEWX)也留一版（已是最新则跳过）
    assert mgr.restore_to(str(p), v1)
    txt = "".join(pg.raw_text for pg in parse_pptx(str(p)).pages)
    assert "OLDX" in txt
    assert len(mgr.list_versions(str(p))) == 2


def test_search_history_finds_deleted_content(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "含 区块链 论述"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))
    fx.make_pptx(p, [{"body": "换成 数据库 主题"}])
    mgr.snapshot_now(str(p))
    hits = mgr.search_history("区块链")
    assert hits and any(h["doc_id"] == vault.doc_id_for(str(p)) for h in hits)


def test_recover_deleted_file(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "重要内容 KEEPME"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))
    did = vault.doc_id_for(str(p))
    p.unlink()                       # 误删原文件
    assert mgr.scan_deleted() == 1   # 标记 deleted
    assert store.get_doc(mgr._conn, did)["status"] == "deleted"
    assert mgr.recover(did)          # 从版本库重建回原路径
    assert p.exists()
    txt = "".join(pg.raw_text for pg in parse_pptx(str(p)).pages)
    assert "KEEPME" in txt
    assert store.get_doc(mgr._conn, did)["status"] == "active"  # 恢复后回 active
