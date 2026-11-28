"""No-op directory churn must not rebuild a large unchanged name overlay."""
import os
from pathlib import Path
import time

from pptx_finder import hot_names, namestore
from pptx_finder.ui.main_window import MainWindow


def seed(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    # Same branch as a multi-million-name full index; no global scan is needed.
    monkeypatch.setattr(namestore, "peek_header", lambda kind: (2_460_000, 1_700_000_000))
    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / "log.bin"
    target.write_bytes(b"old")
    builder = namestore.NameStoreBuilder()
    builder.add(str(target), target.stat().st_size, target.stat().st_mtime)
    for i in range(300):
        builder.add(str(tmp_path / "unrelated" / f"cached-{i}.bin"), i, 1_700_000_000)
    builder.write(kind=namestore.OVERLAY)
    builder.close()
    return docs, target, namestore.current_data_path(namestore.OVERLAY)


def test_unchanged_directory_does_not_build_or_rewrite_overlay(tmp_path, monkeypatch):
    docs, target, before = seed(tmp_path, monkeypatch)
    monkeypatch.setattr(namestore, "NameStoreBuilder", lambda: (_ for _ in ()).throw(
        AssertionError("no-op churn must not construct a full overlay builder")))
    result = MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    assert result.get("unchanged") is True
    assert result["entries"] == 301
    assert result["added"] == 0
    assert not result.get("error")
    assert namestore.current_data_path(namestore.OVERLAY) == before


def test_unchanged_hot_metadata_can_fold_without_rewriting(tmp_path, monkeypatch):
    docs, target, before = seed(tmp_path, monkeypatch)
    hot = ((os.path.normcase(str(target)), str(target), 17),)
    result = MainWindow._reconcile_inventory_dirs_sync((), hot)
    assert result.get("unchanged") is True
    assert result["hot_folded"] == [(hot[0][0], 17)]
    assert namestore.current_data_path(namestore.OVERLAY) == before


def test_changed_and_deleted_files_still_replace_overlay_metadata(tmp_path, monkeypatch):
    docs, target, _ = seed(tmp_path, monkeypatch)
    target.write_bytes(b"changed contents")
    result = MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    assert not result.get("unchanged") and not result.get("error")
    with namestore.open_store(namestore.OVERLAY) as store:
        found = store.entries_for_directories((str(docs),))
        assert found[os.path.normcase(str(target))][2] == len(b"changed contents")
        assert store.count == 301
    target.unlink()
    result = MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    assert not result.get("unchanged") and not result.get("error")
    with namestore.open_store(namestore.OVERLAY) as store:
        assert not store.entries_for_directories((str(docs),))
        assert store.count == 300


def test_unavailable_hot_stat_keeps_retry_and_existing_rebuild_semantics(tmp_path, monkeypatch):
    docs, target, _ = seed(tmp_path, monkeypatch)
    hot = ((os.path.normcase(str(target)), str(target), 19),)
    monkeypatch.setattr(hot_names, "stat_entry", lambda path: (_ for _ in ()).throw(
        hot_names.StatUnavailable("locked")))
    result = MainWindow._reconcile_inventory_dirs_sync((), hot)
    assert not result.get("unchanged")
    assert not result.get("error") and result["hot_folded"] == []


def test_overlay_builder_is_closed_after_write_failure(tmp_path, monkeypatch):
    docs, target, _ = seed(tmp_path, monkeypatch)
    target.write_bytes(b"new metadata")
    closed = []
    real_close = namestore.NameStoreBuilder.close
    def close(self):
        closed.append(self)
        real_close(self)
    monkeypatch.setattr(namestore.NameStoreBuilder, "close", close)
    monkeypatch.setattr(namestore.NameStoreBuilder, "write", lambda *a, **kw: (_ for _ in ()).throw(
        OSError("simulated write failure")))
    result = MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    assert result.get("error")
    assert len(closed) == 1
