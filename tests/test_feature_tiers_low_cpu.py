from __future__ import annotations

import threading
import time
import sys
from types import SimpleNamespace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from pptx_finder import config, db, indexer, renderer, search
from pptx_finder import scanner
from pptx_finder import app as app_mod
from pptx_finder.ui import main_window as main_window_mod
from pptx_finder.ui.index_worker import IndexWorker
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder.versioning.manager import VersionManager
from pptx_finder.versioning.watcher import _Handler

import fixtures_gen as fx


class _StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, req_id, path, page_no, cache_key=None):
        self.rendered.emit(req_id, "")


def test_advanced_features_default_off_and_basic_exts(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))

    assert config.get_version_management_enabled() is False
    assert config.get_document_search_enabled() is False
    assert config.get_smart_grouping_enabled() is False
    assert config.enabled_index_exts() == (".pptx", ".ppt")


def test_settings_feature_toggles_persist_and_notify(monkeypatch, qtbot, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    manager = VersionManager()
    calls = []
    dlg = SettingsDialog(
        manager,
        on_feature_change=lambda key, enabled: calls.append((key, enabled)),
    )
    qtbot.addWidget(dlg)

    dlg.version_feature.setChecked(True)
    dlg.document_feature.setChecked(True)
    dlg.grouping_feature.setChecked(True)

    assert calls == [
        ("version_management", True),
        ("document_search", True),
        ("smart_grouping", True),
    ]
    assert config.get_version_management_enabled() is True
    assert config.get_document_search_enabled() is True
    assert config.get_smart_grouping_enabled() is True
    assert dlg.retention.isEnabled()
    manager.stop()


def test_settings_runtime_rollback_updates_checkbox_without_retriggering_toggle(
    monkeypatch, qtbot, tmp_path
):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    config.set_version_management_enabled(True)
    calls = []
    manager = VersionManager()
    dlg = SettingsDialog(
        manager,
        on_feature_change=lambda key, enabled: calls.append((key, enabled)),
    )
    qtbot.addWidget(dlg)
    assert dlg.version_feature.isChecked()
    config.set_version_management_enabled(False)

    dlg.apply_runtime_feature_state("version_management", False)

    assert not dlg.version_feature.isChecked()
    assert not dlg.retention.isEnabled()
    assert calls == []
    dlg.close()
    qtbot.waitUntil(
        lambda: not any(task.isRunning() for task in dlg._diag_tasks),
        timeout=3000,
    )
    manager.stop()


def test_basic_scan_indexes_only_ppt_without_extra_hash_read(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "deck.pptx", [{"body": "基础搜索"}])
    (docs / "legacy.ppt").write_bytes(b"legacy")
    (docs / "notes.docx").write_bytes(b"not parsed")
    (docs / "manual.pdf").write_bytes(b"not parsed")
    monkeypatch.setattr(
        indexer,
        "_file_sha256",
        lambda _path: (_ for _ in ()).throw(AssertionError("basic mode must not hash whole files")),
    )

    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    summary = indexer.update_index(
        conn,
        [str(docs)],
        workers=1,
        supported_exts=(".pptx", ".ppt"),
        compute_content_hash=False,
    )

    rows = conn.execute("SELECT ext, content_hash FROM files ORDER BY ext").fetchall()
    assert [row["ext"] for row in rows] == [".ppt", ".pptx"]
    assert not str(rows[1]["content_hash"]).startswith("sha256:")
    assert summary["scanned"] == 2


def test_enabling_smart_grouping_backfills_exact_hash(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    deck = docs / "deck.pptx"
    fx.make_pptx(deck, [{"body": "需要归组"}])
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)

    indexer.update_index(
        conn,
        [str(docs)],
        workers=1,
        supported_exts=(".pptx", ".ppt"),
        compute_content_hash=False,
    )
    before = db.get_file_by_path(conn, str(deck))["content_hash"]
    assert not str(before).startswith("sha256:")

    summary = indexer.update_index(
        conn,
        [str(docs)],
        workers=1,
        supported_exts=(".pptx", ".ppt"),
        compute_content_hash=True,
    )
    after = db.get_file_by_path(conn, str(deck))["content_hash"]
    assert str(after).startswith("sha256:")
    assert summary["indexed"] == 1


def test_scan_does_not_delete_still_existing_file_when_directory_walk_misses_it(tmp_path):
    """Permission/transient walk gaps must not turn valid indexed decks into ghosts."""
    deck = tmp_path / "restricted" / "still-here.pptx"
    deck.parent.mkdir()
    deck.write_bytes(b"pptx placeholder")
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    db.upsert_file(
        conn,
        path=str(deck),
        name=deck.name,
        ext=".pptx",
        size=deck.stat().st_size,
        mtime=deck.stat().st_mtime,
        content_hash="stat:test",
        page_count=0,
        status="ok",
        error="",
        indexed_at=time.time(),
    )
    conn.commit()

    summary = indexer.update_index(
        conn,
        [str(tmp_path)],
        workers=1,
        scan_iter=[],  # models os.walk skipping an inaccessible subtree
        supported_exts=(".pptx", ".ppt"),
        compute_content_hash=False,
    )

    assert db.get_file_by_path(conn, str(deck)) is not None
    assert summary["deleted"] == 0


def test_disk_walk_reports_heartbeat_even_when_no_ppt_exists(tmp_path):
    empty_root = tmp_path / "empty"
    (empty_root / "nested").mkdir(parents=True)
    heartbeats = []

    found = list(
        scanner.iter_ppt_files(
            [str(empty_root)],
            supported_exts=(".pptx", ".ppt"),
            scan_progress_cb=lambda count, current: heartbeats.append((count, current)),
        )
    )

    assert found == []
    assert heartbeats
    assert heartbeats[-1][0] >= 1


def test_search_can_skip_old_similarity_group_map(monkeypatch, tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    fid = db.upsert_file(
        conn,
        path=str(tmp_path / "roadmap.pptx"),
        name="roadmap.pptx",
        ext=".pptx",
        size=1,
        mtime=time.time(),
        content_hash="stat:1",
        page_count=1,
        status="ok",
        error="",
        indexed_at=time.time(),
    )
    db.replace_pages(conn, fid, [(1, "产品路线图", "产 品 路 线 图")])
    conn.commit()
    monkeypatch.setattr(
        "pptx_finder.cluster.group_map",
        lambda _conn: (_ for _ in ()).throw(AssertionError("group map must stay cold")),
    )

    results = search.search(conn, "路线图", group_similar=False)

    assert [r.name for r in results] == ["roadmap.pptx"]
    assert results[0].group_id is None


def test_main_window_defaults_to_ppt_only(monkeypatch, qtbot, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=_StubRender(), do_index=False)
    qtbot.addWidget(win)

    assert [win.type_filter.itemText(i) for i in range(win.type_filter.count())] == ["PPT"]
    assert win._search_exts() == (".pptx", ".ppt")
    assert win._enabled_index_exts() == (".pptx", ".ppt")


def test_app_indexing_is_two_worker_low_priority_and_interaction_yields(
    monkeypatch, qtbot, tmp_path,
):
    created = []

    class _FakeWorker(QObject):
        progress = Signal(int, int, str)
        finished_index = Signal(dict)
        finished = Signal()

        def __init__(self, db_path, roots, workers=None, parent=None, **kwargs):
            super().__init__(parent)
            self.db_path = db_path
            self.roots = roots
            self.workers = workers
            self.kwargs = kwargs
            self.started = False
            self.activity = 0
            created.append(self)

        def isRunning(self):
            return False

        def start(self):
            self.started = True

        def stop(self):
            pass

        def wait(self, _timeout=0):
            return True

        def note_user_activity(self):
            self.activity += 1

    monkeypatch.setattr(main_window_mod, "IndexWorker", _FakeWorker)
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=_StubRender(), do_index=False)
    qtbot.addWidget(win)

    assert win._start_indexing([str(tmp_path)], None) is True
    worker = created[-1]
    assert worker.workers == 2
    assert worker.kwargs["background_priority"] is True
    assert worker.kwargs["supported_exts"] == (".pptx", ".ppt")
    assert worker.kwargs["compute_groups"] is False
    assert worker.started is True

    win._note_user_activity()
    assert worker.activity == 1


def test_index_worker_finishes_only_after_maintenance(monkeypatch, tmp_path):
    order = []

    class _Conn:
        def close(self):
            order.append("close")

    monkeypatch.setattr("pptx_finder.ui.index_worker.db.connect", lambda _path: _Conn())
    monkeypatch.setattr("pptx_finder.ui.index_worker.db.init_db", lambda _conn: None)
    monkeypatch.setattr(
        "pptx_finder.ui.index_worker.indexer.update_index",
        lambda _conn, _roots, **_kwargs: {"indexed": 50, "deleted": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "pptx_finder.ui.index_worker.db.maintain",
        lambda _conn: order.append("maintain") or {},
    )
    worker = IndexWorker(
        str(tmp_path / "index.db"),
        [str(tmp_path)],
        workers=1,
        compute_groups=False,
    )
    worker.finished_index.connect(lambda _summary: order.append("finished"))

    worker.run()

    assert order.index("maintain") < order.index("finished")


def test_cancelled_index_worker_skips_expensive_post_processing(monkeypatch, tmp_path):
    calls = []

    class _Conn:
        def close(self):
            calls.append("close")

    monkeypatch.setattr("pptx_finder.ui.index_worker.db.connect", lambda _path: _Conn())
    monkeypatch.setattr("pptx_finder.ui.index_worker.db.init_db", lambda _conn: None)
    monkeypatch.setattr(
        "pptx_finder.ui.index_worker.indexer.update_index",
        lambda _conn, _roots, **_kwargs: {
            "indexed": 1,
            "deleted": 0,
            "errors": 0,
            "cancelled": 1,
        },
    )
    monkeypatch.setattr(
        "pptx_finder.cluster.compute_groups",
        lambda _conn: calls.append("groups"),
    )
    monkeypatch.setattr(
        "pptx_finder.ui.index_worker.db.maintain",
        lambda _conn: calls.append("maintain") or {},
    )
    worker = IndexWorker(
        str(tmp_path / "index.db"),
        [str(tmp_path)],
        workers=1,
        compute_groups=True,
    )
    emitted = []
    worker.finished_index.connect(emitted.append)

    worker.run()

    assert emitted and emitted[-1]["cancelled"] == 1
    assert "groups" not in calls
    assert "maintain" not in calls


def test_transparent_logo_does_not_walk_half_a_million_pixels(monkeypatch):
    pixel_calls = []

    class _Color:
        def alpha(self):
            return 0

        def red(self):
            return 0

        def green(self):
            return 0

        def blue(self):
            return 0

        def setAlpha(self, _value):
            pass

    class _Image:
        Format_ARGB32 = 1

        def convertToFormat(self, _fmt):
            return self

        def width(self):
            return 675

        def height(self):
            return 696

        def pixelColor(self, x, y):
            pixel_calls.append((x, y))
            return _Color()

        def setPixelColor(self, _x, _y, _c):
            pass

    image = _Image()

    class _Pixmap:
        def __init__(self, *_args):
            pass

        def isNull(self):
            return False

        def toImage(self):
            return image

        def scaled(self, *_args):
            return self

        @classmethod
        def fromImage(cls, _image):
            return cls()

    monkeypatch.setattr(main_window_mod, "QPixmap", _Pixmap)

    main_window_mod._app_logo()

    assert len(pixel_calls) <= 4


def test_version_manager_can_start_without_own_watcher(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    mgr = VersionManager()
    calls = []
    monkeypatch.setattr(mgr, "scan_deleted", lambda: calls.append("scan"))
    monkeypatch.setattr(mgr, "_start_watcher", lambda: calls.append("watch"))
    monkeypatch.setattr(mgr, "_start_reconcile_loop", lambda: calls.append("reconcile"))
    monkeypatch.setattr(mgr, "_start_vault_maintenance", lambda: calls.append("maintenance"))

    mgr.start(watch=False)

    assert calls == ["scan", "reconcile", "maintenance"]
    mgr.stop()


def test_watcher_rechecks_dynamic_enabled_extensions(tmp_path):
    ppt = tmp_path / "deck.pptx"
    doc = tmp_path / "notes.docx"
    ppt.write_bytes(b"ppt")
    doc.write_bytes(b"doc")
    enabled = {".pptx"}
    saved = []
    content = []
    handler = _Handler(
        saved.append,
        on_content_saved=content.append,
        allowed_exts=lambda: tuple(enabled),
    )

    handler._fire(str(doc))
    handler._fire(str(ppt))
    enabled.add(".docx")
    handler._fire(str(doc))

    assert saved == [str(ppt)]
    assert content == [str(doc)]


def test_feature_runtime_keeps_live_indexing_but_does_not_start_versions_by_default(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    watcher_instances = []

    class _Watcher:
        def __init__(self, roots, on_saved, on_moved, on_content_saved, on_removed,
                     allowed_exts=None):
            self.on_saved = on_saved
            self.allowed_exts = allowed_exts
            self.started = False
            self.stopped = False
            watcher_instances.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    class _Manager:
        def __init__(self):
            self.started = []
            self.stops = 0
            self.snapshots = []

        def start(self, *, watch=True):
            self.started.append(watch)

        def stop(self):
            self.stops += 1

        def snapshot_now(self, path):
            self.snapshots.append(path)
            return "v1"

        def move_path(self, _old, _new):
            pass

        def mark_deleted(self, _path):
            pass

    class _Bridge:
        def __init__(self):
            self.changed = []

        def emit_content_changed(self, path):
            self.changed.append(path)

    class _Win:
        def __init__(self):
            self.managers = []

        def set_version_manager(self, manager):
            self.managers.append(manager)

    manager = _Manager()
    bridge = _Bridge()
    win = _Win()
    monkeypatch.setattr(app_mod, "VaultWatcher", _Watcher)
    monkeypatch.setattr(app_mod, "default_watch_paths", lambda: [str(tmp_path)])
    runtime = app_mod._FeatureRuntime(win, manager, bridge)

    runtime.start()
    assert watcher_instances[-1].started is True
    assert watcher_instances[-1].allowed_exts() == (".pptx", ".ppt")
    assert manager.started == []

    runtime._on_ppt_saved(str(tmp_path / "deck.pptx"))
    assert bridge.changed == [str(tmp_path / "deck.pptx")]
    runtime.set_version_enabled(True)
    deadline = time.monotonic() + 1
    while not manager.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.started == [False]
    runtime._on_ppt_saved(str(tmp_path / "deck.pptx"))
    assert manager.snapshots == [str(tmp_path / "deck.pptx")]

    runtime.stop()
    assert watcher_instances[-1].stopped is True
    assert manager.stops == 1


def test_feature_runtime_snapshot_failure_reaches_watcher_retry(tmp_path):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"ppt")
    changed = []
    retries = []

    class _Manager:
        def snapshot_now(self, _path):
            raise RuntimeError("file is still being replaced")

        def stop(self):
            pass

    class _Bridge:
        def emit_content_changed(self, path):
            changed.append(path)

    class _Win:
        def set_version_manager(self, _manager):
            pass

    runtime = app_mod._FeatureRuntime(_Win(), _Manager(), _Bridge())
    runtime.version_enabled = True
    handler = _Handler(
        runtime._on_ppt_saved,
        on_content_saved=runtime._on_content_saved,
        allowed_exts=(".pptx",),
    )
    handler._schedule_retry = lambda path, attempt: retries.append((path, attempt))

    handler._fire(str(deck))

    assert changed == [str(deck)]
    assert retries == [(str(deck), 0)]


def test_lazy_version_manager_shutdown_does_not_open_optional_database():
    created = []

    class _Manager:
        def stop(self):
            pass

        def ping(self):
            return "ok"

    proxy = app_mod._LazyVersionManager(
        lambda: created.append(_Manager()) or created[-1]
    )

    proxy.stop()
    assert created == []
    assert proxy.ping() == "ok"
    assert len(created) == 1


def test_feature_runtime_shutdown_catches_watcher_start_race(monkeypatch, tmp_path):
    """A quick quit must not leave a watcher alive after async startup wins late."""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    entered = threading.Event()
    release = threading.Event()
    watchers = []

    class _Watcher:
        def __init__(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=2)
            self.started = False
            self.stopped = False
            watchers.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    class _Manager:
        def __init__(self):
            self.started = []
            self.stops = 0

        def start(self, *, watch=True):
            self.started.append(watch)

        def stop(self):
            self.stops += 1

    class _Bridge:
        def emit_content_changed(self, _path):
            pass

    class _Win:
        def set_version_manager(self, _manager):
            pass

    monkeypatch.setattr(app_mod, "VaultWatcher", _Watcher)
    monkeypatch.setattr(app_mod, "default_watch_paths", lambda: [str(tmp_path)])
    manager = _Manager()
    runtime = app_mod._FeatureRuntime(_Win(), manager, _Bridge())
    starter = threading.Thread(target=runtime.start)
    starter.start()
    assert entered.wait(timeout=1)

    runtime.stop()
    release.set()
    starter.join(timeout=2)

    assert not starter.is_alive()
    assert watchers and watchers[-1].started is True
    assert watchers[-1].stopped is True
    assert manager.started == []


def test_feature_runtime_contains_watcher_start_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))

    class _BrokenWatcher:
        def __init__(self, *_args, **_kwargs):
            raise OSError("watch unavailable")

    class _Manager:
        def stop(self):
            pass

    class _Bridge:
        def emit_content_changed(self, _path):
            pass

    class _Win:
        def set_version_manager(self, _manager):
            pass

    monkeypatch.setattr(app_mod, "VaultWatcher", _BrokenWatcher)
    runtime = app_mod._FeatureRuntime(_Win(), _Manager(), _Bridge())

    runtime.start()

    assert any("watch unavailable" in line for line in runtime.diagnostic_lines())
    runtime.stop()


def test_feature_runtime_reports_version_backend_start_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    config.set_version_management_enabled(True)
    attempted = threading.Event()

    class _Watcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    class _Manager:
        def __init__(self):
            self.stops = 0

        def start(self, *, watch=True):
            attempted.set()
            raise RuntimeError("version database broken")

        def stop(self):
            self.stops += 1

    class _Bridge:
        def __init__(self):
            self.feature_states = []

        def emit_content_changed(self, _path):
            pass

        def emit_feature_state(self, key, enabled):
            self.feature_states.append((key, enabled))

    class _Win:
        def set_version_manager(self, _manager):
            pass

    monkeypatch.setattr(app_mod, "VaultWatcher", _Watcher)
    monkeypatch.setattr(app_mod, "default_watch_paths", lambda: [str(tmp_path)])
    bridge = _Bridge()
    manager = _Manager()
    runtime = app_mod._FeatureRuntime(_Win(), manager, bridge)

    runtime.start()
    assert attempted.wait(timeout=1)
    deadline = time.monotonic() + 1
    while config.get_version_management_enabled() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert any("version database broken" in line for line in runtime.diagnostic_lines())
    assert config.get_version_management_enabled() is False
    assert runtime.version_enabled is False
    assert bridge.feature_states == [("version_management", False)]
    assert manager.stops == 1
    runtime.stop()
    assert manager.stops == 1


def test_visible_powerpoint_window_detection_is_not_an_empty_stub(monkeypatch):
    """Visible user PowerPoint must not be mistaken for a headless preview process."""
    monkeypatch.setattr(renderer.os, "name", "nt")

    def enum_windows(callback, extra):
        callback(1001, extra)

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(EnumWindows=enum_windows, IsWindowVisible=lambda _hwnd: True),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (7, 4242)),
    )

    assert renderer._pid_has_visible_window(4242) is True
    assert renderer._pid_has_visible_window(9999) is False


def test_autostart_shortcut_sync_never_blocks_window_startup(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_sync():
        started.set()
        release.wait(1)
        return True

    monkeypatch.setattr(app_mod, "_sync_autostart_preference", slow_sync)
    before = time.perf_counter()
    thread = app_mod._start_autostart_sync()
    elapsed = time.perf_counter() - before
    try:
        assert elapsed < 0.1
        assert started.wait(0.5)
    finally:
        release.set()
        thread.join(1)
