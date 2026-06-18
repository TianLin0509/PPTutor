"""manager 编排测试（不起 watchdog 实时监听，单测逻辑；实时监听走 E2E）。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder.parser import parse_pptx
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


def _mgr():
    return VersionManager()


def test_add_root_catches_up_existing(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "a.pptx", [{"body": "算力 集群"}])
    fx.make_pptx(docs / "b.pptx", [{"body": "周报"}])
    mgr = _mgr()
    mgr.add_root(str(docs))
    assert len(mgr.list_versions(str(docs / "a.pptx"))) == 1
    assert len(mgr.list_versions(str(docs / "b.pptx"))) == 1


def test_save_creates_version_and_skips_unchanged(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    p = docs / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = _mgr()
    mgr.add_root(str(docs))                       # 首版
    assert mgr.snapshot_now(str(p)) is None        # 没变 → 跳过
    fx.make_pptx(p, [{"body": "v2 改了"}])
    assert mgr.snapshot_now(str(p))                # 变了 → 记
    assert len(mgr.list_versions(str(p))) == 2


def test_unmanaged_path_ignored(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    outside = tmp_path / "outside.pptx"
    fx.make_pptx(outside, [{"body": "x"}])
    mgr = _mgr()
    mgr.add_root(str(docs))
    assert mgr.snapshot_now(str(outside)) is None   # 不在受管目录


def test_restore_old_keeps_current(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    p = docs / "a.pptx"
    fx.make_pptx(p, [{"body": "原始 OLDX"}])
    mgr = _mgr()
    mgr.add_root(str(docs))
    v1 = mgr.list_versions(str(p))[0]["version_id"]
    fx.make_pptx(p, [{"body": "改后 NEWX"}])
    mgr.snapshot_now(str(p))
    # 恢复 v1（覆盖原文件）→ 恢复前会先把当前(NEWX)也留一版
    assert mgr.restore_to(str(p), v1)
    txt = "".join(pg.raw_text for pg in parse_pptx(str(p)).pages)
    assert "OLDX" in txt
    # 当前已是最新版(NEWX)，恢复前留底因内容未变自动跳过 → 仍是 v1 + NEWX
    assert len(mgr.list_versions(str(p))) == 2


def test_search_history_finds_deleted_content(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    p = docs / "a.pptx"
    fx.make_pptx(p, [{"body": "含 区块链 论述"}])
    mgr = _mgr()
    mgr.add_root(str(docs))
    fx.make_pptx(p, [{"body": "换成 数据库 主题"}])
    mgr.snapshot_now(str(p))
    hits = mgr.search_history("区块链")
    assert hits and any(h["doc_id"] == vault.doc_id_for(str(p)) for h in hits)


def test_recover_deleted_file(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    p = docs / "a.pptx"
    fx.make_pptx(p, [{"body": "重要内容 KEEPME"}])
    mgr = _mgr()
    mgr.add_root(str(docs))
    did = vault.doc_id_for(str(p))
    p.unlink()                       # 误删原文件
    assert mgr.scan_deleted() == 1   # 标记 deleted
    assert store.get_doc(mgr._conn, did)["status"] == "deleted"
    assert mgr.recover(did)          # 从版本库重建回原路径
    assert p.exists()
    txt = "".join(pg.raw_text for pg in parse_pptx(str(p)).pages)
    assert "KEEPME" in txt
    assert store.get_doc(mgr._conn, did)["status"] == "active"  # 恢复后回 active
