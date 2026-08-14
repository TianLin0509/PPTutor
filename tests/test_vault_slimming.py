"""版本库减负（2026-08-14）：幽灵宽限自动收割 / 容量上限 / 隔离封顶 / 暂存清扫 / 库卫生。

生产实测 %LOCALAPPDATA%\\pptx-finder 4.0GB、vault 3.66GB，其中 1.84GB 只被幽灵文档
（源文件已不存在）引用。本文件覆盖新增的自动回收机制；全部用临时目录，不碰真实 vault。
"""
from __future__ import annotations

import json
import os
import time

import fixtures_gen as fx
import pytest
import xxhash

from pptx_finder import config
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager

_DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def _conn():
    c = store.connect(vault.db_path())
    store.init_db(c)
    return c


def _add_full_version(conn, doc_id: str, vid: str, ts: float, payload: bytes = b"full-copy") -> None:
    d = vault.vault_dir() / doc_id / "versions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vid}.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
    (d / f"{vid}.pptx").write_bytes(payload)
    conn.execute(
        "INSERT INTO versions(version_id, doc_id, ts, size, content_hash) VALUES(?,?,?,?,?)",
        (vid, doc_id, ts, len(payload), "h-" + vid),
    )
    conn.commit()


def _add_dedup_version(conn, doc_id: str, vid: str, ts: float, payload: bytes) -> str:
    h = xxhash.xxh64(payload).hexdigest()
    (vault._global_objects_dir() / h).write_bytes(payload)
    d = vault.vault_dir() / doc_id / "versions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vid}.json").write_text(
        json.dumps({
            "mode": "dedup",
            "names": ["ppt/slides/slide1.xml"],
            "parts": {"ppt/slides/slide1.xml": h},
        }),
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO versions(version_id, doc_id, ts, size, content_hash) VALUES(?,?,?,?,?)",
        (vid, doc_id, ts, len(payload), "h-" + vid),
    )
    conn.commit()
    return h


def _set_deleted_at(conn, doc_id: str, deleted_at: float) -> None:
    conn.execute(
        "UPDATE managed_docs SET status='deleted', deleted_at=? WHERE doc_id=?",
        (deleted_at, doc_id),
    )
    conn.commit()


# ---------- 幽灵宽限期与固定盘判定 ----------

def test_ghost_grace_period_blocks_recently_missing(tmp_path):
    gone = tmp_path / "gone.pptx"
    fx.make_pptx(gone, [{"body": "刚删"}])
    conn = _conn()
    store.upsert_doc(conn, "doc-ghost", str(gone), 1.0)
    _add_full_version(conn, "doc-ghost", "v1", 1.0)
    gone.unlink()
    _set_deleted_at(conn, "doc-ghost", time.time() - 10 * _DAY)  # 未满 30 天

    assert vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)]) == []
    res = vault.reap_ghost_docs(
        conn, dry_run=False, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)]
    )
    assert res["ghost_docs"] == 0
    assert store.get_doc(conn, "doc-ghost") is not None


def test_ghost_grace_period_reaps_after_30_days(tmp_path):
    gone = tmp_path / "gone.pptx"
    fx.make_pptx(gone, [{"body": "删了很久"}])
    conn = _conn()
    store.upsert_doc(conn, "doc-ghost", str(gone), 1.0)
    _add_full_version(conn, "doc-ghost", "v1", 1.0)
    gone.unlink()
    _set_deleted_at(conn, "doc-ghost", time.time() - 31 * _DAY)

    ghosts = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    assert [g["doc_id"] for g in ghosts] == ["doc-ghost"]
    res = vault.reap_ghost_docs(
        conn, dry_run=False, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)]
    )
    assert res["ghost_docs"] == 1
    assert store.get_doc(conn, "doc-ghost") is None
    assert not (vault.vault_dir() / "doc-ghost").exists()


def test_ghost_on_unmounted_removable_drive_is_not_reaped(tmp_path):
    """路径全在可移动/网络盘上：未挂载 ≠ 消失，即使早已确认缺失也不收。"""
    conn = _conn()
    store.upsert_doc(conn, "doc-usb", "E:\\removable\\deck.pptx", 1.0)
    _add_full_version(conn, "doc-usb", "v1", 1.0)
    _set_deleted_at(conn, "doc-usb", time.time() - 90 * _DAY)

    fixed = [str(tmp_path)]  # E:\\... 不在固定盘列表里
    assert vault.list_ghost_docs(conn, min_missing_sec=0, fixed_roots=fixed) == []
    res = vault.reap_ghost_docs(conn, dry_run=False, fixed_roots=fixed)
    assert res["ghost_docs"] == 0
    assert store.get_doc(conn, "doc-usb") is not None


def test_mark_ghost_docs_seen_stamps_first_missing_only(tmp_path):
    gone = tmp_path / "gone.pptx"
    fx.make_pptx(gone, [{"body": "x"}])
    conn = _conn()
    store.upsert_doc(conn, "doc-ghost", str(gone), 1.0)
    _add_full_version(conn, "doc-ghost", "v1", 1.0)
    gone.unlink()

    assert vault.mark_ghost_docs_seen(conn, fixed_roots=[str(tmp_path)]) == 1
    first = float(store.get_doc(conn, "doc-ghost")["deleted_at"])
    assert first > 0
    assert vault.mark_ghost_docs_seen(conn, fixed_roots=[str(tmp_path)]) == 0  # 已记过不重置
    assert float(store.get_doc(conn, "doc-ghost")["deleted_at"]) == first


def test_set_status_marks_and_clears_deleted_at(tmp_path):
    live = tmp_path / "live.pptx"
    fx.make_pptx(live, [{"body": "x"}])
    conn = _conn()
    store.upsert_doc(conn, "doc", str(live), 1.0)
    assert float(store.get_doc(conn, "doc")["deleted_at"]) == 0  # upsert 清零

    store.set_status(conn, "doc", "deleted")
    first = float(store.get_doc(conn, "doc")["deleted_at"])
    assert first > 0
    store.set_status(conn, "doc", "deleted")  # 重复标记不刷新时刻
    assert float(store.get_doc(conn, "doc")["deleted_at"]) == first
    store.set_status(conn, "doc", "active")
    assert float(store.get_doc(conn, "doc")["deleted_at"]) == 0


def test_heavy_maintenance_reaps_aged_ghosts_and_marks_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTUTOR_VAULT_HEAVY_MAINTENANCE_SEC", "0")  # 每次都跑重维护
    p = tmp_path / "gone.pptx"
    fx.make_pptx(p, [{"body": "自动收割"}])
    manager = VersionManager()
    assert manager.snapshot_now(str(p))
    p.unlink()
    did = vault.doc_id_for(str(p))
    _set_deleted_at(manager._conn, did, time.time() - 31 * _DAY)  # 对账早已确认缺失

    result = manager.run_vault_maintenance()

    assert result["heavy_due"] is True
    assert result["ghosts"]["ghost_docs"] == 1
    assert store.get_doc(manager._conn, did) is None

    # 宽限内的新缺失只补记、不收割
    p2 = tmp_path / "fresh.pptx"
    fx.make_pptx(p2, [{"body": "刚没的"}])
    assert manager.snapshot_now(str(p2))
    p2.unlink()
    did2 = vault.doc_id_for(str(p2))

    result2 = manager.run_vault_maintenance()

    assert result2["ghosts_marked"] >= 1
    assert result2["ghosts"]["ghost_docs"] == 0
    doc2 = store.get_doc(manager._conn, did2)
    assert doc2 is not None and float(doc2["deleted_at"]) > 0


# ---------- 容量上限 ----------

def test_enforce_size_budget_evicts_oldest_healthy_first(tmp_path):
    conn = _conn()
    payload = b"x" * 4096
    store.upsert_doc(conn, "doc", str(tmp_path / "deck.pptx"), 1.0)
    for vid, ts in (("v-old", 1.0), ("v-mid", 2.0), ("v-new", 3.0)):
        _add_full_version(conn, "doc", vid, ts, payload)
    total = vault._budget_relevant_bytes()
    # 预算 = 总量减 2000B：驱逐一个 4096B 版本即达标，且必须是最老的
    res = vault.enforce_size_budget(conn, max_bytes=total - 2000)

    assert res["evicted_versions"] == 1
    remaining = {r["version_id"] for r in store.list_versions(conn, "doc")}
    assert remaining == {"v-mid", "v-new"}
    assert vault._budget_relevant_bytes() <= total - 2000


def test_enforce_size_budget_exempts_branch_base_and_quarantined(tmp_path):
    conn = _conn()
    payload = b"y" * 4096
    store.upsert_doc(conn, "parent", str(tmp_path / "deck.pptx"), 1.0)
    _add_full_version(conn, "parent", "v-base", 1.0, payload)
    _add_full_version(conn, "parent", "v-q", 2.0, payload)
    conn.execute(
        "UPDATE versions SET health='invalid', health_error='deep: corrupt' WHERE version_id='v-q'"
    )
    _add_full_version(conn, "parent", "v-healthy", 3.0, payload)
    store.record_branch(conn, "child", "parent", "v-base", 4.0, "copy/hash_match")
    conn.commit()

    res = vault.enforce_size_budget(conn, max_bytes=1)  # 极限预算：能走的都走

    assert res["evicted_versions"] == 1
    remaining = {r["version_id"] for r in store.list_versions(conn, "parent")}
    assert remaining == {"v-base", "v-q"}  # 分支基 + 隔离豁免
    assert res["gc"] is not None and not res["gc"]["aborted"]


def test_enforce_size_budget_reclaims_shared_objects(tmp_path):
    conn = _conn()
    store.upsert_doc(conn, "doc", str(tmp_path / "deck.pptx"), 1.0)
    h = _add_dedup_version(conn, "doc", "v1", 1.0, b"shared-object-bytes")
    obj = vault._global_objects_dir() / h
    assert obj.exists()

    res = vault.enforce_size_budget(conn, max_bytes=1)

    assert res["evicted_versions"] == 1
    assert not obj.exists()  # 驱逐后 GC 回收了不再被引用的对象


def test_enforce_size_budget_zero_means_unlimited(tmp_path):
    conn = _conn()
    store.upsert_doc(conn, "doc", str(tmp_path / "deck.pptx"), 1.0)
    _add_full_version(conn, "doc", "v1", 1.0)
    res = vault.enforce_size_budget(conn, max_bytes=0)
    assert res["skipped"] is True
    assert store.get_version(conn, "v1") is not None


def test_vault_max_mb_config_roundtrip():
    assert config.get_vault_max_mb() == 5120
    config.set_vault_max_mb(10240)
    assert config.get_vault_max_mb() == 10240
    config.set_vault_max_mb(0)  # 0 = 不限
    assert config.get_vault_max_mb() == 0


# ---------- 隔离版本封顶 ----------

def test_quarantined_versions_capped_per_doc():
    conn = store.connect(":memory:")
    store.init_db(conn)
    manager = VersionManager(conn)
    manager.set_retention_limit(0)  # 健康配额不限，只看隔离封顶
    manager._quarantine_keep_per_doc = 3
    store.upsert_doc(conn, "doc", "C:/deck.pptx", 1)
    for i in range(6):
        store.add_version(
            conn, f"q-{i}", "doc", i + 1, f"s-{i}", 1, 10, f"q-{i}",
            health="invalid", health_error="deep: corrupt",
        )
    store.add_version(conn, "h-1", "doc", 100, "s-h", 1, 10, "h-1")
    conn.commit()

    manager._enforce_quota("doc")

    kept = {row["version_id"] for row in store.list_versions(conn, "doc")}
    assert kept == {"h-1", "q-3", "q-4", "q-5"}  # 最新 3 个隔离版 + 健康版不动


def test_quarantine_cap_exempts_branch_base():
    conn = store.connect(":memory:")
    store.init_db(conn)
    manager = VersionManager(conn)
    manager._quarantine_keep_per_doc = 1
    store.upsert_doc(conn, "doc", "C:/deck.pptx", 1)
    for i in range(3):
        store.add_version(
            conn, f"q-{i}", "doc", i + 1, f"s-{i}", 1, 10, f"q-{i}",
            health="invalid", health_error="x",
        )
    store.record_branch(conn, "child", "doc", "q-0", 10, "copy/hash_match")
    conn.commit()

    manager._enforce_quota("doc")

    kept = {row["version_id"] for row in store.list_versions(conn, "doc")}
    assert kept == {"q-0", "q-2"}  # q-0 是分支基豁免；隔离版只留最新 1 个


# ---------- 暂存改道与清扫覆盖 ----------

def test_verify_temp_staged_in_vault_tmp(tmp_path, monkeypatch):
    src = tmp_path / "a.pptx"
    fx.make_pptx(src, [{"body": "自检暂存"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))
    manifest = vault.manifest_for(did, vid)
    assert manifest["mode"] == "dedup"

    calls = []
    real_mkstemp = vault.tempfile.mkstemp

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(vault.tempfile, "mkstemp", spy)
    assert vault._verify(did, manifest["names"], manifest["parts"]) is True
    assert calls and calls[0]["dir"].endswith("_tmp")
    assert calls[0]["prefix"] == ".pptdoctor-verify-"


def test_preview_temp_staged_in_vault_tmp(tmp_path, monkeypatch):
    import pptx_finder.versioning.manager as manager_mod

    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    manager = VersionManager(store.connect(tmp_path / "preview.db"))
    assert manager.snapshot_now(str(p))
    version_id = manager.list_versions(str(p))[0]["version_id"]
    fake_png = tmp_path / "fake.png"
    monkeypatch.setattr(
        "pptx_finder.versioning.manager.renderer.render_page_once",
        lambda *a, **k: (fake_png.write_bytes(b"png"), str(fake_png))[1],
    )
    calls = []
    real_mkstemp = manager_mod.tempfile.mkstemp

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(manager_mod.tempfile, "mkstemp", spy)
    assert manager.ensure_version_preview(version_id) == str(fake_png)
    assert calls[0]["dir"].endswith("_tmp")
    assert calls[0]["prefix"] == ".pptdoctor-preview-"


def test_sweep_covers_verify_preview_restore_temps(tmp_path):
    tmp_dir = vault.vault_dir() / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stale = [
        tmp_dir / f"{prefix}dead{i}.pptx"
        for i, prefix in enumerate((
            ".pptdoctor-snapshot-",
            ".pptdoctor-verify-",
            ".pptdoctor-preview-",
            ".pptdoctor-restore-",
        ))
    ]
    for p in stale:
        p.write_bytes(b"x")
    keep = tmp_dir / "unrelated.tmp"
    keep.write_bytes(b"x")

    assert vault.sweep_stale_snapshot_temps() >= len(stale)
    assert all(not p.exists() for p in stale)
    assert keep.exists()


# ---------- .object-* 崩溃残留 / 迁移备份 / 库卫生 ----------

def test_collect_garbage_sweeps_crashed_object_temps(tmp_path):
    conn = _conn()
    objdir = vault._global_objects_dir()
    stale = objdir / ".object-deadbeef"
    stale.write_bytes(b"partial")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    inflight = objdir / ".object-writing"
    inflight.write_bytes(b"partial")
    junk = objdir / "notes.txt"  # 非我方暂存命名，不动
    junk.write_bytes(b"x")

    res = vault.collect_garbage(conn, dry_run=False)

    assert res["temp_objects_removed"] == 1
    assert not stale.exists()
    assert inflight.exists()  # 1 小时内：可能是在途写入
    assert junk.exists()


def test_sweep_stale_migration_backups(tmp_path):
    data_root = tmp_path / "data"
    backups = tmp_path / "vault" / "backups"
    data_root.mkdir()
    backups.mkdir(parents=True)
    old = time.time() - 40 * _DAY
    stale_db = data_root / "index.db.pre-v110.bak"
    stale_db2 = data_root / "index.db.bak"
    young = data_root / "index.db.recent.bak"
    keep_db = data_root / "index.db"
    random_bak = data_root / "notes.bak"
    vault_bak = backups / "versions-pre-v105-20260601.db"
    vault_live = backups / "versions.db"  # 不匹配迁移备份命名模式
    for p in (stale_db, stale_db2, young, keep_db, random_bak, vault_bak, vault_live):
        p.write_bytes(b"x")
    for p in (stale_db, stale_db2, vault_bak):
        os.utime(p, (old, old))

    deleted = vault.sweep_stale_migration_backups(
        data_root=str(data_root), vault_root=str(tmp_path / "vault")
    )

    assert deleted == 3
    assert not stale_db.exists() and not stale_db2.exists() and not vault_bak.exists()
    assert young.exists()  # 未满 30 天
    assert keep_db.exists() and random_bak.exists() and vault_live.exists()


def test_maintain_db_optimizes_fts_and_checkpoints(tmp_path):
    conn = _conn()
    store.index_pages(conn, "d", "v1", [(1, "算力 集群 研报")])
    conn.commit()

    res = vault.maintain_db(conn)

    assert res["error"] == ""
    assert res["fts_optimized"] == 1
    assert res["checkpointed"] is True
    assert res["vacuumed"] is False  # 小库不到 VACUUM 阈值


def test_maintain_db_vacuums_when_thresholds_crossed(tmp_path):
    conn = _conn()
    for i in range(400):
        store.index_pages(conn, "d", f"v{i}", [(1, "词" * 800 + str(i))])
    conn.commit()
    conn.execute("DELETE FROM version_pages_fts")
    conn.commit()

    res = vault.maintain_db(conn, min_free_bytes=1, min_free_ratio=0.0)

    assert res["error"] == ""
    assert res["free_bytes_before"] > 0
    assert res["vacuumed"] is True


# ---------- 维护定时器化 ----------

def test_vault_maintenance_loop_runs_periodically_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTUTOR_VAULT_HEAVY_MAINTENANCE_SEC", "0.15")
    manager = VersionManager()
    calls = []
    real_run = manager.run_vault_maintenance

    def counted():
        calls.append(time.time())
        return real_run()

    manager.run_vault_maintenance = counted
    manager._start_vault_maintenance()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.05)
        assert len(calls) >= 2  # 常驻期间周期性触发，不再「每 launch 一次」
    finally:
        manager.stop()
    n = len(calls)
    time.sleep(0.5)
    assert len(calls) == n  # stop 后不再新增


# ---------- 设置 UI ----------

def test_settings_vault_max_combo_persists(qtbot, tmp_path):
    from pptx_finder.ui.settings_dialog import SettingsDialog

    class FakeMgr:
        def list_docs(self):
            return []

    dlg = SettingsDialog(FakeMgr())
    qtbot.addWidget(dlg)
    assert dlg.vault_max.findData(5120) >= 0
    dlg.vault_max.setCurrentIndex(dlg.vault_max.findData(10240))
    assert config.get_vault_max_mb() == 10240
    dlg.vault_max.setCurrentIndex(dlg.vault_max.findData(0))  # 不限
    assert config.get_vault_max_mb() == 0


def test_settings_vault_size_label_measures_in_background(qtbot, tmp_path):
    from pptx_finder.ui.settings_dialog import SettingsDialog

    class FakeMgr:
        def list_docs(self):
            return []

    dlg = SettingsDialog(FakeMgr())
    qtbot.addWidget(dlg)
    dlg._refresh_vault_size()
    qtbot.waitUntil(
        lambda: "测量" not in dlg._vault_size_label.text(),
        timeout=3000,
    )
    assert "0 MB" in dlg._vault_size_label.text()  # 隔离环境无 vault 目录 → 0
