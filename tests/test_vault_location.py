"""版本库存储位置选项 + 幽灵文档收割 + 暂存随库（2026-07-30）。

背景：用户反馈版本管理开启后 C 盘 vault 目录文件暴涨；新增自选存储位置、
暂存随库、幽灵收割（生产实测幽灵文档 48%、约 1.46 GiB 可回收）。
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

import fixtures_gen as fx
from pptx_finder import config
from pptx_finder.versioning import store, vault


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
