"""版本库存储位置选项 + 幽灵文档收割 + 暂存随库（2026-07-30）。

背景：用户反馈版本管理开启后 C 盘 vault 目录文件暴涨；新增自选存储位置、
暂存随库、幽灵收割（生产实测幽灵文档 48%、约 1.46 GiB 可回收）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import fixtures_gen as fx
from pptx_finder import config
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def test_config_vault_dir_roundtrip():
    assert config.get_version_vault_dir() == ""
    config.set_version_vault_dir("D:\\vault-x")
    assert config.get_version_vault_dir() == "D:\\vault-x"
    config.set_version_vault_dir("")
    assert config.get_version_vault_dir() == ""


def test_validate_version_vault_dir(tmp_path):
    assert config.validate_version_vault_dir("") is not None
    assert config.validate_version_vault_dir("C:\\") is not None
    assert config.validate_version_vault_dir("C:\\Windows\\x") is not None
    assert config.validate_version_vault_dir(str(tmp_path / "ok-dir")) is None
    current = Path(config.data_dir()) / "vault"
    assert config.validate_version_vault_dir(str(current / "nested")) is not None
    assert config.validate_version_vault_dir(str(current.parent)) is not None


def test_vault_dir_honors_override(tmp_path):
    custom = tmp_path / "custom-vault"
    config.set_version_vault_dir(str(custom))
    try:
        assert vault.vault_dir() == custom
        assert vault.db_path() == custom / "versions.db"
    finally:
        config.set_version_vault_dir("")


def test_snapshot_temp_staged_in_vault_tmp(tmp_path):
    custom = tmp_path / "custom-vault"
    config.set_version_vault_dir(str(custom))
    try:
        src = tmp_path / "a.pptx"
        fx.make_pptx(src, [{"body": "暂存随库"}])
        with vault.stable_snapshot_source(str(src)) as snap:
            assert str(custom / "_tmp") in str(snap)
            assert os.path.exists(snap)
        assert not os.path.exists(snap)  # 正常退出即清理
    finally:
        config.set_version_vault_dir("")


def test_sweep_covers_vault_tmp(tmp_path):
    custom = tmp_path / "custom-vault"
    config.set_version_vault_dir(str(custom))
    try:
        tmp_dir = vault.vault_dir() / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stale = tmp_dir / ".pptdoctor-snapshot-dead00.pptx"
        stale.write_bytes(b"x")
        assert vault.sweep_stale_snapshot_temps() >= 1
        assert not stale.exists()
    finally:
        config.set_version_vault_dir("")


def _init_conn():
    conn = store.connect(vault.db_path())
    store.init_db(conn)
    return conn


def _add_full_version(conn, doc_id: str, ts: float = 1.0) -> None:
    vid = f"v001-{doc_id}"
    d = vault.vault_dir() / doc_id / "versions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vid}.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
    (d / f"{vid}.pptx").write_bytes(b"full-copy")
    conn.execute(
        "INSERT INTO versions(version_id, doc_id, ts, content_hash) VALUES(?,?,?,?)",
        (vid, doc_id, ts, "h-" + doc_id),
    )
    conn.commit()


def test_reap_ghost_docs(tmp_path):
    live_src = tmp_path / "live.pptx"
    fx.make_pptx(live_src, [{"body": "活着的"}])
    ghost_src = tmp_path / "gone.pptx"
    fx.make_pptx(ghost_src, [{"body": "已删除"}])

    conn = _init_conn()
    store.upsert_doc(conn, "doc-live", str(live_src), 1.0)
    _add_full_version(conn, "doc-live")
    store.upsert_doc(conn, "doc-ghost", str(ghost_src), 1.0)
    _add_full_version(conn, "doc-ghost")
    ghost_src.unlink()

    dry = vault.reap_ghost_docs(conn, dry_run=True)
    assert dry["ghost_docs"] == 1 and dry["ghost_versions"] == 1

    res = vault.reap_ghost_docs(conn, dry_run=False)
    assert res["ghost_docs"] == 1
    assert not (vault.vault_dir() / "doc-ghost").exists()
    assert conn.execute("SELECT COUNT(*) FROM managed_docs WHERE doc_id='doc-ghost'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM managed_docs WHERE doc_id='doc-live'").fetchone()[0] == 1
    conn.close()


def test_migrate_vault_dir(tmp_path):
    src = tmp_path / "src-vault"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.bin").write_bytes(b"aaa")
    (src / "b.bin").write_bytes(b"bb")
    db = sqlite3.connect(str(src / "versions.db"))
    db.execute("CREATE TABLE t(x)")
    db.commit()
    db.close()

    dst = tmp_path / "dst-vault"
    result = vault.migrate_vault_dir(src, dst)
    assert result["files"] == 3
    assert not src.exists()
    assert (dst / "sub" / "a.bin").read_bytes() == b"aaa"
    chk = sqlite3.connect(str(dst / "versions.db"))
    chk.execute("SELECT * FROM t")  # 迁移后的库可用
    chk.close()

    other = tmp_path / "other"
    other.mkdir()
    (other / "x").write_bytes(b"x")
    with pytest.raises(ValueError):
        vault.migrate_vault_dir(other, dst)  # 目标非空拒绝


def test_migration_rejects_same_size_corruption_and_preserves_source(tmp_path):
    src = tmp_path / "src-vault"
    src.mkdir()
    payload = src / "object.bin"
    payload.write_bytes(b"GOOD")
    db = sqlite3.connect(str(src / "versions.db"))
    db.execute("CREATE TABLE t(x)")
    db.commit()
    db.close()
    dst = tmp_path / "dst-vault"

    def corrupt_after_copy(done: int, total: int) -> None:
        if done == total:
            (dst / "object.bin").write_bytes(b"EVIL")  # 同为 4 字节

    with pytest.raises(RuntimeError, match="内容不一致"):
        vault.migrate_vault_dir(src, dst, corrupt_after_copy)

    assert payload.read_bytes() == b"GOOD"
    assert not dst.exists()


def test_migration_rejects_nested_destination_without_deleting_source(tmp_path):
    src = tmp_path / "src-vault"
    src.mkdir()
    (src / "keep.bin").write_bytes(b"must survive")

    with pytest.raises(ValueError, match="不能互相嵌套"):
        vault.migrate_vault_dir(src, src / "nested")

    assert (src / "keep.bin").read_bytes() == b"must survive"


def test_live_manager_migration_reconnects_and_keeps_snapshotting(tmp_path):
    deck = tmp_path / "deck.pptx"
    fx.make_pptx(deck, [{"body": "before migration"}])
    manager = VersionManager(index_roots=[str(tmp_path)])
    first = manager.snapshot_now(str(deck), notify=False)
    assert first
    old_vault = Path(manager._db_path).parent
    new_vault = tmp_path / "new-vault"

    result = manager.migrate_vault_dir(new_vault, config_value=str(new_vault))

    assert result["source_backup"] == ""
    assert not old_vault.exists()
    assert manager._db_path == new_vault / "versions.db"
    assert Path(config.get_version_vault_dir()) == new_vault
    assert manager.get_version(first) is not None
    fx.make_pptx(deck, [{"body": "after migration"}])
    second = manager.snapshot_now(str(deck), notify=False)
    assert second and second != first
    assert (new_vault / "versions.db").is_file()
    manager.stop()


def test_live_manager_switch_keeps_old_vault_but_writes_only_new(tmp_path):
    deck = tmp_path / "deck.pptx"
    fx.make_pptx(deck, [{"body": "old vault"}])
    manager = VersionManager(index_roots=[str(tmp_path)])
    assert manager.snapshot_now(str(deck), notify=False)
    old_vault = Path(manager._db_path).parent
    new_vault = tmp_path / "empty-new-vault"

    manager.switch_vault_dir(new_vault, config_value=str(new_vault))
    fx.make_pptx(deck, [{"body": "new vault"}])
    assert manager.snapshot_now(str(deck), notify=False)

    assert old_vault.is_dir()
    old_check = sqlite3.connect(str(old_vault / "versions.db"))
    try:
        assert old_check.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
    finally:
        old_check.close()
    assert len(manager.list_docs()) == 1
    assert manager._db_path == new_vault / "versions.db"
    manager.stop()


def test_live_migration_reconnect_failure_rolls_back_and_manager_stays_usable(
    tmp_path,
    monkeypatch,
):
    deck = tmp_path / "deck.pptx"
    fx.make_pptx(deck, [{"body": "safe before failed move"}])
    manager = VersionManager(index_roots=[str(tmp_path)])
    first = manager.snapshot_now(str(deck), notify=False)
    source = Path(manager._db_path).parent
    destination = tmp_path / "bad-destination"
    real_open = manager._open_vault_connections

    def fail_new_vault(db_file):
        if Path(db_file).parent == destination:
            raise sqlite3.DatabaseError("injected reconnect failure")
        return real_open(db_file)

    monkeypatch.setattr(manager, "_open_vault_connections", fail_new_vault)

    with pytest.raises(RuntimeError, match="已尝试回滚"):
        manager.migrate_vault_dir(destination, config_value=str(destination))

    assert source.is_dir()
    assert not destination.exists()
    assert config.get_version_vault_dir() == ""
    assert manager.get_version(first) is not None
    fx.make_pptx(deck, [{"body": "still usable after rollback"}])
    assert manager.snapshot_now(str(deck), notify=False)
    manager.stop()


def test_live_migration_waits_for_inflight_snapshot_and_keeps_that_version(
    tmp_path,
    monkeypatch,
):
    """设置页迁库撞上 PowerPoint 保存时应排队，不能截走半份版本或死锁。"""
    deck = tmp_path / "deck.pptx"
    fx.make_pptx(deck, [{"body": "before concurrent move"}])
    manager = VersionManager(index_roots=[str(tmp_path)])
    first = manager.snapshot_now(str(deck), notify=False)
    assert first
    fx.make_pptx(deck, [{"body": "save must survive concurrent move"}])

    objects_written = threading.Event()
    release_snapshot = threading.Event()
    migration_done = threading.Event()
    snapshot_ids: list[str | None] = []
    migration_results: list[dict] = []
    real_dedup = vault._dedup_store

    def pause_after_objects(doc_id, source_path):
        result = real_dedup(doc_id, source_path)
        objects_written.set()
        assert release_snapshot.wait(5)
        return result

    monkeypatch.setattr(vault, "_dedup_store", pause_after_objects)
    snapshot_thread = threading.Thread(
        target=lambda: snapshot_ids.append(manager.snapshot_now(str(deck), notify=False))
    )
    destination = tmp_path / "moved-live-vault"
    migration_thread = threading.Thread(
        target=lambda: (
            migration_results.append(
                manager.migrate_vault_dir(destination, config_value=str(destination))
            ),
            migration_done.set(),
        )
    )

    snapshot_thread.start()
    assert objects_written.wait(3)
    migration_thread.start()
    time.sleep(0.15)
    assert not migration_done.is_set()
    release_snapshot.set()
    snapshot_thread.join(8)
    migration_thread.join(8)

    assert not snapshot_thread.is_alive() and not migration_thread.is_alive()
    assert snapshot_ids and snapshot_ids[0]
    assert migration_results and migration_results[0]["source_backup"] == ""
    assert manager._db_path == destination / "versions.db"
    assert manager.get_version(str(snapshot_ids[0])) is not None
    assert manager.audit_repository(deep=True)["ok"] is True
    manager.stop()
