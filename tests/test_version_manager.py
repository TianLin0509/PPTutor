"""manager 编排测试（不起 watchdog 实时监听，单测逻辑；实时监听走 E2E）。

新架构：谁变管谁——任何 .pptx 保存即 snapshot_now，第一次见到该文件就建 v1。
"""
from __future__ import annotations

import os
import time

import fixtures_gen as fx
import pytest

from pptx_finder.parser import parse_pptx
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture(autouse=True)
def _isolated_version_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


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


def test_reconcile_known_docs_catches_missed_save(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = VersionManager(store.connect(tmp_path / "reconcile-vault.db"))
    assert mgr.snapshot_now(str(p))

    fx.make_pptx(p, [{"body": "v2 watcher missed"}])
    future = time.time() + 3
    os.utime(p, (future, future))

    assert mgr.reconcile_known_docs(scan_new_files=False) == 1
    assert len(mgr.list_versions(str(p))) == 2


def test_reconcile_known_docs_skips_unchanged_file(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = VersionManager(store.connect(tmp_path / "reconcile-skip-vault.db"))
    assert mgr.snapshot_now(str(p))

    assert mgr.reconcile_known_docs(scan_new_files=False) == 0
    assert len(mgr.list_versions(str(p))) == 1


def test_reconcile_known_docs_catches_new_file_in_managed_directory(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    p1 = docs / "a.pptx"
    p2 = docs / "new-copy-missed-by-watcher.pptx"
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_COMMON_DIRS", "0")
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_DIRS", str(docs))
    fx.make_pptx(p1, [{"body": "v1"}])
    mgr = VersionManager(store.connect(tmp_path / "reconcile-new-vault.db"))
    assert mgr.snapshot_now(str(p1))
    fx.make_pptx(p2, [{"body": "new file watcher missed"}])

    assert mgr.reconcile_known_docs() == 1
    assert len(mgr.list_versions(str(p2))) == 1
    assert "last_new_checked=1" in "\n".join(mgr.diagnostic_lines())


def test_reconcile_known_docs_can_disable_new_file_scan(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    p1 = docs / "a.pptx"
    p2 = docs / "new-copy-missed-by-watcher.pptx"
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_COMMON_DIRS", "0")
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_DIRS", str(docs))
    fx.make_pptx(p1, [{"body": "v1"}])
    mgr = VersionManager(store.connect(tmp_path / "reconcile-no-new-vault.db"))
    assert mgr.snapshot_now(str(p1))
    fx.make_pptx(p2, [{"body": "new file watcher missed"}])

    assert mgr.reconcile_known_docs(scan_new_files=False) == 0
    assert mgr.list_versions(str(p2)) == []


def test_save_creates_version_and_skips_unchanged(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = _mgr()
    assert mgr.snapshot_now(str(p))             # 首版
    assert mgr.snapshot_now(str(p)) is None      # 没变 → 跳过
    fx.make_pptx(p, [{"body": "v2 改了"}])
    assert mgr.snapshot_now(str(p))              # 变了 → 记
    assert len(mgr.list_versions(str(p))) == 2


def test_summary_stats_separates_protected_versions_and_rollback_docs(tmp_path):
    p1 = tmp_path / "one-version.pptx"
    p2 = tmp_path / "rollback-ready.pptx"
    fx.make_pptx(p1, [{"body": "only v1"}])
    fx.make_pptx(p2, [{"body": "v1"}])
    mgr = _mgr()
    assert mgr.snapshot_now(str(p1))
    assert mgr.snapshot_now(str(p2))
    fx.make_pptx(p2, [{"body": "v2 changed"}])
    assert mgr.snapshot_now(str(p2))

    stats = mgr.summary_stats()

    assert stats["protected_docs"] == 2
    assert stats["total_versions"] == 3
    assert stats["rollback_docs"] == 1
    assert stats["single_version_docs"] == 1


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


def test_search_history_details_uses_fresh_connection_not_ui_read_conn(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "含 区块链 论述"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))

    class BrokenReadConn:
        def execute(self, *args, **kwargs):
            raise AssertionError("background history details must not use UI read connection")

    mgr._read_conn = BrokenReadConn()

    result = mgr.search_history_details("区块链")

    assert result["total"] >= 1
    assert result["rows"]
    assert result["rows"][0]["doc_path"].endswith("a.pptx")


def test_list_versions_by_doc_details_uses_fresh_connection_not_ui_read_conn(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))
    did = vault.doc_id_for(str(p))

    class BrokenReadConn:
        def execute(self, *args, **kwargs):
            raise AssertionError("background version list must not use UI read connection")

    mgr._read_conn = BrokenReadConn()

    rows = mgr.list_versions_by_doc_details(did)

    assert rows
    assert rows[0]["version_id"]
    assert rows[0]["page_count"] == 1


def test_list_versions_details_uses_fresh_connection_not_ui_read_conn(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))

    class BrokenReadConn:
        def execute(self, *args, **kwargs):
            raise AssertionError("background file version detail must not use UI read connection")

    mgr._read_conn = BrokenReadConn()

    rows = mgr.list_versions_details(str(p))

    assert rows
    assert rows[0]["version_id"]
    assert rows[0]["page_count"] == 1


def test_list_versions_details_includes_summary_and_preview_fields(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = VersionManager(store.connect(tmp_path / "summary-vault.db"))
    assert mgr.snapshot_now(str(p))
    fx.make_pptx(p, [{"body": "v2 changed"}])
    assert mgr.snapshot_now(str(p))

    rows = mgr.list_versions_details(str(p))

    assert len(rows) == 2
    assert rows[0]["changed"]
    assert "thumb_path" in rows[0]


def test_describe_version_diff_reports_text_pages_and_media(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "alpha beta"}, {"body": "stable"}])
    mgr = VersionManager(store.connect(tmp_path / "diff-vault.db"))
    assert mgr.snapshot_now(str(p))
    fx.make_pptx(p, [{"body": "alpha gamma"}, {"body": "stable"}, {"body": "new page"}])
    assert mgr.snapshot_now(str(p))
    latest = mgr.list_versions(str(p))[0]["version_id"]

    diff = mgr.describe_version_diff(latest)

    assert diff["ok"] is True
    assert diff["previous_version_id"]
    assert diff["page_count"] == 3
    assert diff["previous_page_count"] == 2
    assert diff["text"]["changed_pages"] == [1]
    assert diff["text"]["added_pages"] == [3]
    assert "gamma" in diff["text"]["added_terms"]
    assert "beta" in diff["text"]["removed_terms"]
    assert diff["package"]["changed_parts"] >= 1
    assert any("文本改动" in line or "新增" in line for line in diff["lines"])


def test_version_store_init_migrates_preview_columns(tmp_path):
    conn = store.connect(tmp_path / "old-vault.db")
    conn.execute(
        """CREATE TABLE versions(
           version_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ts REAL DEFAULT 0,
           session_id TEXT DEFAULT '', page_count INTEGER DEFAULT 0, size INTEGER DEFAULT 0,
           content_hash TEXT DEFAULT ''
        )"""
    )

    store.init_db(conn)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(versions)").fetchall()}
    assert {"changed", "thumb_path"} <= cols


def test_ensure_version_preview_renders_and_caches(tmp_path, monkeypatch):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = VersionManager(store.connect(tmp_path / "preview-vault.db"))
    assert mgr.snapshot_now(str(p))
    version_id = mgr.list_versions(str(p))[0]["version_id"]
    fake_png = tmp_path / "fake-preview.png"
    calls = []
    closed = []

    def fake_render(path, page_no, cache_key=None, long_edge=0, hi_priority=False, priority=None):
        calls.append((path, page_no, cache_key, long_edge, hi_priority))
        fake_png.write_bytes(b"png")
        return fake_png

    monkeypatch.setattr("pptx_finder.versioning.manager.renderer.render_page", fake_render)
    monkeypatch.setattr("pptx_finder.versioning.manager.renderer.close_current_presentation", lambda: closed.append(True))

    first = mgr.ensure_version_preview(version_id)
    second = mgr.ensure_version_preview(version_id)
    row = store.get_version(mgr._conn, version_id)

    assert first == str(fake_png)
    assert second == str(fake_png)
    assert row["thumb_path"] == str(fake_png)
    assert len(calls) == 1
    assert calls[0][1] == 1
    assert calls[0][3] == 360
    assert closed == [True]


def test_list_docs_details_uses_fresh_connection_not_ui_read_conn(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))

    class BrokenReadConn:
        def execute(self, *args, **kwargs):
            raise AssertionError("background doc list must not use UI read connection")

    mgr._read_conn = BrokenReadConn()

    rows = mgr.list_docs_details()

    assert rows
    assert rows[0]["path"].endswith("a.pptx")


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
