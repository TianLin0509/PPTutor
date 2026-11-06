"""变更流水账：用真实 NTFS 卷端到端验证（非 NTFS 或日志未开的机器自动跳过）。"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from pptx_finder import usn_journal as usn
from pptx_finder.versioning.usn_feed import UsnFeed, load_state, save_state
from pptx_finder.versioning.watcher import _Handler

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _work_dir(tmp_path) -> Path:
    # pytest 的 tmp_path 在系统临时目录下，而临时目录恰恰被排除；放到仓库旁边的真实目录
    base = Path(__file__).resolve().parent.parent / ".usn-test-work" / tmp_path.name
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture()
def work(tmp_path):
    base = _work_dir(tmp_path)
    yield base
    shutil.rmtree(base.parent, ignore_errors=True)


def _feed(root, state, known=None):
    events = {"saved": [], "moved": [], "content": [], "removed": [], "dirs": []}
    handler = _Handler(
        lambda p: events["saved"].append(p),
        lambda a, b: events["moved"].append((a, b)),
        [root],
        lambda p: events["content"].append(p),
        lambda p: events["removed"].append(p),
        (".pptx", ".docx", ".pdf"),
        lambda d: events["dirs"].append(d),
    )
    feed = UsnFeed(handler, [root], state_path=str(state), poll_sec=0.1, known_rows=known)
    if root not in feed.open():
        pytest.skip(f"no usable journal on {root}")
    return feed, events, handler


def _wait(cond, timeout=12.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_new_pptx_reaches_handler_without_any_dirwatch(work, tmp_path):
    root = usn.volume_root(str(work))
    feed, events, handler = _feed(root, tmp_path / "state.json")
    feed.start()
    try:
        target = work / "新建 汇报.pptx"
        target.write_bytes(b"x" * 100)
        assert _wait(lambda: any(os.path.samefile(p, target) for p in events["saved"] if os.path.exists(p)))
    finally:
        handler.stop()
        feed.stop()


def test_changes_while_stopped_are_replayed_from_saved_position(work, tmp_path):
    """程序关着时存的文件：下次启动接着读，不靠全盘扫描。"""
    root = usn.volume_root(str(work))
    state = tmp_path / "state.json"
    info = usn.VolumeJournal(root).query()
    save_state(str(state), {root: {"id": info.journal_id, "usn": info.next_usn}})
    target = work / "离线期间.pptx"
    target.write_bytes(b"y" * 10)
    feed, events, handler = _feed(root, state)
    feed.start()
    try:
        assert _wait(lambda: any(os.path.basename(p) == target.name for p in events["saved"]))
    finally:
        handler.stop()
        feed.stop()
    # 落盘位置滞后于已读位置，防抖中的事件不会因退出而永远丢失
    assert int(load_state(str(state))[root]["usn"]) == info.next_usn


def test_delete_found_by_reconciling_known_rows(work, tmp_path):
    target = work / "将被删除.pptx"
    target.write_bytes(b"z")
    st = target.stat()
    known = lambda d, rec: [(str(target), st.st_size, st.st_mtime)]  # noqa: E731
    root = usn.volume_root(str(work))
    feed, events, handler = _feed(root, tmp_path / "state.json", known)
    feed.start()
    try:
        target.unlink()
        assert _wait(lambda: str(target) in events["removed"])
    finally:
        handler.stop()
        feed.stop()


def test_rename_keeps_identity_for_version_history(work, tmp_path):
    source = work / "旧名字.pptx"
    source.write_bytes(b"content")
    st = source.stat()
    known = lambda d, rec: [(str(source), st.st_size, st.st_mtime)]  # noqa: E731
    root = usn.volume_root(str(work))
    feed, events, handler = _feed(root, tmp_path / "state.json", known)
    feed.start()
    try:
        time.sleep(0.4)
        dest = work / "新名字.pptx"
        source.rename(dest)
        assert _wait(lambda: any(os.path.basename(b) == dest.name for _a, b in events["moved"]))
    finally:
        handler.stop()
        feed.stop()


def test_lost_position_reports_gap_instead_of_silence(work, tmp_path):
    root = usn.volume_root(str(work))
    state = tmp_path / "state.json"
    save_state(str(state), {root: {"id": 1, "usn": 1}})
    gaps = []
    handler = _Handler(lambda p: None, None, [root], None, None, (".pptx",), None)
    feed = UsnFeed(handler, [root], state_path=str(state), on_gap=lambda r, why: gaps.append(r))
    if root not in feed.open():
        pytest.skip("no journal")
    feed.stop()
    handler.stop()
    assert gaps == [root]


def test_temp_directory_is_skipped_even_outside_appdata(monkeypatch, tmp_path):
    fake_temp = str(tmp_path / "MyTemp")
    monkeypatch.setenv("TEMP", fake_temp)
    monkeypatch.setenv("TMP", fake_temp)
    handler = _Handler(lambda p: None, None, ["C:\\"], None, None, (".pptx",), None)
    assert handler.skip_path(os.path.join(fake_temp, "a.pptx"))
    assert not handler.skip_path("Z:\normal\a.pptx")


def test_root_inside_temp_is_respected(monkeypatch, tmp_path):
    fake_temp = str(tmp_path)
    monkeypatch.setenv("TEMP", fake_temp)
    monkeypatch.setenv("TMP", fake_temp)
    root = str(tmp_path / "picked")
    handler = _Handler(lambda p: None, None, [root], None, None, (".pptx",), None)
    assert not handler.skip_path(os.path.join(root, "a.pptx"))


def _rows(*paths):
    infos = [(str(p), p.stat().st_size, p.stat().st_mtime) for p in paths]

    def known(directory, recursive):
        prefix = directory.rstrip(os.sep) + os.sep
        return [r for r in infos if r[0].startswith(prefix)
                and (recursive or os.sep not in r[0][len(prefix):])]
    return known


def test_shift_delete_of_cold_folder_reports_every_known_file(work, tmp_path):
    """从没动过的文件夹被整个删除：子文件和目录路径都已查不到，也要在父目录对上账。"""
    folder = work / "冷门" / "子目录"
    folder.mkdir(parents=True)
    target = folder / "旧稿.pptx"
    target.write_bytes(b"a")
    root = usn.volume_root(str(work))
    feed, events, handler = _feed(root, tmp_path / "state.json", _rows(target))
    time.sleep(0.3)
    feed.start()
    try:
        time.sleep(0.5)
        shutil.rmtree(work / "冷门")
        assert _wait(lambda: str(target) in events["removed"])
    finally:
        handler.stop()
        feed.stop()


def test_rename_of_cold_folder_keeps_version_identity(work, tmp_path):
    folder = work / "旧目录"
    folder.mkdir()
    target = folder / "汇报.pptx"
    target.write_bytes(b"a")
    root = usn.volume_root(str(work))
    feed, events, handler = _feed(root, tmp_path / "state.json", _rows(target))
    feed.start()
    try:
        time.sleep(0.5)
        folder.rename(work / "新目录")
        expected = str(work / "新目录" / "汇报.pptx")
        assert _wait(lambda: (str(target), expected) in [
            (a, b) for a, b in events["moved"]])
    finally:
        handler.stop()
        feed.stop()


def test_saved_pptx_is_searchable_through_real_window_pipeline(qtbot, work, tmp_path):
    """真实链路：流水账 → 共享监听 → 桥接信号 → 主窗口 → 写入索引库。"""
    from pptx_finder import app as app_mod, db
    from pptx_finder.config import resource_path
    from pptx_finder.ui.main_window import MainWindow
    from pptx_finder.ui.version_bridge import VersionBridge

    from test_live_index import StubRender

    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    root = usn.volume_root(str(work))
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False, roots=[root])
    qtbot.addWidget(win)
    bridge = VersionBridge()
    bridge.content_changed.connect(win.on_content_changed)
    bridge.journal_gap.connect(win.on_journal_gap)

    class Manager:
        pass

    runtime = app_mod._FeatureRuntime(win, Manager(), bridge)
    runtime.start()
    try:
        status = runtime.watcher_status()
        if not any(row.get("backend") == "usn" for row in status):
            pytest.skip("no usable journal")
        target = work / "刚保存的汇报.pptx"
        shutil.copyfile(resource_path("assets/blank_16x9.pptx"), target)
        qtbot.waitUntil(lambda: db.get_file_by_path(conn, str(target)) is not None, timeout=20000)
    finally:
        runtime.stop()
