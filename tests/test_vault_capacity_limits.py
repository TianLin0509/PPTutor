"""Hard admission limits use isolated vaults, never the user's history."""
import os
import zipfile

import fixtures_gen as fx
import pytest

from pptx_finder import config
from pptx_finder.versioning import store, vault, capacity
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('PPTX_FINDER_DATA_DIR', str(tmp_path/'data'))


def deck(path, text, media_bytes=0):
    fx.make_pptx(path, [{'body': text}])
    if media_bytes:
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('ppt/media/capacity-test.bin', os.urandom(media_bytes))
    return str(path)


def test_oversized_new_snapshot_does_not_consume_space_or_replace_restore_point(tmp_path):
    manager=VersionManager()
    try:
        path=deck(tmp_path/'demo.pptx', 'old recoverable content')
        old=manager.snapshot_now(path)
        original_hash=store.get_version(manager._conn, old)['content_hash']
        config.set_vault_max_mb(1)
        deck(tmp_path/'demo.pptx', 'new oversized content', 2*1024*1024)
        with pytest.raises(OSError, match='容量'):
            manager.snapshot_now(path)
        versions=list(manager._conn.execute('SELECT version_id FROM versions'))
        assert [r[0] for r in versions]==[old]
        assert vault.vault_size_bytes() < 1024*1024
        assert store.get_doc_by_path(manager._conn,path)['latest_version_id']==old
        output=tmp_path/'recovered.pptx'
        assert vault.rebuild_to(store.get_version(manager._conn,old)['doc_id'],old,str(output))
        assert vault.file_hash(str(output))==original_hash
    finally:manager.stop()


def test_usage_counts_database_and_migration_backups_but_lists_tmp_separately(tmp_path):
    root=vault.vault_dir()
    (root/'versions.db').write_bytes(b'db'*101)
    (root/'backups').mkdir()
    (root/'backups'/'saved.db').write_bytes(b'b'*303)
    (root/'_tmp').mkdir()
    (root/'_tmp'/'active.pptx').write_bytes(b't'*404)
    state=capacity.usage()
    assert state['saved_bytes']==505
    assert state['temporary_bytes']==404
    assert state['total_bytes']==909


def test_unreadable_usage_is_not_treated_as_empty(monkeypatch):
    root=vault.vault_dir()
    def unreadable(*args, **kwargs):raise PermissionError('quota-test-denied')
    monkeypatch.setattr(capacity.os,'scandir',unreadable)
    with pytest.raises(PermissionError,match='quota-test-denied'):
        capacity.usage()


def test_configuration_applies_immediately_and_preserves_latest_recovery_point(tmp_path):
    manager=VersionManager()
    try:
        config.set_vault_max_mb(0)
        path=deck(tmp_path/'demo.pptx','old-large',2*1024*1024)
        older=manager.snapshot_now(path)
        newest=manager.snapshot_now(deck(tmp_path/'demo.pptx','latest-small'))
        state=manager.configure_storage('',1)
        assert config.get_vault_max_mb()==1
        assert state['saved_bytes']<1024*1024
        assert not state['blocked']
        assert store.get_version(manager._conn,older) is None
        assert store.get_version(manager._conn,newest) is not None
        output=tmp_path/'latest-export.pptx'
        assert manager.export(path,newest,str(output))
        assert vault.file_hash(str(output))==vault.file_hash(path)
    finally:manager.stop()


def test_protected_floor_over_limit_is_explicit_and_does_not_delete_history(tmp_path):
    manager=VersionManager()
    try:
        path=deck(tmp_path/'demo.pptx','only-baseline',2*1024*1024)
        old=manager.snapshot_now(path)
        state=manager.configure_storage('',1)
        assert state['blocked'] and state['saved_bytes']>state['max_bytes']
        assert '暂停' in state['last_error']
        assert store.get_version(manager._conn,old)
        assert manager.export(path,old,str(tmp_path/'export.pptx'))
    finally:manager.stop()


def test_quota_uses_physical_growth_instead_of_large_shared_source_size(tmp_path):
    manager=VersionManager()
    try:
        config.set_vault_max_mb(3)
        first=deck(tmp_path/'first.pptx','shared-media',2*1024*1024)
        assert manager.snapshot_now(first)
        second=deck(tmp_path/'second.pptx','different-text-shared-media')
        with zipfile.ZipFile(first) as original:
            shared=original.read('ppt/media/capacity-test.bin')
        with zipfile.ZipFile(second,'a') as target:
            target.writestr('ppt/media/capacity-test.bin',shared)
        assert manager.snapshot_now(second)
        assert manager.storage_status()['saved_bytes']<3*1024*1024
        assert len(list(manager._conn.execute('SELECT version_id FROM versions')))==2
    finally:manager.stop()


def test_switch_to_new_location_uses_new_limit_and_keeps_previous_library(tmp_path):
    manager=VersionManager()
    try:
        old=manager.snapshot_now(deck(tmp_path/'old.pptx','previous-library',2*1024*1024))
        source=vault.vault_dir()
        dest=tmp_path/'different-vault'
        state=manager.configure_storage(str(dest),1,migrate=False)
        assert not state['blocked'] and state['directory']==str(dest)
        assert store.get_version(manager._conn,old) is None
        assert (source/'versions.db').is_file()
        assert manager.snapshot_now(deck(tmp_path/'new.pptx','new-library'))
        manager.configure_storage(str(source),3,migrate=False)
        assert store.get_version(manager._conn,old) is not None
    finally:manager.stop()


def test_migration_applies_limit_in_new_location_and_preserves_content_hash(tmp_path):
    manager=VersionManager()
    try:
        path=deck(tmp_path/'demo.pptx','migrate-me')
        old=manager.snapshot_now(path)
        expected=vault.file_hash(path)
        dest=tmp_path/'moved-vault'
        state=manager.configure_storage(str(dest),1)
        assert config.get_version_vault_dir()==str(dest)
        assert config.get_vault_max_mb()==1
        assert state['directory']==str(dest)
        output=tmp_path/'recovered.pptx'
        assert manager.export(path,old,str(output))
        assert vault.file_hash(str(output))==expected
    finally:manager.stop()


def test_full_vault_does_not_overwrite_current_file_without_pre_restore_copy(tmp_path,monkeypatch):
    manager=VersionManager()
    try:
        path=deck(tmp_path/'demo.pptx','recoverable-old',2*1024*1024)
        old=manager.snapshot_now(path)
        deck(tmp_path/'demo.pptx','current-unsaved')
        before=(tmp_path/'demo.pptx').read_bytes()
        config.set_vault_max_mb(1)
        monkeypatch.setattr('pptx_finder.versioning.manager.actions.presentation_open_state',lambda *a:False)
        assert not manager.restore_to(path,old)
        assert '未覆盖' in manager.last_restore_error()
        assert (tmp_path/'demo.pptx').read_bytes()==before
    finally:manager.stop()


def test_rejected_capture_never_emits_saved_notification(tmp_path):
    notifications=[]
    manager=VersionManager(on_snapshot=lambda *a:notifications.append(a))
    try:
        config.set_vault_max_mb(1)
        with pytest.raises(vault.VaultCapacityError):
            manager.snapshot_now(deck(tmp_path/'huge.pptx','oversize',2*1024*1024))
        assert notifications==[]
    finally:manager.stop()


def test_other_manager_commits_invalidate_cached_capacity_before_admission(tmp_path):
    config.set_vault_max_mb(3)
    first,second=VersionManager(),VersionManager()
    try:
        first.storage_status()
        second.storage_status()
        assert first.snapshot_now(deck(tmp_path/'a.pptx','first',2*1024*1024))
        with pytest.raises(vault.VaultCapacityError):
            second.snapshot_now(deck(tmp_path/'b.pptx','second',2*1024*1024))
        assert first.storage_status()['saved_bytes']<=3*1024*1024
        assert len(list(first._conn.execute('SELECT version_id FROM versions')))==1
    finally:
        first.stop()
        second.stop()


def test_limit_changed_during_unlimited_capture_still_rejects_and_reclaims_candidate(tmp_path,monkeypatch):
    config.set_vault_max_mb(0)
    manager=VersionManager()
    original=vault.snapshot
    def tightened(*args,**kwargs):
        config.set_vault_max_mb(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(vault,'snapshot',tightened)
    try:
        with pytest.raises(vault.VaultCapacityError):
            manager.snapshot_now(deck(tmp_path/'new.pptx','oversize',2*1024*1024))
        assert list(manager._conn.execute('SELECT version_id FROM versions'))==[]
        assert manager.storage_status()['saved_bytes']<=1024*1024
    finally:manager.stop()


def test_post_write_accounting_failure_discards_unaccepted_candidate_and_keeps_old(tmp_path,monkeypatch):
    manager=VersionManager()
    try:
        path=deck(tmp_path/'demo.pptx','old-safe')
        old=manager.snapshot_now(path)
        config.set_vault_max_mb(1)
        manager._storage_data_bytes=None
        original=manager._measure_storage
        calls=[]
        def fail_once():
            calls.append(True)
            if len(calls)==2:raise PermissionError('post-write-accounting-denied')
            return original()
        monkeypatch.setattr(manager,'_measure_storage',fail_once)
        with pytest.raises(vault.VaultCapacityError,match='post-write-accounting-denied'):
            manager.snapshot_now(deck(tmp_path/'demo.pptx','new-too-big',2*1024*1024))
        assert [row[0] for row in manager._conn.execute('SELECT version_id FROM versions')]==[old]
        assert store.get_doc_by_path(manager._conn,path)['latest_version_id']==old
        assert manager.storage_status()['saved_bytes']<=1024*1024
        assert manager.export(path,old,str(tmp_path/'recovered.pptx'))
    finally:manager.stop()


def test_database_free_pages_are_reclaimed_before_evicting_recoverable_history(tmp_path):
    manager=VersionManager()
    try:
        versions=[]
        for index in range(3):
            versions.append(manager.snapshot_now(deck(tmp_path/'demo.pptx',f'version-{index}',1024*1024)))
        manager._conn.execute('CREATE TABLE capacity_test_big (payload BLOB)')
        manager._conn.execute('INSERT INTO capacity_test_big VALUES (zeroblob(?))',(80*1024*1024,))
        manager._conn.commit()
        manager._conn.execute('DELETE FROM capacity_test_big')
        manager._conn.commit()
        before=manager.storage_status()['saved_bytes']
        assert before>82*1024*1024
        state=manager.configure_storage('',82)
        assert state['saved_bytes']<82*1024*1024
        assert all(store.get_version(manager._conn,version) is not None for version in versions)
        assert state['evicted_versions']==0
    finally:manager.stop()


def test_failed_database_hygiene_does_not_sacrifice_healthy_history(tmp_path,monkeypatch):
    manager=VersionManager()
    try:
        versions=[manager.snapshot_now(deck(tmp_path/'demo.pptx',f'old-{index}',1024*1024))
                  for index in range(3)]
        monkeypatch.setattr(vault,'maintain_db',lambda *a,**kw:{'error':'database busy'})
        state=manager.set_storage_limit(1)
        assert state['blocked'] and 'database busy' in state['last_error']
        assert state['evicted_versions']==0
        assert all(store.get_version(manager._conn,version) for version in versions)
    finally:manager.stop()


def test_new_version_can_reclaim_eligible_old_history_without_losing_previous_latest(tmp_path):
    config.set_vault_max_mb(0)
    manager=VersionManager()
    try:
        path=str(tmp_path/'rotation.pptx')
        older=[]
        for index in range(3):
            older.append(manager.snapshot_now(deck(tmp_path/'rotation.pptx',f'old-{index}',512*1024)))
        config.set_vault_max_mb(2)
        new=manager.snapshot_now(deck(tmp_path/'rotation.pptx','new',900*1024))
        assert new
        assert store.get_version(manager._conn,older[-1]) is not None
        assert store.get_version(manager._conn,new) is not None
        assert store.get_version(manager._conn,older[0]) is None
        assert manager.storage_status()['saved_bytes']<=2*1024*1024
    finally:manager.stop()


def test_existing_overflow_blocks_admission_before_creating_temporary_copy(tmp_path, monkeypatch):
    manager=VersionManager()
    try:
        old=manager.snapshot_now(deck(tmp_path/'old.pptx', 'protected baseline', 2*1024*1024))
        config.set_vault_max_mb(1)
        path=deck(tmp_path/'new.pptx', 'new file')
        def no_copy(*args,**kwargs):
            pytest.fail('A full vault must reject before making its stable temporary copy')
        monkeypatch.setattr(vault,'stable_snapshot_source',no_copy)
        with pytest.raises(OSError, match='容量'):
            manager.snapshot_now(path)
        assert store.get_version(manager._conn,old) is not None
    finally:manager.stop()
