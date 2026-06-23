from __future__ import annotations

import shutil

import fixtures_gen as fx

from pptx_finder.parser import parse_pptx
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


def _mgr():
    return VersionManager()


def test_rename_preserves_doc_identity_and_history_without_move_event(tmp_path):
    p = tmp_path / "deck.pptx"
    renamed = tmp_path / "renamed.pptx"
    fx.make_pptx(p, [{"body": "v1 ROOT"}])
    mgr = _mgr()
    mgr.snapshot_now(str(p))
    fx.make_pptx(p, [{"body": "v2 CURRENT"}])
    mgr.snapshot_now(str(p))
    original_doc_id = vault.doc_id_for(str(p))

    p.rename(renamed)
    assert mgr.snapshot_now(str(renamed)) is None

    assert vault.doc_id_for(str(renamed)) != original_doc_id
    assert store.get_doc(mgr._conn, original_doc_id)["path"] == str(renamed)
    assert [v["version_id"] for v in mgr.list_versions(str(renamed))] == [
        v["version_id"] for v in store.list_versions(mgr._conn, original_doc_id)
    ]
    assert len(mgr.list_versions(str(renamed))) == 2


def test_move_event_preserves_doc_identity_then_records_future_edits(tmp_path):
    src = tmp_path / "src.pptx"
    dest_dir = tmp_path / "sub"
    dest_dir.mkdir()
    dest = dest_dir / "moved.pptx"
    fx.make_pptx(src, [{"body": "v1 BEFORE MOVE"}])
    mgr = _mgr()
    mgr.snapshot_now(str(src))
    original_doc_id = vault.doc_id_for(str(src))

    src.rename(dest)
    assert mgr.move_path(str(src), str(dest))
    fx.make_pptx(dest, [{"body": "v2 AFTER MOVE"}])
    assert mgr.snapshot_now(str(dest))

    assert store.get_doc(mgr._conn, original_doc_id)["path"] == str(dest)
    assert len(mgr.list_versions(str(dest))) == 2
    assert len(mgr.list_versions_by_doc(original_doc_id)) == 2


def test_copy_becomes_branch_with_inherited_history_and_independent_future(tmp_path):
    parent = tmp_path / "parent.pptx"
    child = tmp_path / "child-copy.pptx"
    fx.make_pptx(parent, [{"body": "parent v1 SHARED"}])
    mgr = _mgr()
    mgr.snapshot_now(str(parent))
    fx.make_pptx(parent, [{"body": "parent v2 BRANCHPOINT"}])
    mgr.snapshot_now(str(parent))
    parent_versions = mgr.list_versions(str(parent))
    branch_version_id = parent_versions[0]["version_id"]

    shutil.copy2(parent, child)
    assert mgr.snapshot_now(str(child)) is None
    child_doc_id = vault.doc_id_for(str(child))
    branch = store.get_branch(mgr._conn, child_doc_id)
    assert branch["parent_doc_id"] == vault.doc_id_for(str(parent))
    assert branch["branched_from_version_id"] == branch_version_id
    assert len(mgr.list_versions(str(child))) == 2

    fx.make_pptx(child, [{"body": "child v3 ONLY CHILD"}])
    assert mgr.snapshot_now(str(child))

    child_versions = mgr.list_versions(str(child))
    parent_versions_after_child_edit = mgr.list_versions(str(parent))
    assert len(child_versions) == 3
    assert len(parent_versions_after_child_edit) == 2
    assert child_versions[0]["doc_id"] == child_doc_id
    assert all(v["doc_id"] != child_doc_id for v in parent_versions_after_child_edit)


def test_copy_branch_registration_is_silent_for_users(tmp_path):
    parent = tmp_path / "parent.pptx"
    child = tmp_path / "child-copy.pptx"
    events = []
    fx.make_pptx(parent, [{"body": "parent v1 SILENT"}])
    mgr = VersionManager(on_snapshot=lambda path, version_id: events.append((path, version_id)))
    assert mgr.snapshot_now(str(parent))
    events.clear()

    shutil.copy2(parent, child)
    assert mgr.snapshot_now(str(child)) is None

    assert events == []
    assert store.get_branch(mgr._conn, vault.doc_id_for(str(child))) is not None
    assert store.list_versions(mgr._conn, vault.doc_id_for(str(child))) == []


def test_parent_edits_after_copy_do_not_leak_into_child_branch(tmp_path):
    parent = tmp_path / "parent.pptx"
    child = tmp_path / "child-copy.pptx"
    fx.make_pptx(parent, [{"body": "parent v1 BRANCH"}])
    mgr = _mgr()
    mgr.snapshot_now(str(parent))
    branch_version_id = mgr.list_versions(str(parent))[0]["version_id"]

    shutil.copy2(parent, child)
    mgr.snapshot_now(str(child))
    fx.make_pptx(parent, [{"body": "parent v2 AFTER CHILD BRANCH"}])
    mgr.snapshot_now(str(parent))

    child_version_ids = [v["version_id"] for v in mgr.list_versions(str(child))]
    parent_version_ids = [v["version_id"] for v in mgr.list_versions(str(parent))]
    assert child_version_ids == [branch_version_id]
    assert parent_version_ids[0] not in child_version_ids


def test_copy_from_old_parent_version_branches_from_matching_history_version(tmp_path):
    parent = tmp_path / "parent.pptx"
    child = tmp_path / "child-from-old.pptx"
    old_export = tmp_path / "old-export.pptx"
    fx.make_pptx(parent, [{"body": "parent v1 OLDHASH"}])
    mgr = _mgr()
    mgr.snapshot_now(str(parent))
    old_version_id = mgr.list_versions(str(parent))[0]["version_id"]
    fx.make_pptx(parent, [{"body": "parent v2 LATEST"}])
    mgr.snapshot_now(str(parent))

    assert mgr.export(str(parent), old_version_id, str(old_export))
    shutil.copy2(old_export, child)
    assert mgr.snapshot_now(str(child)) is None

    child_doc_id = vault.doc_id_for(str(child))
    branch = store.get_branch(mgr._conn, child_doc_id)
    assert branch["branched_from_version_id"] == old_version_id
    assert [v["version_id"] for v in mgr.list_versions(str(child))] == [old_version_id]


def test_restore_inherited_parent_version_targets_child_file(tmp_path):
    parent = tmp_path / "parent.pptx"
    child = tmp_path / "child-copy.pptx"
    fx.make_pptx(parent, [{"body": "parent v1 RESTOREME"}])
    mgr = _mgr()
    mgr.snapshot_now(str(parent))
    inherited_version_id = mgr.list_versions(str(parent))[0]["version_id"]

    shutil.copy2(parent, child)
    mgr.snapshot_now(str(child))
    fx.make_pptx(child, [{"body": "child changed"}])
    mgr.snapshot_now(str(child))

    assert mgr.restore_to(str(child), inherited_version_id)
    child_text = "".join(pg.raw_text for pg in parse_pptx(str(child)).pages)
    parent_text = "".join(pg.raw_text for pg in parse_pptx(str(parent)).pages)
    assert "RESTOREME" in child_text
    assert "RESTOREME" in parent_text
    assert child.exists() and parent.exists()
