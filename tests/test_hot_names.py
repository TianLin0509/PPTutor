"""文件名热层（2026-11-21）：新建的任意文件秒级可搜，不再被 churn 目录挤掉。

真机现象：PPT 内容 4 秒入库，但「全部文件」里新建的 txt/xlsx/md 5 分钟后仍搜不到。
根因是增量层按脏目录整目录重列：脏目录集合 2 万封顶、满了就不收，取出顺序随机，
增量层十几万条时一轮 70 多秒，全盘扫描期间整轮暂停。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from pptx_finder import db, hot_names, namestore, search
from pptx_finder.versioning.watcher import _Handler


def _search(name: str) -> list[str]:
    stores = [s for s in (namestore.open_store(k) for k in namestore.KINDS) if s]
    try:
        return [r.name for r in search.search_names(stores, name)]
    finally:
        for s in stores:
            s.close()


# ---------- 纯逻辑 ----------

def test_hot_names_dedupes_and_evicts_oldest():
    hot = hot_names.HotNames(max_entries=3)
    for name in ("a", "b", "c", "a", "d"):
        hot.note(fr"C:\x\{name}.txt")
    assert [p for _k, p, _s in hot.snapshot()] == [
        r"C:\x\c.txt", r"C:\x\a.txt", r"C:\x\d.txt"]
    assert hot.evicted == 1
    hot.note(r"C:\x\~$lock.docx")       # Office 锁文件不进
    assert len(hot) == 3


def test_drop_folded_keeps_entries_touched_again():
    hot = hot_names.HotNames()
    hot.note(r"C:\x\a.txt")
    hot.note(r"C:\x\b.txt")
    folded = [(k, s) for k, _p, s in hot.snapshot()]
    hot.note(r"C:\x\b.txt")              # 并入期间又改了一次：必须留下
    assert hot.drop_folded(folded) == 1
    assert [p for _k, p, _s in hot.snapshot()] == [r"C:\x\b.txt"]


def test_hot_layer_makes_new_file_searchable_and_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    f = tmp_path / "20261121-刚建的表格.xlsx"
    f.write_bytes(b"x")
    hot = hot_names.HotNames()
    hot.note(str(f))
    hot.note(str(tmp_path / "已经删掉的.txt"))
    assert hot_names.write_hot_layer(hot.snapshot()) == 1
    assert _search("刚建的表格") == [f.name]
    assert hot_names.write_hot_layer(()) == 0
    assert namestore.current_data_path(namestore.HOT) is None


# ---------- 目录监听（watchdog 兜底）也逐条上报 ----------

def test_watcher_reports_each_new_file_name(tmp_path):
    names, dirs = [], []
    handler = _Handler(
        lambda _p: None, roots=[str(tmp_path)], allowed_exts=(".pptx",),
        on_other_dir_changed=dirs.append, on_name_changed=names.append,
    )
    handler._trigger(str(tmp_path / "photo.jpg"))
    handler._trigger(str(tmp_path / "deck.pptx"))
    handler._trigger(str(tmp_path / "~$deck.pptx"))

    class _Dir:
        is_directory = True
        src_path = str(tmp_path / "新文件夹")

    handler.on_created(_Dir())
    assert names == [str(tmp_path / "photo.jpg"), str(tmp_path / "deck.pptx"),
                     str(tmp_path / "新文件夹")]
    assert dirs == [str(tmp_path)]
    handler.stop()


# ---------- 主窗接线 ----------

def _window(qtbot, tmp_path, monkeypatch):
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    conn = db.connect(tmp_path / "ui.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, do_index=False, index_all_files_enabled=True)
    qtbot.addWidget(win)
    return win, conn


def test_new_file_searchable_even_when_dirty_queue_overflowed(qtbot, tmp_path, monkeypatch):
    """脏目录集合已满（真机常态）：新文件照样一秒内能搜到。"""
    from pptx_finder.ui.main_window import MainWindow

    win, conn = _window(qtbot, tmp_path, monkeypatch)
    for i in range(MainWindow._INVENTORY_DIRS_MAX + 10):
        win.note_inventory_dir_dirty(str(tmp_path / f"churn{i}"))
    assert win._inventory_dirs_overflowed
    f = tmp_path / "20261121-溢出后新建.md"
    f.write_text("x", encoding="utf-8")
    win.note_inventory_dir_dirty(str(tmp_path))          # 旧通道：被丢掉
    win.note_names_changed([str(f)])                      # 新通道：逐条收下
    qtbot.waitUntil(lambda: _search("溢出后新建") == [f.name], timeout=5000)
    conn.close()


def test_overlay_round_folds_hot_entries_then_hot_shrinks(qtbot, tmp_path, monkeypatch):
    from pptx_finder.ui.main_window import MainWindow

    win, conn = _window(qtbot, tmp_path, monkeypatch)
    f = tmp_path / "并入增量层.txt"
    f.write_text("x", encoding="utf-8")
    win.note_names_changed([str(f)])
    res = MainWindow._reconcile_inventory_dirs_sync((), win._hot_names.snapshot())
    assert res.get("error") is None and res["added"] == 1
    win._on_inventory_reconciled(res)
    assert len(win._hot_names) == 0
    qtbot.waitUntil(lambda: namestore.current_data_path(namestore.HOT) is None, timeout=5000)
    assert _search("并入增量层") == [f.name]          # 现在由增量层提供
    conn.close()


def test_dirty_dirs_are_reconciled_first_in_first_out(qtbot, tmp_path, monkeypatch):
    win, conn = _window(qtbot, tmp_path, monkeypatch)
    order = [str(tmp_path / f"d{i}") for i in range(5)]
    for d in order:
        win.note_inventory_dir_dirty(d)
    assert list(win._dirty_inventory_dirs) == order
    conn.close()


def test_disabling_all_files_clears_hot_layer(qtbot, tmp_path, monkeypatch):
    win, conn = _window(qtbot, tmp_path, monkeypatch)
    f = tmp_path / "关掉以后.txt"
    f.write_text("x", encoding="utf-8")
    win.note_names_changed([str(f)])
    qtbot.waitUntil(lambda: namestore.current_data_path(namestore.HOT) is not None, timeout=5000)
    win.apply_feature_flags(index_all_files_enabled=False)
    qtbot.waitUntil(lambda: namestore.current_data_path(namestore.HOT) is None, timeout=5000)
    win.note_names_changed([str(f)])
    assert len(win._hot_names) == 0
    conn.close()


# ---------- 真实链路：NTFS 流水账 → 桥接 → 主窗 → 热层 → 搜索 ----------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_new_any_file_searchable_through_real_journal_pipeline(qtbot, tmp_path, monkeypatch):
    from pptx_finder import app as app_mod, usn_journal as usn
    from pptx_finder.ui.version_bridge import VersionBridge

    from test_live_index import StubRender
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    work = Path(__file__).resolve().parent.parent / ".usn-test-work" / tmp_path.name
    work.mkdir(parents=True, exist_ok=True)
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    root = usn.volume_root(str(work))
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False, roots=[root],
                     index_all_files_enabled=True)
    qtbot.addWidget(win)
    bridge = VersionBridge()
    bridge.names_changed.connect(win.note_names_changed)

    class Manager:
        pass

    runtime = app_mod._FeatureRuntime(win, Manager(), bridge)
    runtime.start()
    try:
        if not any(row.get("backend") == "usn" for row in runtime.watcher_status()):
            pytest.skip("no usable journal")
        sub = work / "新建的子目录"
        sub.mkdir()
        target = sub / f"热层探针{os.getpid()}.xlsx"
        target.write_bytes(b"x")
        t0 = time.monotonic()
        qtbot.waitUntil(lambda: target.name in _search("热层探针"), timeout=15000)
        assert time.monotonic() - t0 < 10
    finally:
        runtime.stop()
        conn.close()
        shutil.rmtree(work.parent, ignore_errors=True)


# ---------- 2026-11-21 Codex 审查补的四条 ----------

def test_restart_keeps_unfolded_hot_entries(qtbot, tmp_path, monkeypatch):
    """上次没来得及并入增量层的热层条目，重启后第一次重写不能把它们冲掉。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    old = tmp_path / "重启前新建.txt"
    old.write_text("x", encoding="utf-8")
    hot = hot_names.HotNames()
    hot.note(str(old))
    hot_names.write_hot_layer(hot.snapshot())
    win, conn = _window(qtbot, tmp_path, monkeypatch)
    new = tmp_path / "重启后新建.txt"
    new.write_text("x", encoding="utf-8")
    win.note_names_changed([str(new)])
    qtbot.waitUntil(lambda: new.name in _search("重启后新建"), timeout=5000)
    assert old.name in _search("重启前新建")
    conn.close()


def test_fold_refreshes_metadata_already_in_overlay(tmp_path, monkeypatch):
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    f = tmp_path / "会变大.bin"
    f.write_bytes(b"x")
    MainWindow._reconcile_inventory_dirs_sync((str(tmp_path),))
    f.write_bytes(b"x" * 5000)
    hot = hot_names.HotNames()
    hot.note(str(f))
    MainWindow._reconcile_inventory_dirs_sync((), hot.snapshot())
    st = namestore.open_store(namestore.OVERLAY)
    try:
        sizes = [st.entry(i)[2] for i in range(st.count) if st.entry(i)[1] == f.name]
    finally:
        st.close()
    assert sizes == [5000]


def test_transient_stat_failure_keeps_hot_entry(tmp_path, monkeypatch):
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    f = tmp_path / "被占用.pptx"
    f.write_bytes(b"x")
    hot = hot_names.HotNames()
    hot.note(str(f))

    def busy(_path):
        raise hot_names.StatUnavailable("sharing violation")

    monkeypatch.setattr(hot_names, "stat_entry", busy)
    res = MainWindow._reconcile_inventory_dirs_sync((), hot.snapshot())
    assert res["hot_folded"] == []
    assert hot_names.write_hot_layer(hot.snapshot()) == 1   # 名字仍然可搜


def test_hot_write_failure_retries_without_new_events(qtbot, tmp_path, monkeypatch):
    win, conn = _window(qtbot, tmp_path, monkeypatch)
    f = tmp_path / "写失败后重试.txt"
    f.write_text("x", encoding="utf-8")
    real = hot_names.write_hot_layer
    calls = []

    def flaky(snap):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk busy")
        return real(snap)

    monkeypatch.setattr(hot_names, "write_hot_layer", flaky)
    win.note_names_changed([str(f)])
    qtbot.waitUntil(lambda: _search("写失败后重试") == [f.name], timeout=8000)
    assert len(calls) >= 2
    conn.close()
