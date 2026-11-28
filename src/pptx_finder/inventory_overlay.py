"""Cheap equality check before rewriting the entire live filename overlay.

Only direct children of dirty directories and explicitly hot paths can change.
Compare that small slice first; a real delta still uses the existing rebuild.
"""
from __future__ import annotations

import os

from . import hot_names, namestore


def _metadata(path, size, mtime, is_dir):
    # Match NameStoreBuilder's on-disk representation without changing images,
    # search precision, path spelling, or the full-file index population.
    return (path, 0 if is_dir else max(0, min(int(size), namestore.SIZE_CAP)),
            max(0, min(int(mtime), 0xFFFFFFFF)), bool(is_dir))


def unchanged_overlay(dirs, hot, *, baseline, snapshot_at, full_dir_max):
    """Return no-op totals, or None when the normal rebuild remains necessary."""
    previous = namestore.open_store(namestore.OVERLAY)
    if previous is None:
        return None
    dirty_keys = {os.path.normcase(os.path.abspath(str(path))) for path in dirs}
    hot_keys = {key for key, _path, _seq in hot}
    related_dirs = set(dirs)
    related_dirs.update(os.path.dirname(path) for _key, path, _seq in hot)
    try:
        old = previous.entries_for_directories(related_dirs)
        prior = {
            key: _metadata(row[0], row[2], row[3], row[4])
            for key, row in old.items()
            if os.path.normcase(os.path.dirname(row[0])) in dirty_keys or key in hot_keys
        }
        expected = {}
        folded = []
        for key, path, seq in hot:
            try:
                item = hot_names.stat_entry(path)
            except hot_names.StatUnavailable:
                # Keep the original retry and replacement behavior on unknown
                # state. Never acknowledge a hot entry we could not inspect.
                return None
            if item is not None:
                p, size, mtime, is_dir = item
                expected[key] = _metadata(p, size, mtime, is_dir)
            folded.append((key, seq))
        checked = 0
        for directory in dirs:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                return None
            checked += 1
            for entry in entries:
                key = os.path.normcase(entry.path)
                if key in expected or entry.name.startswith("~$"):
                    continue
                try:
                    stat = entry.stat()
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    return None
                if (snapshot_at and len(entries) > full_dir_max
                        and max(stat.st_mtime, stat.st_ctime) < snapshot_at - 120):
                    continue
                size = 0 if is_dir else int(stat.st_size)
                base = baseline.get(key)
                if base is not None and (int(base[2]), int(base[3]), bool(base[4])) == (
                        size, int(stat.st_mtime), is_dir):
                    continue
                expected[key] = _metadata(entry.path, size, stat.st_mtime, is_dir)
        if prior != expected:
            return None
        return {"added": 0, "dirs": checked, "entries": previous.count,
                "overflowed": False, "hot_folded": folded, "unchanged": True}
    finally:
        previous.close()
