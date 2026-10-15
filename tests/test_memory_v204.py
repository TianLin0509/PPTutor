"""v2.0.4：常驻内存过大的三处根因。

真机（588 万个文件）上进程在 835 MB 与 1.78 GB 之间来回锯齿：
1. 脏目录一溢出就立刻全盘重扫，扫 8 分钟期间又溢出，于是每 9 分钟一轮、永不停；
2. 扫描时把**每一个**路径都装进一个 set，只为了去重和删除判定；
3. 文件名索引的几大段全攒在内存里，落盘时还整体复制一遍。
"""
from __future__ import annotations

import os
import time
import tracemalloc

from pptx_finder import db, indexer, namestore


def _conn(tmp_path, name="i.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


# ---------- 1. 溢出触发的全盘校准要限频，扫完不能马上再扫 ----------

def _window(qtbot, tmp_path, monkeypatch):
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    conn = _conn(tmp_path, "ui.db")
    win = MainWindow(conn=conn, do_index=False, index_all_files_enabled=True)
    qtbot.addWidget(win)
    scheduled: list[str] = []
    monkeypatch.setattr(
        win, "_schedule_full_coverage_scan",
        lambda roots, reason: scheduled.append(reason))
    monkeypatch.setattr(win, "_toast", lambda *a, **k: None)
    return win, conn, scheduled


def test_overflow_right_after_full_scan_does_not_rescan(qtbot, tmp_path, monkeypatch):
    win, conn, scheduled = _window(qtbot, tmp_path, monkeypatch)
    win._on_index_done({"indexed": 0})          # 一轮完整扫描刚结束
    assert time.time() - win._last_full_scan_wall < 5
    for i in range(win._INVENTORY_DIRS_MAX + 10):
        win.note_inventory_dir_dirty(str(tmp_path / f"d{i}"))
    assert win._inventory_dirs_overflowed is True

    win._run_inventory_reconcile()

    assert scheduled == []                       # v2.0.3：这里会立刻再排一轮全盘扫描
    assert win._inventory_dirs_overflowed is True  # 标记留着，过了冷却期再校准
    conn.close()


def test_overflow_long_after_last_scan_still_calibrates(qtbot, tmp_path, monkeypatch):
    win, conn, scheduled = _window(qtbot, tmp_path, monkeypatch)
    win._last_full_scan_wall = time.time() - win._OVERFLOW_RESCAN_MIN_SEC - 1
    win._inventory_dirs_overflowed = True

    win._run_inventory_reconcile()

    assert scheduled == ["inventory_live_overflow"]
    conn.close()


def test_failed_or_cancelled_scan_does_not_reset_cooldown(qtbot, tmp_path, monkeypatch):
    win, conn, _scheduled = _window(qtbot, tmp_path, monkeypatch)
    win._last_full_scan_wall = 0.0
    win._on_index_done({"cancelled": 1})
    win._on_index_done({"error": "boom"})
    assert win._last_full_scan_wall == 0.0
    conn.close()


# ---------- 2. 扫描不再把每个路径都留在内存里 ----------

def _tree(root, dirs=20, files=30):
    for d in range(dirs):
        sub = root / f"dir{d:02d}"
        sub.mkdir(parents=True)
        for f in range(files):
            (sub / f"file_{d:02d}_{f:03d}.bin").write_bytes(b"x")


def test_disjoint_roots_keep_no_per_path_set(tmp_path):
    seen = indexer._ScanSeen({}, mode="none")
    for i in range(1000):
        assert seen.add(f"C:\\a\\{i}.bin")
    assert len(seen) == 1000
    assert seen._all is None and seen._dirs is None
    assert seen._existing_seen == set()


def test_scan_seen_only_remembers_existing_rows():
    existing = {"C:\\a\\old.pptx": object()}
    seen = indexer._ScanSeen(existing, mode="none")
    seen.add("C:\\a\\old.pptx")
    seen.add("C:\\a\\new.bin")
    assert "C:\\a\\old.pptx" in seen
    assert seen._existing_seen == {"C:\\a\\old.pptx"}


def test_roots_overlap_detection():
    assert indexer._roots_overlap(["C:\\", "C:\\Work"])
    assert indexer._roots_overlap(["D:\\x", "d:\\X\\"])
    assert not indexer._roots_overlap(["C:\\", "D:\\"])
    assert not indexer._roots_overlap(["C:\\Work", "C:\\Workshop"])


def test_nested_roots_still_count_each_file_once(tmp_path):
    root = tmp_path / "data"
    _tree(root, dirs=5, files=7)
    conn = _conn(tmp_path)
    names: list[str] = []
    summary = indexer.update_index(
        conn, [str(root), str(root / "dir01")],
        workers=1, supported_exts=(".pptx",),
        name_sink=lambda p, size, mtime: names.append(p),
    )
    assert summary["scanned"] == 35
    assert len(names) == len(set(names)) == 35
    conn.close()


def test_missing_file_is_still_retired_without_full_path_set(tmp_path):
    root = tmp_path / "decks"
    root.mkdir()
    import fixtures_gen as fx

    keep = fx.make_pptx(root / "keep.pptx", [{"body": "保留"}])
    gone = fx.make_pptx(root / "gone.pptx", [{"body": "删除"}])
    conn = _conn(tmp_path)
    indexer.update_index(conn, [str(root)], workers=1, supported_exts=(".pptx",))
    os.remove(gone)
    summary = indexer.update_index(conn, [str(root)], workers=1, supported_exts=(".pptx",))
    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert str(keep) in paths and str(gone) not in paths
    assert summary["deleted"] == 1
    conn.close()


# ---------- 3. 文件名索引的大段攒在临时文件里 ----------

def test_name_builder_keeps_bulk_sections_out_of_process_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    n = 150_000
    from pptx_finder import namequery
    namequery.fold("预热繁簡詞典")  # 词典加载是一次性的，不算进建库
    tracemalloc.start()
    try:
        builder = namestore.NameStoreBuilder()
        for i in range(n):
            builder.add(f"C:\\projects\\group{i % 50:02d}\\一份很长的文件名_{i:06d}_final.docx",
                        1234, 1_700_000_000)
        _cur, peak_add = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        dest = builder.write(kind=namestore.MAIN)
        _cur, peak_write = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    file_size = os.path.getsize(dest)
    # 旧实现：攒料≈文件大小（1.0），落盘再复制一遍（2.0）。新实现只剩偏移表（每条 4 字节）
    # 和五个临时文件的写缓冲。
    assert file_size > 15_000_000
    assert peak_add < file_size * 0.3, (peak_add, file_size)
    assert peak_write < file_size * 0.3, (peak_write, file_size)
    builder.close()

    with namestore.NameStore(dest) as store:
        assert store.count == n
        path, name, size, _mtime, is_dir = store.entry(n - 1)
        assert name == f"一份很长的文件名_{n - 1:06d}_final.docx"
        assert path.endswith(name) and size == 1234 and not is_dir


def test_name_builder_close_removes_spool_files(tmp_path, monkeypatch):
    data = tmp_path / "appdata"
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(data))
    builder = namestore.NameStoreBuilder()
    builder.add(str(tmp_path / "a.txt"), 1, 0)
    assert any(p.name.startswith(".names-part-") for p in data.iterdir())
    builder.close()
    assert not any(p.name.startswith(".names-part-") for p in data.iterdir())


def test_bounded_dir_cache_keeps_paths_and_scoped_search_exact(tmp_path, monkeypatch):
    """目录去重表只记最近用过的目录；漏记只会让 DIRS 多存一份，路径与搜索结果不能变。"""
    from pptx_finder import search

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(namestore, "_DIR_CACHE_MAX", 3)
    expected = []
    builder = namestore.NameStoreBuilder()
    for rnd in range(3):                       # 来回切换目录，逼出淘汰后再遇到的情况
        for d in range(8):
            directory = f"C:\\work\\proj{d}"
            if rnd == 0:
                builder.add_dir(directory, 0)
            path = f"{directory}\\note_{rnd}_{d}.txt"
            builder.add(path, 10 + d, 1_700_000_000)
            expected.append(path)
    dest = builder.write(kind=namestore.MAIN)
    builder.close()
    with namestore.NameStore(dest) as store:
        files = [store.entry(i)[0] for i in range(store.count) if not store.entry(i)[4]]
        assert files == expected
        hits = sorted(r.path for r in search.search_names(store, "note", scope="C:\\work\\proj5",
                                                       exists=lambda _p: True))
        assert hits == sorted(p for p in expected if "\\proj5\\" in p)
        assert {r.path for r in search.search_names(store, "proj7", exists=lambda _p: True)} >= {"C:\\work\\proj7"}


def test_nested_roots_only_track_the_nested_subtree():
    """自定义根落在 C:\\ 里时，只记自定义根下面的目录，C:\\ 其余目录一个不记。"""
    nested = indexer._nested_roots(["C:\\", "C:\\Work"])
    assert nested == (os.path.normcase("C:\\Work\\"),)
    assert indexer._nested_roots(["C:\\", "D:\\"]) == ()

    seen = indexer._ScanSeen({}, mode="dirs", nested_roots=nested)
    for i in range(50):
        assert seen.add(f"C:\\Other\\d{i}\\a.txt")
    assert seen._dirs == set()                      # C:\ 其余部分不记
    assert seen.add("C:\\Work\\deck\\a.pptx")
    assert seen.add("C:\\Work\\deck\\b.pptx")
    assert seen.add("C:\\Other\\x\\c.txt")          # 走出嵌套子树
    assert not seen.add("C:\\Work\\deck\\a.pptx")   # 第二个根再走到同一目录：整批重复
    assert not seen.add("C:\\Work\\deck\\b.pptx")
    assert len(seen) == 53


def test_name_index_spool_failure_keeps_content_index(qtbot, tmp_path, monkeypatch):
    """临时文件写不进去（磁盘满）只放弃这一轮文件名索引，内容索引照常完成。"""
    from pptx_finder.ui.index_worker import IndexWorker

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "docs"
    docs.mkdir()
    import fixtures_gen as fx

    fx.make_pptx(docs / "keep.pptx", [{"body": "磁盘满也要能搜到"}])
    (docs / "other.bin").write_bytes(b"x")

    def boom(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(namestore.NameStoreBuilder, "add", boom)
    emitted: list[dict] = []
    worker = IndexWorker(str(tmp_path / "i.db"), [str(docs)], workers=1,
                         supported_exts=(".pptx",), build_name_index=True)
    worker.finished_index.connect(emitted.append)
    worker.run()                                     # 同步跑，不起线程

    assert emitted and "error" not in emitted[0]
    assert emitted[0]["indexed"] == 1
    assert "OSError" in emitted[0]["name_index_error"]
    conn = _conn(tmp_path, "i.db")
    assert conn.execute("SELECT COUNT(*) FROM files WHERE status='ok'").fetchone()[0] == 1
    conn.close()
    assert namestore.current_data_path(namestore.MAIN) is None   # 半份索引不落盘
