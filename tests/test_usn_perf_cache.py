"""USN path reuse must reduce native calls without suppressing file events."""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from pptx_finder import usn_journal as usn
from pptx_finder.versioning import usn_feed as module


class Journal:
    def __init__(self):
        self.paths = {1: "C:/work", 2: "C:/work/file.pptx"}
        self.calls = Counter()

    def path_for(self, fid):
        self.calls[fid] += 1
        return self.paths.get(fid)


@pytest.fixture
def fixture(tmp_path):
    changed, dirty, events = [], [], []
    handler = SimpleNamespace(
        skip_directory=lambda path: False,
        skip_path=lambda path: "excluded" in path,
        is_content_path=lambda path: path.endswith(".pptx"),
        name_changed=changed.append,
        other_dir_changed=dirty.append,
        on_created=lambda event: events.append(("created", event.src_path)),
        on_modified=lambda event: events.append(("modified", event.src_path)),
        on_moved=lambda event: events.append(("moved", event.src_path, event.dest_path)),
        on_deleted=lambda event: events.append(("deleted", event.src_path)),
    )
    feed = module.UsnFeed(handler, [], state_path=str(tmp_path / "state.json"))
    journal = Journal()
    return feed, module._Volume(journal, "C:/"), journal, changed, dirty, events


def process(feed, volume, *, fid=2, reason=usn.USN_REASON_DATA_OVERWRITE, parent=1):
    feed._process_files(volume, {fid: [reason, parent, 0]})


@pytest.mark.parametrize("suffix", [".pptx", ".txt", ".xlsx", ".md"])
def test_repeated_data_changes_reuse_path_but_deliver_every_event(fixture, suffix):
    feed, volume, journal, changed, dirty, events = fixture
    journal.paths[2] = f"C:/work/file{suffix}"
    for _ in range(30):
        process(feed, volume)
    assert journal.calls[2] == 1
    assert changed == [journal.paths[2]] * 30
    if suffix == ".pptx":
        assert events == [("modified", journal.paths[2])] * 30
    else:
        assert dirty == ["C:/work"] * 30


def test_parent_change_forces_native_lookup(fixture):
    feed, volume, journal, changed, _dirty, _events = fixture
    process(feed, volume)
    journal.paths[3] = "C:/elsewhere"
    journal.paths[2] = "C:/elsewhere/file.pptx"
    process(feed, volume, parent=3)
    assert journal.calls[2] == 2
    assert changed[-1] == journal.paths[2]


@pytest.mark.parametrize("reason", [usn.USN_REASON_FILE_CREATE,
    usn.USN_REASON_RENAME_NEW_NAME,
    usn.USN_REASON_DATA_EXTEND | usn.USN_REASON_RENAME_NEW_NAME,
    usn.USN_REASON_DATA_EXTEND | usn.USN_REASON_FILE_CREATE | 0x80000000])
def test_path_changing_reason_never_reuses_cached_path(fixture, reason):
    feed, volume, journal, changed, _dirty, events = fixture
    process(feed, volume)
    journal.paths[2] = "C:/work/renamed.pptx"
    process(feed, volume, reason=reason)
    assert journal.calls[2] == 2
    assert changed[-1] == journal.paths[2]
    process(feed, volume)
    assert journal.calls[2] == 2
    assert changed[-1] == journal.paths[2]
    if reason & usn.USN_REASON_RENAME_NEW_NAME:
        assert ("moved", "C:/work/file.pptx", journal.paths[2]) in events


def test_delete_invalidates_file_path(fixture):
    feed, volume, journal, _changed, _dirty, _events = fixture
    process(feed, volume)
    process(feed, volume, reason=usn.USN_REASON_FILE_DELETE)
    assert 2 not in volume.files
    assert 2 not in volume.paths
    process(feed, volume)
    assert journal.calls[2] == 2


def test_excluded_rename_invalidates_previously_cached_path(fixture):
    feed, volume, journal, changed, _dirty, _events = fixture
    process(feed, volume)
    journal.paths[2] = "C:/excluded/file.pptx"
    process(feed, volume, reason=usn.USN_REASON_RENAME_NEW_NAME)
    assert 2 not in volume.files
    assert 2 not in volume.paths
    process(feed, volume)
    assert journal.calls[2] == 3
    assert changed == ["C:/work/file.pptx"]


def test_file_capacity_evicts_one_entry_instead_of_entire_cache(fixture, monkeypatch):
    feed, volume, journal, _changed, _dirty, _events = fixture
    monkeypatch.setattr(module, "FILE_CACHE_MAX", 3)
    volume.files.update({10: "C:/work/a.pptx", 11: "C:/work/b.pptx", 12: "C:/work/c.pptx"})
    process(feed, volume)
    assert set(volume.files) == {11, 12, 2}
    process(feed, volume, reason=usn.USN_REASON_FILE_CREATE)
    assert set(volume.files) == {11, 12, 2}
    assert journal.calls[2] == 2


def test_directory_capacity_evicts_one_entry_instead_of_entire_cache(fixture, monkeypatch):
    feed, volume, journal, _changed, _dirty, _events = fixture
    monkeypatch.setattr(module, "DIR_CACHE_MAX", 3)
    volume.dirs.update({10: "C:/a", 11: "C:/b", 12: None})
    assert feed._dir_path(volume, 1) == "C:/work"
    assert set(volume.dirs) == {11, 12, 1}
    assert feed._dir_path(volume, 11) == "C:/b"
    assert journal.calls[11] == 0


def test_directory_rename_still_invalidates_all_file_paths(fixture):
    feed, volume, journal, _changed, _dirty, _events = fixture
    process(feed, volume)
    volume.pending_dir_moves[3] = (None, None)
    journal.paths[3] = None
    record = (3, 1, 0, 0, usn.USN_REASON_RENAME_NEW_NAME, usn.FILE_ATTRIBUTE_DIRECTORY)
    feed._process_dir_events(volume, [record])
    assert volume.files == {}
    assert volume.paths == {}
    process(feed, volume)
    assert journal.calls[2] == 2


def test_journal_gap_invalidates_paths_renamed_during_missing_history(fixture):
    feed, volume, journal, changed, _dirty, _events = fixture
    process(feed, volume)
    journal.paths[2] = "C:/work/renamed-during-gap.pptx"
    journal.query = lambda: SimpleNamespace(journal_id=3, next_usn=100)
    feed._recover_gap(volume, "history overwritten")
    assert volume.files == {}
    assert volume.paths == {}
    process(feed, volume)
    assert changed[-1] == journal.paths[2]
    assert journal.calls[2] == 2


@pytest.mark.parametrize("reason", [usn.USN_REASON_DATA_OVERWRITE,
    usn.USN_REASON_DATA_EXTEND, usn.USN_REASON_DATA_TRUNCATION,
    usn.USN_REASON_DATA_EXTEND | usn.USN_REASON_DATA_TRUNCATION,
    usn.USN_REASON_DATA_EXTEND | 0x80000000,
    usn.USN_REASON_DATA_OVERWRITE | usn.USN_REASON_DATA_TRUNCATION | 0x80000000])
def test_successful_hot_update_does_not_dirty_entire_directory(fixture, reason):
    feed, volume, journal, changed, dirty, _events = fixture
    journal.paths[2] = "C:/work/log.txt"

    def note(path):
        changed.append(path)
        return True

    feed._handler.name_changed = note
    for _ in range(30):
        process(feed, volume, reason=reason)
    assert changed == [journal.paths[2]] * 30
    assert dirty == []
    assert journal.calls[2] == 1


@pytest.mark.parametrize("result", [False, None, 1])
def test_unsuccessful_or_unconfirmed_hot_update_keeps_directory_fallback(fixture, result):
    feed, volume, journal, _changed, dirty, _events = fixture
    journal.paths[2] = "C:/work/log.txt"
    feed._handler.name_changed = lambda path: result
    process(feed, volume)
    assert dirty == ["C:/work"]


def test_missing_hot_callback_keeps_directory_fallback(fixture):
    feed, volume, journal, _changed, dirty, _events = fixture
    journal.paths[2] = "C:/work/log.txt"
    del feed._handler.name_changed
    process(feed, volume)
    assert dirty == ["C:/work"]


@pytest.mark.parametrize("reason", [usn.USN_REASON_FILE_CREATE,
    usn.USN_REASON_FILE_DELETE, usn.USN_REASON_RENAME_NEW_NAME,
    usn.USN_REASON_DATA_EXTEND | usn.USN_REASON_FILE_CREATE,
    usn.USN_REASON_DATA_EXTEND | usn.USN_REASON_FILE_CREATE | 0x80000000])
def test_structural_events_still_dirty_directory_when_hot_update_succeeds(fixture, reason):
    feed, volume, journal, _changed, dirty, _events = fixture
    journal.paths[2] = "C:/work/log.txt"
    feed._handler.name_changed = lambda path: True
    process(feed, volume, reason=reason)
    assert dirty == ["C:/work"]


@pytest.mark.parametrize("callback_kind", ["success", "missing", "raises"])
def test_real_handler_reports_whether_hot_callback_accepted_event(callback_kind):
    from pptx_finder.versioning.watcher import _Handler

    accepted = []

    def failing(path):
        raise RuntimeError("test hot layer failure")

    callback = {"success": accepted.append, "missing": None, "raises": failing}[callback_kind]
    handler = _Handler(lambda path: None, roots=[], on_name_changed=callback)
    try:
        assert handler.name_changed("C:/work/log.txt") is (callback_kind == "success")
        assert accepted == (["C:/work/log.txt"] if callback_kind == "success" else [])
    finally:
        handler.stop()


def test_repeated_writes_keep_exact_hot_metadata_without_directory_reconcile(
        fixture, tmp_path, monkeypatch):
    from pptx_finder import hot_names, namestore, search
    from pptx_finder.versioning.watcher import _Handler

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    feed, volume, journal, _changed, dirty, _events = fixture
    target = tmp_path / "searchable-log.txt"
    journal.paths[1] = str(tmp_path)
    journal.paths[2] = str(target)
    hot = hot_names.HotNames()
    handler = _Handler(lambda path: None, roots=[str(tmp_path)], on_name_changed=hot.note)
    feed._handler.name_changed = handler.name_changed
    try:
        for size in (1, 57, 301):
            target.write_bytes(b"x" * size)
            process(feed, volume)
            assert hot_names.write_hot_layer(hot.snapshot()) == 1
            store = namestore.open_store(namestore.HOT)
            assert store is not None
            try:
                path, _name, stored_size, stored_mtime, _is_dir = store.entry(0)
                assert path == str(target)
                assert stored_size == target.stat().st_size == size
                assert int(stored_mtime) == int(target.stat().st_mtime)
                results = search.search_names([store], "searchable-log")
                assert [result.name for result in results] == [target.name]
            finally:
                store.close()
        assert len(hot) == 1
        assert dirty == []
        assert journal.calls[2] == 1
    finally:
        handler.stop()


def test_noncontent_churn_cannot_evict_content_rename_identity(fixture, monkeypatch):
    feed, volume, journal, _changed, _dirty, events = fixture
    monkeypatch.setattr(module, "PATH_CACHE_MAX", 3)
    process(feed, volume)
    for fid in range(10, 20):
        journal.paths[fid] = f"C:/work/log{fid}.txt"
        process(feed, volume, fid=fid)
    assert volume.files == {2: "C:/work/file.pptx"}
    assert len(volume.paths) == 3
    assert 2 not in volume.paths
    journal.paths[2] = "C:/work/new-name.pptx"
    process(feed, volume, reason=usn.USN_REASON_RENAME_NEW_NAME)
    assert ("moved", "C:/work/file.pptx", journal.paths[2]) in events


def test_unknown_reason_modifier_never_enables_data_fast_path(fixture):
    feed, volume, journal, _changed, dirty, _events = fixture
    journal.paths[2] = "C:/work/log.txt"
    feed._handler.name_changed = lambda path: True
    reason = usn.USN_REASON_DATA_EXTEND | 0x10000000
    process(feed, volume, reason=reason)
    process(feed, volume, reason=reason)
    assert journal.calls[2] == 2
    assert dirty == ["C:/work", "C:/work"]


def test_failed_native_lookup_preserves_content_identity_for_later_rename(fixture):
    feed, volume, journal, _changed, _dirty, events = fixture
    process(feed, volume)
    journal.paths[2] = None
    process(feed, volume, reason=usn.USN_REASON_RENAME_NEW_NAME)
    assert volume.files[2] == "C:/work/file.pptx"
    assert 2 not in volume.paths
    journal.paths[2] = "C:/work/renamed.pptx"
    process(feed, volume, reason=usn.USN_REASON_RENAME_NEW_NAME)
    assert ("moved", "C:/work/file.pptx", journal.paths[2]) in events
    assert volume.files[2] == journal.paths[2]


def test_confirmed_noncontent_destination_drops_content_identity(fixture):
    feed, volume, journal, _changed, _dirty, _events = fixture
    process(feed, volume)
    journal.paths[2] = "C:/work/renamed.txt"
    process(feed, volume, reason=usn.USN_REASON_RENAME_NEW_NAME)
    assert 2 not in volume.files
    assert volume.paths[2] == journal.paths[2]
