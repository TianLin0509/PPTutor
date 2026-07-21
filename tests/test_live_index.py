"""实时索引：保存事件单文件并入 + 启动按新鲜度做后台增量对账。"""
from __future__ import annotations

import threading
import time

import fixtures_gen as fx
from test_ui import StubRender, StubThumb, _finish_fake_task, _index, _index_multi, _install_fake_background_task

from pptx_finder import db, indexer, search
from pptx_finder.ui.index_worker import IndexWorker
from pptx_finder.ui.live_indexer import LiveIndexer
import pptx_finder.ui.main_window as main_window_mod
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.thumb_worker import ThumbWorker


class _FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class _FakeLiveIndexer:
    def __init__(self, *_args, **_kwargs):
        self.indexed = _FakeSignal()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self, _ms):
        return True

    def submit(self, _path):
        pass


def test_index_single_adds_without_deleting(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "old.pptx", [{"body": "旧文件保留"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    fx.make_pptx(docs / "new.pptx", [{"body": "新内容关键词XYZ"}])
    assert indexer.index_single(conn, str(docs / "new.pptx"))
    assert search.search(conn, "旧文件保留")       # 旧记录没被删
    assert search.search(conn, "新内容关键词XYZ")    # 新文件并入


def test_index_single_missing_file(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    assert indexer.index_single(conn, str(tmp_path / "nope.pptx")) is False


def test_health_recycle_sync_deletes_recycled_paths_from_index(qtbot, monkeypatch, tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    keep = docs / "keep.pptx"
    dup = docs / "dup.pptx"
    fx.make_pptx(keep, [{"body": "重复清理保留项"}])
    fx.make_pptx(dup, [{"body": "重复清理待删除项"}])
    dbp = tmp_path / "i.db"
    conn = db.connect(dbp)
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)

    def fake_recycle(paths):
        assert paths == [str(dup)]
        return {
            "ok": True,
            "recycled": 1,
            "recycled_paths": [str(dup)],
            "failed": [],
            "freed_bytes": dup.stat().st_size,
        }

    monkeypatch.setattr("pptx_finder.health.recycle_paths", fake_recycle)

    result = win._recycle_health_paths_and_sync_index([str(dup)])

    assert result["index_deleted"] == 1
    assert db.get_file_by_path(win._conn, str(dup)) is None
    assert db.get_file_by_path(win._conn, str(keep)) is not None
    assert not search.search(win._conn, "重复清理待删除项")
    assert search.search(win._conn, "重复清理保留项")


def test_live_index_via_snapshot(qtbot, tmp_path):
    """on_version_snapshot（watcher 事件）应把新建文件实时并入搜索索引。"""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    newp = tmp_path / "实时测试LT.pptx"
    fx.make_pptx(newp, [{"body": "实时索引验证内容"}])
    assert not search.search(win._conn, "实时测试LT")   # 索引前搜不到
    win.on_version_snapshot(str(newp), "v1")            # 模拟 watcher 留版事件
    res = search.search(win._conn, "实时测试LT")
    assert any("实时测试LT" in r.name for r in res)      # 实时进索引后可搜


def test_live_index_via_word_pdf_content_change(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    docx = tmp_path / "实时文档.docx"
    fx.make_docx(docx, ["Word 实时变化唯一词"])

    win.on_content_changed(str(docx))

    res = search.search(win._conn, "实时变化唯一词")
    assert [r.path for r in res] == [str(docx)]


def test_startup_skips_scan_when_indexed(qtbot, tmp_path):
    """已有索引时 _index_is_empty False（启动不再全盘扫）。"""
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win._index_is_empty() is False


def test_index_is_empty_on_blank_db(qtbot, tmp_path):
    conn = db.connect(tmp_path / "blank.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win._index_is_empty() is True


def test_startup_empty_index_check_runs_in_background(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []
    starts = []

    def fake_stats(_conn, **_kwargs):
        calls.append("stats")
        return {"file_count": 0, "page_count": 0}

    monkeypatch.setattr(main_window_mod.db, "stats", fake_stats)
    monkeypatch.setattr(main_window_mod, "LiveIndexer", _FakeLiveIndexer)
    monkeypatch.setattr(
        MainWindow,
        "_start_indexing",
        lambda self, roots, workers: starts.append((roots, workers)) or True,
    )

    conn = db.connect(tmp_path / "blank.db")
    db.init_db(conn)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=True,
        roots=["C:/docs"],
        workers=2,
    )
    qtbot.addWidget(win)

    assert starts == []
    startup_task = next(t for t in tasks if t.label == "startup-index-check")
    calls_before_startup_check = len(calls)

    _finish_fake_task(startup_task)

    assert len(calls) == calls_before_startup_check + 1
    assert starts == [(["C:/docs"], 2)]
    lines = "\n".join(win.diagnostic_lines())
    assert "startup_index_check:" in lines
    assert "decision=start_scan" in lines
    assert "files=0" in lines


def test_startup_empty_index_with_rebuild_reason_is_explained(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    starts = []

    monkeypatch.setattr(main_window_mod, "LiveIndexer", _FakeLiveIndexer)
    monkeypatch.setattr(
        MainWindow,
        "_start_indexing",
        lambda self, roots, workers: starts.append((roots, workers)) or True,
    )

    conn = db.connect(tmp_path / "blank-upgrade.db")
    db.init_db(conn)
    db.set_meta(conn, db.META_INDEX_REBUILD_REASON, "index_version:4->5")
    conn.commit()
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=True,
        roots=["C:/docs"],
        workers=2,
    )
    qtbot.addWidget(win)
    startup_task = next(t for t in tasks if t.label == "startup-index-check")

    _finish_fake_task(startup_task)

    assert starts == [(["C:/docs"], 2)]
    lines = "\n".join(win.diagnostic_lines())
    assert "decision=start_scan_rebuild" in lines
    assert "rebuild=index_version:4->5" in lines


def test_startup_existing_index_check_updates_status_without_scan(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    starts = []

    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn, **_kwargs: {
            "file_count": 4,
            "page_count": 9,
            "last_completed_scan_at": time.time(),
        },
    )
    monkeypatch.setattr(main_window_mod, "LiveIndexer", _FakeLiveIndexer)
    monkeypatch.setattr(
        MainWindow,
        "_start_indexing",
        lambda self, roots, workers: starts.append((roots, workers)) or True,
    )

    conn = _index(tmp_path)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=True,
    )
    qtbot.addWidget(win)
    startup_task = next(t for t in tasks if t.label == "startup-index-check")

    _finish_fake_task(startup_task)

    assert starts == []
    assert "索引就绪：4 个文件 · 9 页" in win.status_label.text()
    lines = "\n".join(win.diagnostic_lines())
    assert "startup_index_check:" in lines
    assert "decision=use_existing" in lines
    assert "files=4" in lines
    assert "pages=9" in lines


def test_startup_stale_index_reconciles_known_files_without_full_disk_scan(
    qtbot, monkeypatch, tmp_path,
):
    tasks = _install_fake_background_task(monkeypatch)
    starts = []
    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn, **_kwargs: {
            "file_count": 4,
            "page_count": 9,
            "last_completed_scan_at": time.time() - MainWindow._KNOWN_RECONCILE_INTERVAL_SEC - 1,
        },
    )
    monkeypatch.setattr(main_window_mod, "LiveIndexer", _FakeLiveIndexer)
    monkeypatch.setattr(
        MainWindow,
        "_start_indexing",
        lambda self, roots, workers: starts.append((roots, workers)) or True,
    )
    conn = _index(tmp_path)
    db.set_meta(
        conn,
        db.META_LAST_COMPLETED_SCAN_AT,
        str(time.time() - MainWindow._KNOWN_RECONCILE_INTERVAL_SEC - 1),
    )
    conn.commit()
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=True,
        roots=["C:/docs"],
        workers=2,
    )
    qtbot.addWidget(win)

    startup_task = next(t for t in tasks if t.label == "startup-index-check")
    _finish_fake_task(startup_task)

    assert starts == []
    known_task = next(t for t in tasks if t.label == "startup-known-file-reconcile")
    _finish_fake_task(known_task)
    assert "decision=reconcile_known" in "\n".join(win.diagnostic_lines())


def test_startup_old_scan_policy_schedules_low_priority_full_coverage(
    qtbot, monkeypatch, tmp_path,
):
    conn = _index(tmp_path)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=False,
    )
    qtbot.addWidget(win)
    scheduled = []
    monkeypatch.setattr(
        win,
        "_schedule_full_coverage_scan",
        lambda roots, reason: scheduled.append((roots, reason)),
        raising=False,
    )

    win._on_startup_index_checked(
        win._startup_index_token,
        ["C:/docs"],
        8,
        {
            "file_count": 4,
            "page_count": 9,
            "pending_count": 0,
            "last_completed_scan_at": time.time(),
            "last_known_reconcile_at": time.time(),
            "scan_policy_version": "1",
        },
    )

    assert scheduled == [(["C:/docs"], "scan_policy_upgrade")]
    assert win._startup_index_check_decision == "schedule_full_coverage_upgrade"


def test_startup_week_old_full_scan_schedules_single_worker_coverage(
    qtbot, monkeypatch, tmp_path,
):
    from pptx_finder.scanner import SCAN_POLICY_VERSION

    conn = _index(tmp_path)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=False,
    )
    qtbot.addWidget(win)
    scheduled = []
    monkeypatch.setattr(
        win,
        "_schedule_full_coverage_scan",
        lambda roots, reason: scheduled.append((roots, reason)),
        raising=False,
    )

    win._on_startup_index_checked(
        win._startup_index_token,
        None,
        8,
        {
            "file_count": 4,
            "page_count": 9,
            "pending_count": 0,
            "last_completed_scan_at": time.time() - win._FULL_COVERAGE_INTERVAL_SEC - 1,
                "last_known_reconcile_at": time.time(),
                "scan_policy_version": SCAN_POLICY_VERSION,
                "completed_feature_signature": win._current_index_feature_signature(),
            },
    )

    assert scheduled == [(None, "periodic_coverage")]
    assert win._startup_index_check_decision == "schedule_full_coverage"


def test_scheduled_coverage_defers_when_busy_then_uses_one_worker(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=False,
    )
    qtbot.addWidget(win)
    starts = []
    monkeypatch.setattr(
        win,
        "_start_indexing",
        lambda roots, workers: starts.append(
            (roots, workers, win._starting_automatic_coverage)
        ) or True,
    )
    win._coverage_scan_roots = ["C:/"]
    win._coverage_scan_reason = "periodic_coverage"
    win._search_pending_req = 42

    win._run_scheduled_coverage_scan()

    assert starts == []
    assert win._coverage_scan_timer.isActive()

    win._coverage_scan_timer.stop()
    win._search_pending_req = None
    win._run_scheduled_coverage_scan()

    assert starts == [(["C:/"], 1, True)]


def test_startup_existing_index_with_pending_reconciles_known_files_only(
    qtbot, monkeypatch, tmp_path,
):
    tasks = _install_fake_background_task(monkeypatch)
    starts = []

    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn, **_kwargs: {
            "file_count": 4,
            "page_count": 9,
            "pending_count": 2,
            "status_counts": {"ok": 2, "pending": 2},
        },
    )
    monkeypatch.setattr(main_window_mod, "LiveIndexer", _FakeLiveIndexer)
    monkeypatch.setattr(
        MainWindow,
        "_start_indexing",
        lambda self, roots, workers: starts.append((roots, workers)) or True,
    )

    conn = _index(tmp_path)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=True,
        roots=["C:/docs"],
        workers=2,
    )
    qtbot.addWidget(win)
    startup_task = next(t for t in tasks if t.label == "startup-index-check")

    _finish_fake_task(startup_task)

    assert starts == []
    assert any(t.label == "startup-known-file-reconcile" for t in tasks)
    lines = "\n".join(win.diagnostic_lines())
    assert "decision=reconcile_known" in lines
    assert "pending=2" in lines


def test_known_file_reconcile_finds_changed_pending_and_new_but_preserves_missing(tmp_path):
    docs = tmp_path / "known"
    docs.mkdir()
    changed = docs / "changed.pptx"
    deleted = docs / "deleted.pptx"
    pending = docs / "pending.pptx"
    unchanged = docs / "unchanged.pptx"
    for path, word in (
        (changed, "changed-old"),
        (deleted, "deleted-old"),
        (pending, "pending-old"),
        (unchanged, "unchanged"),
    ):
        fx.make_pptx(path, [{"body": word}])

    db_path = tmp_path / "known.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    conn.execute("UPDATE files SET status='pending' WHERE path=?", (str(pending),))
    conn.commit()
    conn.close()

    time.sleep(0.01)
    fx.make_pptx(changed, [{"body": "changed-new-and-longer"}])
    deleted.unlink()
    new_sibling = docs / "new-sibling.pptx"
    fx.make_pptx(new_sibling, [{"body": "created while app was closed"}])

    result = main_window_mod._scan_known_index_changes(str(db_path), limit=20)

    assert result["checked"] == 4
    assert set(result["paths"]) == {str(changed), str(new_sibling)}
    assert result["pending_paths"] == [str(pending)]
    assert str(deleted) not in result["paths"]
    assert result["new_paths"] == 1
    assert result["remaining"] == 0
    verify = db.connect(db_path)
    try:
        assert db.meta_value(verify, db.META_LAST_KNOWN_RECONCILE_AT, "0") == "0"
    finally:
        verify.close()


def test_known_file_reconcile_clean_pass_advances_freshness_marker(tmp_path):
    conn = _index(tmp_path)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    result = main_window_mod._scan_known_index_changes(db_path)

    assert result["paths"] == []
    verify = db.connect(db_path)
    try:
        assert float(db.meta_value(verify, db.META_LAST_KNOWN_RECONCILE_AT, "0")) > 0
    finally:
        verify.close()


def test_known_file_reconcile_skips_transient_stat_errors(monkeypatch, tmp_path):
    conn = _index(tmp_path)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    protected = conn.execute("SELECT path FROM files ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    real_stat = main_window_mod.os.stat

    def guarded_stat(path):
        if str(path).endswith(protected):
            raise PermissionError("temporarily unavailable")
        return real_stat(path)

    monkeypatch.setattr(main_window_mod.os, "stat", guarded_stat)
    result = main_window_mod._scan_known_index_changes(db_path)

    assert protected not in result["paths"]


def test_known_file_reconcile_retries_hydrated_cloud_placeholder(
    monkeypatch, tmp_path,
):
    conn = _index(tmp_path)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    cloud = conn.execute("SELECT path FROM files ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE files SET status='cloud_placeholder' WHERE path=?", (cloud,))
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_window_mod.indexer_mod, "_is_cloud_placeholder", lambda *_: False)
    hydrated = main_window_mod._scan_known_index_changes(db_path)
    assert cloud in hydrated["paths"]

    monkeypatch.setattr(main_window_mod.indexer_mod, "_is_cloud_placeholder", lambda *_: True)
    still_cloud = main_window_mod._scan_known_index_changes(db_path)
    assert cloud not in still_cloud["paths"]


def test_known_file_reconcile_honors_error_retry_breaker(tmp_path):
    conn = _index(tmp_path)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    target = conn.execute("SELECT path FROM files ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute(
        "UPDATE files SET status='error', parse_failures=?, retry_after=0 WHERE path=?",
        (indexer.MAX_UNCHANGED_PARSE_FAILURES, target),
    )
    conn.commit()
    conn.close()

    fused = main_window_mod._scan_known_index_changes(db_path)
    assert target not in fused["paths"]

    conn = db.connect(db_path)
    conn.execute(
        "UPDATE files SET parse_failures=1, retry_after=? WHERE path=?",
        (time.time() + 3600, target),
    )
    conn.commit()
    conn.close()
    waiting = main_window_mod._scan_known_index_changes(db_path)
    assert target not in waiting["paths"]

    conn = db.connect(db_path)
    conn.execute("UPDATE files SET retry_after=0 WHERE path=?", (target,))
    conn.commit()
    conn.close()
    due = main_window_mod._scan_known_index_changes(db_path)
    assert target in due["paths"]


def test_known_file_reconcile_rotates_capped_batches(tmp_path):
    docs = tmp_path / "rotation"
    docs.mkdir()
    paths = []
    for i in range(4):
        path = docs / f"pending-{i}.pptx"
        fx.make_pptx(path, [{"body": f"pending {i}"}])
        paths.append(str(path))
    db_path = tmp_path / "rotation.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    conn.execute("UPDATE files SET status='pending'")
    conn.commit()
    conn.close()

    pending = main_window_mod._scan_known_index_changes(str(db_path), limit=2)
    assert pending["paths"] == []
    assert set(pending["pending_paths"]) == set(paths)

    conn = db.connect(db_path)
    conn.execute("UPDATE files SET status='ok', size=size+1")
    conn.commit()
    conn.close()
    first = main_window_mod._scan_known_index_changes(str(db_path), limit=2)
    second = main_window_mod._scan_known_index_changes(str(db_path), limit=2)

    assert first["remaining"] == 2
    assert second["remaining"] == 2
    assert set(first["paths"]).isdisjoint(second["paths"])
    assert set(first["paths"] + second["paths"]) == set(paths)


def test_pending_resume_drips_all_paths_in_current_session(qtbot, tmp_path):
    win = MainWindow(
        conn=_index(tmp_path),
        render_worker=StubRender(),
        thumb_worker=StubThumb(),
        do_index=False,
    )
    qtbot.addWidget(win)
    submitted = []
    win._submit_live_index = submitted.append

    win._queue_pending_index_resume(["a.pptx", "b.pptx", "c.pptx"])

    assert submitted == ["a.pptx"]
    assert list(win._startup_pending_queue) == ["b.pptx", "c.pptx"]
    assert win._startup_pending_timer.isActive()
    win._resume_one_pending_index()
    win._resume_one_pending_index()
    assert submitted == ["a.pptx", "b.pptx", "c.pptx"]
    assert not win._startup_pending_timer.isActive()


def test_index_progress_uses_lock_free_aggregate_bar(qtbot, tmp_path):
    """建库中不在 GUI 线程轮询 type_counts，避免与写库/VACUUM 争锁。"""
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    def _f(name, ext, status):
        db.upsert_file(conn, path=str(tmp_path / name), name=name, ext=ext, size=1, mtime=1.0,
                       content_hash="h", page_count=1, status=status, error="", indexed_at=1.0)

    _f("a.pptx", ".pptx", "ok")       # 已建
    _f("b.pptx", ".pptx", "ok")       # 已建
    _f("c.docx", ".docx", "pending")  # 已登记文件名、内容待补建
    conn.commit()

    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)

    win._on_index_progress(2, 3, r"C:\docs\c.docx")

    assert win.type_rail.isHidden()
    assert not win.index_bar.isHidden()
    assert win.index_bar.maximum() == 3
    assert win.index_bar.value() == 2
    assert "前台操作优先" in win.status_label.text()


def test_live_indexer_async_off_main_thread(qtbot, tmp_path):
    """LiveIndexer：submit 入队 → 后台线程索引 → indexed 信号 → 文件可搜。

    这是 UI 冻结修复的核心——实时索引在后台串行线程跑，主线程绝不 parse/写库。
    """
    docs = tmp_path / "d"
    docs.mkdir()
    dbp = tmp_path / "i.db"
    conn = db.connect(dbp)
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)  # 空基线
    fx.make_pptx(docs / "异步LT.pptx", [{"body": "异步索引内容ABC"}])

    li = LiveIndexer(str(dbp))
    li.start()
    try:
        with qtbot.waitSignal(li.indexed, timeout=5000):
            li.submit(str(docs / "异步LT.pptx"))  # 主线程仅入队
    finally:
        li.stop()
        li.wait(3000)

    conn2 = db.connect(dbp)  # 新连接读后台已提交的数据
    assert any("异步LT" in r.name for r in search.search(conn2, "异步索引内容ABC"))


def test_live_indexer_coalesces_duplicate_paths(tmp_path):
    li = LiveIndexer(str(tmp_path / "i.db"))
    p = str(tmp_path / "same.pptx")
    li.submit(p)
    li.submit(p)
    assert li._q.qsize() == 1


def test_live_indexer_retries_transient_database_connect_failure(
    monkeypatch, qtbot, tmp_path
):
    attempts = []

    class FakeConn:
        def close(self):
            return None

    def flaky_connect(_path):
        attempts.append(True)
        if len(attempts) < 3:
            raise OSError("database temporarily unavailable")
        return FakeConn()

    monkeypatch.setattr("pptx_finder.ui.live_indexer.db.connect", flaky_connect)
    monkeypatch.setattr(
        "pptx_finder.ui.live_indexer.indexer.index_single",
        lambda _conn, _path, **_kwargs: True,
    )
    monkeypatch.setattr(LiveIndexer, "_CONNECT_RETRY_SEC", 0.01, raising=False)
    li = LiveIndexer(str(tmp_path / "i.db"))
    li.start()
    try:
        with qtbot.waitSignal(li.indexed, timeout=1000):
            li.submit(str(tmp_path / "deck.pptx"))
        assert len(attempts) == 3
        assert li.isRunning()
    finally:
        li.stop()
        li.wait(3000)


def test_live_index_deferred_while_full_index_running(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    submitted: list[str] = []

    class FakeLive:
        def submit(self, path: str):
            submitted.append(path)

        def stop(self):
            pass

        def wait(self, _ms):
            return True

    class RunningIndexer:
        def __init__(self):
            self.running = True

        def isRunning(self):
            return self.running

        def stop(self):
            pass

        def wait(self, _ms):
            return True

    running = RunningIndexer()
    win._live = FakeLive()
    win._indexer = running

    win._index_file_live("C:/new.pptx")

    assert submitted == []
    assert win._live_deferred_paths == {"C:/new.pptx"}

    running.running = False
    win._flush_deferred_live_index()

    assert submitted == ["C:/new.pptx"]
    assert win._live_deferred_paths == set()


def test_deferred_live_flush_is_batched(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_LIVE_FLUSH_BATCH", 2)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    submitted: list[str] = []
    scheduled: list[tuple[int, object]] = []

    monkeypatch.setattr(win, "_submit_live_index", lambda path: submitted.append(path))
    monkeypatch.setattr(
        main_window_mod.QTimer,
        "singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )
    win._live_deferred_paths = {f"C:/new-{i}.pptx" for i in range(5)}

    win._flush_deferred_live_index()

    assert len(submitted) == 2
    assert len(win._live_deferred_paths) == 3
    assert len(scheduled) == 1
    assert scheduled[-1][0] >= 1

    scheduled.pop(0)[1]()
    assert len(submitted) == 4
    assert len(win._live_deferred_paths) == 1
    assert len(scheduled) == 1

    scheduled.pop(0)[1]()
    assert len(submitted) == 5
    assert win._live_deferred_paths == set()
    assert scheduled == []


def test_deferred_live_flush_does_not_sort_entire_storm(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_LIVE_FLUSH_BATCH", 64)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    submitted: list[str] = []
    scheduled: list[tuple[int, object]] = []

    def fail_sorted(_items):
        raise AssertionError("deferred live flush must not sort the full pending storm")

    monkeypatch.setattr(win, "_submit_live_index", lambda path: submitted.append(path))
    monkeypatch.setattr(main_window_mod, "sorted", fail_sorted, raising=False)
    monkeypatch.setattr(
        main_window_mod.QTimer,
        "singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )
    win._live_deferred_paths = {f"C:/storm-{i}.pptx" for i in range(500)}

    win._flush_deferred_live_index()

    assert len(submitted) == 64
    assert len(win._live_deferred_paths) == 436
    assert len(scheduled) == 1
    assert scheduled[-1][0] >= 1


def test_index_worker_reports_done_only_after_clustering(monkeypatch, qtbot, tmp_path):
    cluster_started = threading.Event()
    release_cluster = threading.Event()
    emitted = []

    def fake_update_index(conn, roots, progress_cb=None, workers=None, stop_event=None):
        return {"indexed": 1, "deleted": 0}

    def slow_compute_groups(conn):
        cluster_started.set()
        release_cluster.wait(1)
        return {}

    monkeypatch.setattr(indexer, "update_index", fake_update_index)
    monkeypatch.setattr("pptx_finder.cluster.compute_groups", slow_compute_groups)

    worker = IndexWorker(str(tmp_path / "i.db"), [str(tmp_path)], workers=1)
    worker.finished_index.connect(lambda summary: emitted.append(summary))
    worker.start()
    try:
        assert cluster_started.wait(1), "cluster should start in worker thread"
        qtbot.wait(80)
        assert emitted == []
    finally:
        release_cluster.set()
        worker.wait(1000)
    qtbot.waitUntil(lambda: bool(emitted), timeout=1000)
    assert emitted == [{"indexed": 1, "deleted": 0}]


def test_no_change_coverage_skips_cluster_and_database_maintenance(monkeypatch, qtbot, tmp_path):
    heavy_calls = []

    monkeypatch.setattr(
        indexer,
        "update_index",
        lambda *args, **kwargs: {
            "indexed": 0,
            "deleted": 0,
            "skipped_ppt": 0,
            "errors": 0,
        },
    )
    monkeypatch.setattr(
        "pptx_finder.cluster.compute_groups",
        lambda _conn: heavy_calls.append("cluster") or {},
    )
    monkeypatch.setattr(
        "pptx_finder.db.maintain",
        lambda _conn: heavy_calls.append("maintain") or {"error": ""},
    )

    worker = IndexWorker(str(tmp_path / "i.db"), [str(tmp_path)], workers=1)
    worker.start()
    try:
        with qtbot.waitSignal(worker.finished_index, timeout=3000):
            pass
        worker.wait(1000)
    finally:
        worker.stop()
        worker.wait(1000)

    assert heavy_calls == []


def test_index_worker_connection_failure_emits_terminal_error(monkeypatch, qtbot, tmp_path):
    monkeypatch.setattr(
        "pptx_finder.ui.index_worker.db.connect",
        lambda _path: (_ for _ in ()).throw(OSError("index database unavailable")),
    )
    worker = IndexWorker(str(tmp_path / "i.db"), [str(tmp_path)], workers=1)
    worker.start()
    try:
        with qtbot.waitSignal(worker.finished_index, timeout=1000) as blocker:
            pass
        assert "index database unavailable" in str(blocker.args[0].get("error"))
    finally:
        worker.stop()
        worker.wait(3000)


def test_index_worker_throttles_progress_burst_before_ui(monkeypatch, qtbot, tmp_path):
    emitted: list[tuple[int, int, str]] = []

    def fake_update_index(conn, roots, progress_cb=None, workers=None, stop_event=None):
        assert progress_cb is not None
        for i in range(1, 20):
            progress_cb(i, 100, f"deck-{i}.pptx")
        progress_cb(100, 100, "完成")
        return {"indexed": 100, "deleted": 0}

    monkeypatch.setattr(indexer, "update_index", fake_update_index)
    monkeypatch.setattr("pptx_finder.cluster.compute_groups", lambda _conn: {})
    worker = IndexWorker(str(tmp_path / "i.db"), [str(tmp_path)], workers=1)
    worker.progress.connect(lambda done, total, cur: emitted.append((done, total, cur)))
    worker.start()
    try:
        with qtbot.waitSignal(worker.finished_index, timeout=3000):
            pass
        worker.wait(1000)
        qtbot.wait(50)
    finally:
        if worker.isRunning():
            worker.stop()
            worker.wait(1000)

    assert emitted[0] == (1, 100, "deck-1.pptx")
    assert emitted[-1] == (100, 100, "完成")
    assert len(emitted) <= 3


def test_automatic_coverage_worker_uses_windows_background_priority(
    monkeypatch, qtbot, tmp_path,
):
    priority = []
    update_kwargs = []
    monkeypatch.setattr(
        "pptx_finder.ui.index_worker._set_windows_background_mode",
        lambda enabled: priority.append(enabled) or True,
        raising=False,
    )
    monkeypatch.setattr(
        indexer,
        "update_index",
        lambda *args, **kwargs: update_kwargs.append(kwargs) or {"indexed": 0, "deleted": 0},
    )

    worker = IndexWorker(
        str(tmp_path / "i.db"),
        [str(tmp_path)],
        workers=1,
        background_priority=True,
    )
    worker.start()
    with qtbot.waitSignal(worker.finished_index, timeout=3000):
        pass
    assert worker.wait(1000)
    assert priority == [True, False]
    assert update_kwargs and update_kwargs[0]["isolated_worker"] is True


def test_thumb_worker_coalesces_duplicate_requests():
    tw = ThumbWorker()
    tw.request("a.pptx", 1)
    tw.request("a.pptx", 1)
    tw.request("a.pptx", 2)
    assert tw._q.qsize() == 2
    tw.clear()


def test_live_refresh_keeps_selection_by_path(qtbot, tmp_path):
    """live 后台重搜（查询文本未变）：选中按 path 保留，不被拽回首行。"""
    conn = _index_multi(tmp_path, {f"deck-{i}.pptx": [f"共同词 保留选中 变体{i}"] for i in range(3)})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False,
                     smart_grouping_enabled=False)
    qtbot.addWidget(win)
    win.search_box.setText("共同词")
    win._do_search()
    assert win.result_list.count() == 3

    win.result_list.setCurrentRow(1)   # 用户选中第二个文件
    keep_path = win._results[1].path
    assert win._cur is not None and win._cur.path == keep_path

    win._last_user_input_ts = 0.0      # F1 用户输入宽限：拨到宽限期外，让后台重搜真实发生
    win._do_live_refresh()             # watcher 触发的后台重搜：结果集不变

    assert win.result_list.count() == 3
    assert win._cur is not None
    assert win._cur.path == keep_path  # 选中不跳回首行
    assert win.result_list.currentRow() == 1


def test_user_requery_still_selects_first(qtbot, tmp_path):
    """用户主动改词的重搜保持原行为：选中回第一行。"""
    conn = _index_multi(tmp_path, {f"deck-{i}.pptx": [f"共同词 保留选中 变体{i}"] for i in range(3)})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False,
                     smart_grouping_enabled=False)
    qtbot.addWidget(win)
    win.search_box.setText("共同词")
    win._do_search()
    win.result_list.setCurrentRow(2)

    win.search_box.setText("保留选中")
    win._do_search()

    assert win.result_list.count() == 3
    assert win.result_list.currentRow() == win._first_selectable_row()
    assert win._cur is not None
    assert win._cur.path == win._results[0].path


# ---------- 后台刷新节流（F1 重搜间隔/输入宽限、F2 预览去重、F3 detail 免重载） ----------

def test_live_refresh_throttles_background_research(qtbot, monkeypatch, tmp_path):
    """F1：后台重搜最小间隔 15s——间隔内的第二次 tick 直接放弃，不 reschedule。"""
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    searches = []
    monkeypatch.setattr(win, "_do_search", lambda: searches.append(1))
    win._last_user_input_ts = 0.0  # 越过用户输入宽限，单测 15s 间隔

    win._do_live_refresh()
    assert searches == [1]           # 首次后台重搜放行

    win._do_live_refresh()
    assert searches == [1]           # 15s 内第二次被跳过

    win._last_bg_research_ts -= MainWindow._BG_RESEARCH_MIN_INTERVAL_SEC + 1
    win._do_live_refresh()
    assert searches == [1, 1]        # 间隔推进后放行


def test_live_refresh_waits_for_user_input_grace(qtbot, monkeypatch, tmp_path):
    """F1：用户 5s 内刚改过查询词时后台重搜不抢跑；宽限过后放行。"""
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    searches = []
    monkeypatch.setattr(win, "_do_search", lambda: searches.append(1))
    win.search_box.setText("昇腾")   # textChanged 打点 _last_user_input_ts = now

    win._do_live_refresh()
    assert searches == []            # 宽限 5s 内不放行

    win._last_user_input_ts = time.monotonic() - MainWindow._USER_INPUT_GRACE_SEC - 1
    win._do_live_refresh()
    assert searches == [1]           # 宽限过后放行


def test_live_refresh_manual_search_bypasses_throttle(qtbot, tmp_path):
    """F1：用户手动 _do_search 不经过节流，也不占用后台重搜的 15s 时钟。"""
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")

    win._do_search()                 # 手动搜索：立即执行
    assert win._results
    assert win._last_bg_research_ts == 0.0  # 手动搜索不占用后台时钟

    win._last_user_input_ts = 0.0
    win._do_live_refresh()           # 首次后台 tick 仍立即放行并打点
    assert win._last_bg_research_ts > 0.0


def test_preview_request_dedupes_same_file_same_page(qtbot, tmp_path):
    """F2：同文件同页的预览重发被去重；翻页/文件内容变化照常发出。"""
    render = StubRender()
    win = MainWindow(conn=_index(tmp_path), render_worker=render, thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    assert win._cur is not None

    win._request_preview()
    assert len(render.calls) == 1
    win._request_preview()           # 同 key：后台重搜恢复选中后的连带重发被去重
    assert len(render.calls) == 1

    win._view_page += 1              # 翻页 → key 不同，照常发出
    win._request_preview()
    assert len(render.calls) == 2

    win._cur.mtime += 1000           # 文件内容已变（编辑后重索引）→ 照常重渲染
    win._request_preview()
    assert len(render.calls) == 3


def _drive_detail_load_once(win, tasks):
    """调度一次 detail 加载并同步跑完（等价于 80ms timer 到点 + 后台任务完成）。"""
    win._schedule_detail_update()
    win._detail_update_timer.stop()
    win._run_detail_update(win._detail_update_token)
    detail_tasks = [t for t in tasks if t.label == "detail-load"]
    _finish_fake_task(detail_tasks[-1])
    return detail_tasks


def test_detail_schedule_skips_reload_for_same_path(qtbot, monkeypatch, tmp_path):
    """F3：同 path 且内容未变时 _schedule_detail_update 不重查版本+大纲；
    面板若在结果重建瞬时被清空，用缓存 payload 同步回填。"""
    tasks = _install_fake_background_task(monkeypatch)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    cur = win._results[0]
    win._cur = cur

    detail_tasks = _drive_detail_load_once(win, tasks)
    assert len(detail_tasks) == 1
    assert win._detail_loaded_path == cur.path
    assert win.detail_panel._path == cur.path

    win.detail_panel.clear_selection()       # 模拟结果重建瞬时清空面板
    win._schedule_detail_update()
    detail_tasks = [t for t in tasks if t.label == "detail-load"]
    assert len(detail_tasks) == 1            # 没有再起后台加载
    assert not win._detail_update_timer.isActive()
    assert win.detail_panel._path == cur.path  # 缓存 payload 同步回填


def test_detail_schedule_reloads_on_path_change_and_force(qtbot, monkeypatch, tmp_path):
    """F3：force（留版/恢复版本后）与换 path 照常重载。"""
    tasks = _install_fake_background_task(monkeypatch)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), thumb_worker=StubThumb(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    cur = win._results[0]
    win._cur = cur
    _drive_detail_load_once(win, tasks)

    win._schedule_detail_update(force=True)  # 同 path 但 force → 重载
    win._detail_update_timer.stop()
    win._run_detail_update(win._detail_update_token)
    detail_tasks = [t for t in tasks if t.label == "detail-load"]
    assert len(detail_tasks) == 2
    _finish_fake_task(detail_tasks[-1])

    others = [r for r in db.recent_files(win._conn, limit=20) if r.path != cur.path]
    assert others
    win._cur = others[0]                     # 换 path → 重载
    win._schedule_detail_update()
    win._detail_update_timer.stop()
    win._run_detail_update(win._detail_update_token)
    detail_tasks = [t for t in tasks if t.label == "detail-load"]
    assert len(detail_tasks) == 3
