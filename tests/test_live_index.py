"""实时索引：保存事件单文件并入 + 启动按新鲜度做后台增量对账。"""
from __future__ import annotations

import threading
import time

import fixtures_gen as fx
from test_ui import StubRender, StubThumb, _finish_fake_task, _index, _install_fake_background_task

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

    def fake_stats(_conn):
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

    assert calls == []
    assert starts == []
    startup_task = next(t for t in tasks if t.label == "startup-index-check")

    _finish_fake_task(startup_task)

    assert calls == ["stats"]
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
        lambda _conn: {
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
        lambda _conn: {
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
        lambda roots, workers: starts.append((roots, workers)) or True,
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

    assert starts == [(["C:/"], 1)]


def test_startup_existing_index_with_pending_reconciles_known_files_only(
    qtbot, monkeypatch, tmp_path,
):
    tasks = _install_fake_background_task(monkeypatch)
    starts = []

    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn: {
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


def test_index_progress_updates_type_rail(qtbot, tmp_path):
    """建库中底部分类型迷你条（设计 D）：每类显示 已建/发现 x/y，建完打 ✓，无此类显 —。"""
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

    assert not win.type_rail.isHidden()
    assert win._type_bars["PPT"]._cap.text() == "PPT 2/2 ✓"   # 2 个 pptx 都已建
    assert win._type_bars["Word"]._cap.text() == "Word 0/1"   # 1 个 docx 待补建
    assert win._type_bars["PDF"]._cap.text() == "PDF —"       # 没有 PDF（xlsx/txt 已砍，桶=PPT/Word/PDF）
    win._close_type_conn()


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


def test_index_worker_reports_search_ready_before_clustering(monkeypatch, qtbot, tmp_path):
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
        assert emitted == [{"indexed": 1, "deleted": 0}]
    finally:
        release_cluster.set()
        worker.wait(1000)


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


def test_thumb_worker_coalesces_duplicate_requests():
    tw = ThumbWorker()
    tw.request("a.pptx", 1)
    tw.request("a.pptx", 1)
    tw.request("a.pptx", 2)
    assert tw._q.qsize() == 2
    tw.clear()
