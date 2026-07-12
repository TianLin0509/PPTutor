from __future__ import annotations

import os
import threading

import fixtures_gen as fx
import pytest

from pptx_finder import app
from pptx_finder.config import DEFAULT_VERSION_KEEP_PER_DOC
from pptx_finder.parser import parse_pptx
from pptx_finder.text_tokenize import tokenize
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager
from pptx_finder.versioning.watcher import _Handler


@pytest.fixture(autouse=True)
def _isolated_version_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def test_autostart_sync_rewrites_enabled_link_target(monkeypatch):
    writes: list[bool] = []
    monkeypatch.setattr(app, "get_autostart", lambda: True)
    monkeypatch.setattr(app.autostart, "is_enabled", lambda: True)
    monkeypatch.setattr(
        app.autostart,
        "set_enabled",
        lambda enabled: writes.append(enabled) or True,
    )

    assert app._sync_autostart_preference() is True
    assert writes == [True]


def test_autostart_sync_removes_stale_link_when_preference_is_off(monkeypatch):
    writes: list[bool] = []
    monkeypatch.setattr(app, "get_autostart", lambda: False)
    monkeypatch.setattr(app.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(
        app.autostart,
        "set_enabled",
        lambda enabled: writes.append(enabled) or True,
    )

    assert app._sync_autostart_preference() is True
    assert writes == [False]


def test_reconcile_cursor_eventually_checks_document_after_first_batch(tmp_path):
    conn = store.connect(tmp_path / "versions.db")
    store.init_db(conn)
    paths: list[str] = []
    for i in range(201):
        path = tmp_path / f"deck-{i:03}.pptx"
        path.write_bytes(b"placeholder")
        paths.append(str(path))
        store.upsert_doc(conn, f"doc-{i:03}", str(path), float(10_000 - i))
    conn.commit()

    manager = VersionManager(conn)
    manager._reconcile_batch_docs = 200
    checked: list[str] = []
    manager.snapshot_now = lambda path, notify=True: checked.append(path) or None

    manager.reconcile_known_docs(scan_new_files=False)
    manager.reconcile_known_docs(scan_new_files=False)

    assert set(paths) <= set(checked)


def test_deleted_event_calls_version_status_callback():
    indexed: list[str] = []
    removed: list[str] = []
    handler = _Handler(
        lambda _path: None,
        on_content_saved=indexed.append,
        on_removed=removed.append,
    )

    class Event:
        is_directory = False
        src_path = r"C:\Users\me\Desktop\gone.pptx"

    handler.on_deleted(Event())

    assert indexed == [Event.src_path]
    assert removed == [Event.src_path]


def test_history_details_orders_newest_match_first(tmp_path):
    conn = store.connect(tmp_path / "versions.db")
    store.init_db(conn)
    doc_id = "doc"
    store.upsert_doc(conn, doc_id, str(tmp_path / "deck.pptx"), 1)
    for version_id, ts in (("old", 10.0), ("new", 20.0)):
        store.add_version(conn, version_id, doc_id, ts, "s", 1, 1, version_id)
        store.index_pages(
            conn,
            doc_id,
            version_id,
            [(1, tokenize("needle content"))],
        )
    store.set_version_health(conn, "old", "invalid", "bad object")
    conn.commit()

    result = VersionManager._search_history_details_on_conn(
        conn,
        "needle",
        "needle",
        200,
    )

    assert [row["ts"] for row in result["rows"]] == [20.0, 10.0]
    assert result["rows"][0]["health"] == "ok"
    assert result["rows"][1]["health"] == "invalid"
    assert result["rows"][1]["health_error"] == "bad object"


def test_snapshot_reads_one_stable_copy_when_source_changes_mid_capture(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "deck.pptx"
    fx.make_pptx(source, [{"body": "STATE_V1"}])
    conn = store.connect(vault.db_path())
    store.init_db(conn)
    real_dedup_store = vault._dedup_store
    captured_sources: list[str] = []

    def mutate_original_then_store(doc_id: str, snapshot_source: str):
        captured_sources.append(snapshot_source)
        fx.make_pptx(source, [{"body": "STATE_V2"}])
        return real_dedup_store(doc_id, snapshot_source)

    monkeypatch.setattr(vault, "_dedup_store", mutate_original_then_store)
    first = vault.snapshot(conn, str(source))
    assert first
    assert captured_sources and os.path.abspath(captured_sources[0]) != os.path.abspath(source)

    restored = tmp_path / "restored-v1.pptx"
    assert vault.rebuild_to(vault.doc_id_for(str(source)), first, str(restored))
    text = " ".join(page.raw_text for page in parse_pptx(str(restored)).pages)
    assert "STATE_V1" in text
    assert "STATE_V2" not in text

    second = vault.snapshot(conn, str(source))
    assert second and second != first


def test_corrupt_stable_source_is_not_recorded_as_recovery_point(tmp_path):
    source = tmp_path / "broken.pptx"
    source.write_bytes(b"PK\x03\x04 truncated package")
    conn = store.connect(vault.db_path())
    store.init_db(conn)

    with pytest.raises(vault.InvalidSnapshotError):
        vault.snapshot(conn, str(source))

    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0


def test_invalid_full_snapshot_never_replaces_destination(tmp_path):
    conn = store.connect(vault.db_path())
    store.init_db(conn)
    doc_id = "doc"
    version_id = "invalid-full"
    bad = vault.version_file(doc_id, version_id)
    bad.write_bytes(b"PK\x03\x04 truncated package")
    vault._manifest_path(doc_id, version_id).write_text(
        '{"mode":"full","names":[],"parts":{}}',
        encoding="utf-8",
    )
    dest = tmp_path / "current.pptx"
    original = b"CURRENT MUST SURVIVE"
    dest.write_bytes(original)

    assert vault.rebuild_to(doc_id, version_id, str(dest)) is False
    assert dest.read_bytes() == original


def test_restore_target_survives_pre_restore_snapshot_quota(tmp_path):
    source = tmp_path / "quota-restore.pptx"
    manager = VersionManager()
    manager.set_retention_limit(2)

    fx.make_pptx(source, [{"body": "RESTORE_TARGET_V1"}])
    target_version = manager.snapshot_now(str(source))
    fx.make_pptx(source, [{"body": "SAVED_V2"}])
    assert manager.snapshot_now(str(source))

    # This third state is intentionally not snapshotted yet. restore_to() must
    # preserve it first, but that quota pass must not delete the selected v1.
    fx.make_pptx(source, [{"body": "UNSNAPSHOTTED_V3"}])
    assert manager.restore_to(str(source), str(target_version))

    restored_text = " ".join(
        page.raw_text for page in parse_pptx(str(source)).pages
    )
    assert "RESTORE_TARGET_V1" in restored_text
    assert store.get_version(manager._conn, str(target_version)) is not None


def test_dedup_verification_rejects_parseable_but_hash_mismatched_object(tmp_path):
    source = tmp_path / "parseable-object-corruption.pptx"
    fx.make_pptx(source, [{"body": "ORIGINAL_TEXT"}])
    doc_id = "doc"
    names, parts = vault._dedup_store(doc_id, str(source))
    slide_name = next(
        name for name in names if name.startswith("ppt/slides/slide1.xml")
    )
    object_path = vault._global_objects_dir() / parts[slide_name]
    original = object_path.read_bytes()
    mutated = original.replace(b"ORIGINAL_TEXT", b"MUTATED_TEXT")
    assert mutated != original
    object_path.write_bytes(mutated)

    # The package remains valid OpenXML and parseable, but no longer matches
    # the manifest's content-addressed hash map.
    assert vault._verify(doc_id, names, parts) is False


def test_summary_stats_counts_inherited_branch_history(tmp_path):
    parent = tmp_path / "parent.pptx"
    child = tmp_path / "child.pptx"
    fx.make_pptx(parent, [{"body": "v1"}])
    manager = VersionManager()
    manager.snapshot_now(str(parent))
    fx.make_pptx(parent, [{"body": "v2"}])
    manager.snapshot_now(str(parent))
    child.write_bytes(parent.read_bytes())
    assert manager.snapshot_now(str(child)) is None

    stats = manager.summary_stats()

    assert stats["protected_docs"] == 2
    assert stats["rollback_docs"] == 2
    assert stats["single_version_docs"] == 0


def test_summary_stats_excludes_quarantined_points_from_recovery_kpis():
    conn = store.connect(":memory:")
    store.init_db(conn)
    for doc_id in ("invalid-only", "one-healthy"):
        store.upsert_doc(conn, doc_id, f"C:/{doc_id}.pptx", 1)
    store.add_version(
        conn,
        "bad-only",
        "invalid-only",
        1,
        "s1",
        1,
        10,
        "bad-only",
        health="invalid",
        health_error="bad",
    )
    store.add_version(
        conn,
        "good",
        "one-healthy",
        1,
        "s1",
        1,
        10,
        "good",
    )
    store.add_version(
        conn,
        "bad-newer",
        "one-healthy",
        2,
        "s2",
        1,
        10,
        "bad-newer",
        health="invalid",
        health_error="bad",
    )
    conn.commit()

    stats = store.summary_stats(conn)

    assert stats["total_versions"] == 3
    assert stats["healthy_versions"] == 1
    assert stats["unhealthy_versions"] == 2
    assert stats["protected_docs"] == 1
    assert stats["rollback_docs"] == 0
    assert stats["single_version_docs"] == 1


def test_repository_fsck_verifies_every_object_hash(tmp_path):
    source = tmp_path / "deck.pptx"
    fx.make_pptx(source, [{"body": "healthy repository"}])
    manager = VersionManager()
    assert manager.snapshot_now(str(source))

    clean = manager.audit_repository(deep=True)
    assert clean["ok"] is True
    assert clean["versions_checked"] == 1
    assert clean["objects_hashed"] > 0
    assert clean["hash_errors"] == 0

    version_id = manager.list_versions(str(source))[0]["version_id"]
    object_path = next(vault._global_objects_dir().iterdir())
    original = object_path.read_bytes()
    object_path.write_bytes(b"silent corruption")
    broken = manager.audit_repository(deep=True)
    assert broken["ok"] is False
    assert broken["hash_errors"] == 1
    assert broken["invalid_count"] == 1
    assert manager.get_version(version_id)["health"] == "invalid"
    assert str(manager.get_version(version_id)["health_error"]).startswith("deep:")

    # A quick startup check cannot prove object bytes healthy, so it must not
    # accidentally clear a quarantine established by a deep hash check.
    quick = manager.audit_repository(deep=False)
    assert quick["ok"] is False
    assert quick["quarantined_versions"] == 1
    assert manager.get_version(version_id)["health"] == "invalid"

    object_path.write_bytes(original)
    repaired = manager.audit_repository(deep=True)
    assert repaired["ok"] is True
    assert manager.get_version(version_id)["health"] == "ok"


def test_repository_fsck_marks_legacy_invalid_full_version(tmp_path):
    conn = store.connect(vault.db_path())
    store.init_db(conn)
    doc_id = "doc"
    version_id = "legacy-bad-full"
    path = str(tmp_path / "old.pptx")
    store.upsert_doc(conn, doc_id, path, 1)
    store.add_version(conn, version_id, doc_id, 1, "s", 0, 20, "legacy")
    store.set_latest(conn, doc_id, version_id)
    conn.commit()
    vault.version_file(doc_id, version_id).write_bytes(b"PK\x03\x04 truncated")
    vault._manifest_path(doc_id, version_id).write_text(
        '{"mode":"full","names":[],"parts":{}}',
        encoding="utf-8",
    )
    manager = VersionManager(conn)

    result = manager.audit_repository(deep=False)

    assert result["ok"] is False
    assert result["invalid_count"] == 1
    assert store.get_version(conn, version_id)["health"] == "invalid"
    assert manager.restore_to(path, version_id) is False


def test_repository_fsck_detects_manifest_vs_database_hash_mismatch(tmp_path):
    source = tmp_path / "hash-mismatch.pptx"
    fx.make_pptx(source, [{"body": "original canonical content"}])
    manager = VersionManager()
    version_id = manager.snapshot_now(str(source))
    manager._conn.execute(
        "UPDATE versions SET content_hash=? WHERE version_id=?",
        ("pkg:0000000000000000", version_id),
    )
    manager._conn.commit()

    result = manager.audit_repository(deep=False)

    assert result["ok"] is False
    assert result["invalid_count"] == 1
    version = store.get_version(manager._conn, version_id)
    assert version["health"] == "invalid"
    assert "content hash mismatch" in version["health_error"]


def test_deleted_file_recovery_falls_back_to_latest_healthy_version(tmp_path):
    source = tmp_path / "recover-me.pptx"
    fx.make_pptx(source, [{"body": "last healthy content"}])
    manager = VersionManager()
    healthy_id = manager.snapshot_now(str(source))
    doc_id = vault.doc_id_for(str(source))
    store.add_version(
        manager._conn,
        "newer-but-invalid",
        doc_id,
        9_999_999_999,
        "bad-session",
        0,
        10,
        "bad",
        health="invalid",
        health_error="deep: corrupt object",
    )
    store.set_latest(manager._conn, doc_id, "newer-but-invalid")
    manager._conn.commit()
    source.unlink()
    store.set_status(manager._conn, doc_id, "deleted")

    assert manager.recover(doc_id) is True
    assert source.exists()
    assert parse_pptx(str(source)).pages[0].raw_text == "last healthy content"
    assert store.get_doc(manager._conn, doc_id)["status"] == "active"
    assert store.get_version(manager._conn, healthy_id)["health"] == "ok"


def test_copy_identity_never_branches_from_quarantined_version(tmp_path):
    parent = tmp_path / "parent.pptx"
    copied = tmp_path / "copied.pptx"
    fx.make_pptx(parent, [{"body": "same valid package"}])
    manager = VersionManager()
    parent_version = manager.snapshot_now(str(parent))
    store.set_version_health(
        manager._conn,
        parent_version,
        "invalid",
        "deep: object hash mismatch",
    )
    manager._conn.commit()
    copied.write_bytes(parent.read_bytes())

    copied_version = manager.snapshot_now(str(copied))

    copied_doc_id = vault.doc_id_for(str(copied))
    assert copied_version is not None
    assert store.get_branch(manager._conn, copied_doc_id) is None
    assert store.get_version(manager._conn, copied_version)["health"] == "ok"


def test_unchanged_file_rebuilds_healthy_baseline_after_quarantine(tmp_path):
    source = tmp_path / "quarantined-latest.pptx"
    fx.make_pptx(source, [{"body": "still valid on disk"}])
    manager = VersionManager()
    bad_version = manager.snapshot_now(str(source))
    store.set_version_health(
        manager._conn,
        bad_version,
        "invalid",
        "deep: stored object is corrupt",
    )
    manager._conn.commit()

    replacement = manager.snapshot_now(str(source))

    assert replacement is not None
    assert replacement != bad_version
    assert store.get_version(manager._conn, replacement)["health"] == "ok"


def test_clean_reconcile_cycle_clears_stale_error():
    manager = VersionManager()
    manager._reconcile_last_error = "old transient failure"

    assert manager.reconcile_known_docs(limit=1, scan_new_files=False) == 0

    line = next(
        row for row in manager.diagnostic_lines()
        if row.startswith("version_reconcile:")
    )
    assert "error=-" in line


def test_reconcile_reactivates_deleted_path_recreated_while_app_was_off(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "recreated.pptx"
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_DIRS", str(tmp_path))
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_COMMON_DIRS", "0")
    manager = VersionManager()
    fx.make_pptx(source, [{"body": "before delete"}])
    assert manager.snapshot_now(str(source))
    doc_id = vault.doc_id_for(str(source))
    source.unlink()
    assert manager.mark_deleted(str(source)) is True
    fx.make_pptx(source, [{"body": "recreated while offline"}])

    created = manager.reconcile_known_docs(
        limit=1,
        notify=False,
        scan_new_files=True,
    )

    assert created == 1
    assert store.get_doc(manager._conn, doc_id)["status"] == "active"
    assert len(store.list_versions(manager._conn, doc_id)) == 2


def test_default_retention_matches_settings_recommendation():
    manager = VersionManager()

    assert manager._keep_per_doc == DEFAULT_VERSION_KEEP_PER_DOC == 100


def test_retention_preserves_session_milestones_before_dense_recent_saves():
    conn = store.connect(":memory:")
    store.init_db(conn)
    manager = VersionManager(conn)
    manager.set_retention_limit(3)
    doc_id = "doc"
    store.upsert_doc(conn, doc_id, "C:/deck.pptx", 1)
    for version_id, ts, session_id in (
        ("old-milestone", 1, "old-session"),
        ("new-1", 2, "new-session"),
        ("new-2", 3, "new-session"),
        ("new-3", 4, "new-session"),
        ("new-4", 5, "new-session"),
    ):
        store.add_version(
            conn, version_id, doc_id, ts, session_id, 1, 10, version_id
        )
    conn.commit()

    manager._enforce_quota(doc_id)

    kept = {row["version_id"] for row in store.list_versions(conn, doc_id)}
    assert len(kept) == 3
    assert "new-4" in kept
    assert "old-milestone" in kept


def test_quarantined_versions_do_not_consume_healthy_retention_budget():
    conn = store.connect(":memory:")
    store.init_db(conn)
    manager = VersionManager(conn)
    manager.set_retention_limit(2)
    doc_id = "doc"
    store.upsert_doc(conn, doc_id, "C:/deck.pptx", 1)
    for version_id, ts, health in (
        ("healthy-old", 1, "ok"),
        ("healthy-new", 2, "ok"),
        ("quarantined-newest", 3, "invalid"),
    ):
        store.add_version(
            conn,
            version_id,
            doc_id,
            ts,
            version_id,
            1,
            10,
            version_id,
            health=health,
            health_error="deep: corrupt" if health != "ok" else "",
        )
    conn.commit()

    manager._enforce_quota(doc_id)

    kept = {row["version_id"] for row in store.list_versions(conn, doc_id)}
    assert kept == {"healthy-old", "healthy-new", "quarantined-newest"}


def test_manual_audit_waits_for_legacy_migration_without_blocking_snapshots(
    monkeypatch,
    tmp_path,
):
    manager = VersionManager()
    migration_started = threading.Event()
    allow_migration_finish = threading.Event()
    audit_attempted = threading.Event()
    audit_finished = threading.Event()

    def slow_migration():
        migration_started.set()
        assert allow_migration_finish.wait(2)
        return {"errors": 0}

    monkeypatch.setattr(vault, "migrate_legacy_objects", slow_migration)
    monkeypatch.setattr(
        vault,
        "audit_repository",
        lambda _conn, deep=False: {
            "ok": True,
            "deep": deep,
            "versions_checked": 0,
            "invalid_versions": {},
            "invalid_count": 0,
            "missing_objects": 0,
            "hash_errors": 0,
        },
    )
    monkeypatch.setattr(
        vault,
        "collect_garbage",
        lambda _conn, dry_run=True: {"aborted": False, "errors": 0},
    )

    maintenance = threading.Thread(target=manager.run_vault_maintenance)

    def run_manual_audit():
        audit_attempted.set()
        manager.audit_repository(deep=True)
        audit_finished.set()

    manual_audit = threading.Thread(target=run_manual_audit)
    maintenance.start()
    assert migration_started.wait(1)
    manual_audit.start()
    assert audit_attempted.wait(1)
    assert not audit_finished.wait(0.1)

    source = tmp_path / "save-during-maintenance.pptx"
    fx.make_pptx(source, [{"body": "save must not wait for fsck coordination"}])
    snapshot_result = []
    snapshot_finished = threading.Event()

    def take_snapshot():
        snapshot_result.append(manager.snapshot_now(str(source)))
        snapshot_finished.set()

    snapshot = threading.Thread(target=take_snapshot)
    snapshot.start()
    assert snapshot_finished.wait(1)
    snapshot.join(1)
    assert snapshot_result and snapshot_result[0]

    allow_migration_finish.set()
    maintenance.join(2)
    manual_audit.join(2)

    assert not maintenance.is_alive()
    assert not manual_audit.is_alive()
    assert audit_finished.is_set()


def test_vault_maintenance_throttles_heavy_migration_and_gc(monkeypatch):
    calls = []
    manager = VersionManager()
    monkeypatch.setattr(
        vault,
        "migrate_legacy_objects",
        lambda: calls.append("migration") or {"errors": 0},
    )
    monkeypatch.setattr(
        vault,
        "collect_garbage",
        lambda _conn, dry_run=True: calls.append("gc") or {
            "aborted": False,
            "errors": 0,
        },
    )

    first = manager.run_vault_maintenance()
    second = manager.run_vault_maintenance()

    assert calls == ["migration", "gc"]
    assert first["garbage"]["aborted"] is False
    assert second["migration"]["skipped"] is True
    assert second["garbage"]["skipped"] is True
