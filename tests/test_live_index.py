"""实时索引：watcher 留版事件 → 单文件并入搜索（无需重扫）+ 启动跳过全盘扫。"""
from __future__ import annotations

import threading

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
        lambda _conn: {"file_count": 4, "page_count": 9},
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


def test_startup_existing_index_with_pending_resumes_scan(qtbot, monkeypatch, tmp_path):
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

    assert starts == [(["C:/docs"], 2)]
    lines = "\n".join(win.diagnostic_lines())
    assert "decision=resume_pending" in lines
    assert "pending=2" in lines


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
    assert win._type_bars["Excel"]._cap.text() == "Excel —"   # 没有 Excel
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
