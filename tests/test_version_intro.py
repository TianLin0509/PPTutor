"""P0-1 版本存在感：留版回调 → 跨线程桥 VersionBridge → UI 盾牌 + 仅首次告知。"""
from __future__ import annotations

import fixtures_gen as fx
from test_ui import StubRender, _finish_fake_task, _index, _install_fake_background_task

from pptx_finder import config
from pptx_finder.models import FileResult
import pptx_finder.ui.main_window as main_window_mod
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.version_bridge import VersionBridge
from pptx_finder.versioning.manager import VersionManager


# ---------- manager 回调 ----------
def test_snapshot_fires_callback(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "算力 集群"}])
    seen = []
    mgr = VersionManager(on_snapshot=lambda path, vid: seen.append((path, vid)))
    vid = mgr.snapshot_now(str(p))
    assert seen == [(str(p), vid)]              # 留版成功 → 回调
    seen.clear()
    assert mgr.snapshot_now(str(p)) is None     # 内容没变
    assert seen == []                            # 不回调


def test_restore_keepbottom_does_not_notify(tmp_path):
    """恢复前的自动留底 notify=False，不触发用户可见的「新版本」通知。"""
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "OLD"}])
    seen = []
    mgr = VersionManager(on_snapshot=lambda *a: seen.append(a))
    mgr.snapshot_now(str(p))
    v1 = mgr.list_versions(str(p))[0]["version_id"]
    fx.make_pptx(p, [{"body": "NEW"}])
    mgr.snapshot_now(str(p))
    seen.clear()
    mgr.restore_to(str(p), v1)                   # 恢复前留底应静默
    assert seen == []


# ---------- 跨线程桥 ----------
def test_bridge_emits_signal(qtbot):
    bridge = VersionBridge()
    got = []
    bridge.snapshotted.connect(lambda path, vid: got.append((path, vid)))
    with qtbot.waitSignal(bridge.snapshotted, timeout=500):
        bridge.emit_snapshot("C:/x.pptx", "v1")
    assert got == [("C:/x.pptx", "v1")]


def test_bridge_queues_feature_runtime_rollback(qtbot):
    bridge = VersionBridge()
    got = []
    bridge.feature_state.connect(lambda key, enabled: got.append((key, enabled)))
    with qtbot.waitSignal(bridge.feature_state, timeout=500):
        bridge.emit_feature_state("version_management", False)
    assert got == [("version_management", False)]


# ---------- 主窗盾牌 + 首次告知 ----------
class _StubVer:
    def __init__(self, docs=0, versions=None):
        self._docs = docs
        self._v = versions or []

    def list_docs(self):
        return list(range(self._docs))

    def list_versions(self, path):
        return self._v

    def is_managed(self, path):
        return True

    def restore_to(self, *a, **k):
        return True

    def export(self, *a, **k):
        return True


def test_shield_shows_count(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=3), do_index=False)
    qtbot.addWidget(win)
    win.refresh_version_shield()
    qtbot.waitUntil(lambda: not win.version_shield.isHidden(), timeout=2000)
    assert not win.version_shield.isHidden()
    assert "3" in win.version_shield.text()


def test_shield_hidden_when_zero(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=0), do_index=False)
    qtbot.addWidget(win)
    win.refresh_version_shield()
    qtbot.waitUntil(lambda: win.version_shield.isHidden(), timeout=2000)
    assert win.version_shield.isHidden()


def test_shield_refresh_counts_docs_in_background(qtbot, tmp_path, monkeypatch):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class CountingDocs(_StubVer):
        def list_docs(self):
            calls.append("list_docs")
            return [1, 2, 3]

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=CountingDocs(), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()

    win.refresh_version_shield()

    assert calls == []
    assert tasks and tasks[-1].label == "version-shield-refresh"

    _finish_fake_task(tasks[-1])

    assert calls == ["list_docs"]
    assert not win.version_shield.isHidden()
    assert "3" in win.version_shield.text()


def test_version_shield_refresh_reuses_inflight_count(qtbot, tmp_path, monkeypatch):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class CountingDocs(_StubVer):
        def list_docs(self):
            calls.append("list_docs")
            return [1, 2, 3]

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=CountingDocs(), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()

    win.refresh_version_shield()
    first_task = tasks[-1]
    win.refresh_version_shield()
    win.refresh_version_shield()

    shield_tasks = [task for task in tasks if task.label == "version-shield-refresh"]
    assert shield_tasks == [first_task]
    assert calls == []

    _finish_fake_task(first_task)

    assert calls == ["list_docs"]
    assert not win.version_shield.isHidden()
    assert "3" in win.version_shield.text()


def test_stale_version_shield_refresh_is_ignored(qtbot, tmp_path, monkeypatch):
    tasks = _install_fake_background_task(monkeypatch)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=3), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()

    win.refresh_version_shield()
    old_task = tasks[-1]
    win._version_shield_token += 1
    win.refresh_version_shield()
    new_task = tasks[-1]

    new_task.done.emit(3)
    new_task.finished.emit()
    assert "3" in win.version_shield.text()

    old_task.done.emit(1)
    old_task.finished.emit()
    assert "3" in win.version_shield.text()


def test_version_shield_refresh_debounced_on_snapshot(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_VERSION_SHIELD_REFRESH_MS", 20, raising=False)

    class CountingDocs(_StubVer):
        def __init__(self):
            super().__init__(docs=2)
            self.calls = 0

        def list_docs(self):
            self.calls += 1
            return super().list_docs()

    vm = CountingDocs()
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    qtbot.wait(10)  # drain the startup dashboard refresh so this test counts only shield refreshes
    monkeypatch.setattr(win, "_index_file_live", lambda _path: None)
    monkeypatch.setattr(win, "_maybe_show_version_intro", lambda: None)
    vm.calls = 0

    win.on_version_snapshot("C:/a.pptx", "v1")
    win.on_version_snapshot("C:/b.pptx", "v2")
    win.on_version_snapshot("C:/c.pptx", "v3")

    assert vm.calls == 0
    qtbot.waitUntil(lambda: vm.calls == 1, timeout=1000)


def test_version_shield_refresh_uses_restartable_timer(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_VERSION_SHIELD_REFRESH_MS", 20, raising=False)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    run_tokens: list[int] = []
    monkeypatch.setattr(win, "_run_version_shield_refresh", lambda token: run_tokens.append(token))

    win._schedule_version_shield_refresh()
    win._schedule_version_shield_refresh()
    win._schedule_version_shield_refresh()

    qtbot.waitUntil(lambda: bool(run_tokens), timeout=1000)
    qtbot.wait(80)

    assert run_tokens == [win._version_shield_token]


def test_current_file_snapshot_refreshes_detail_debounced(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_DETAIL_UPDATE_DELAY_MS", 20, raising=False)
    monkeypatch.setattr(MainWindow, "_DETAIL_DOT_DELAY_MS", 20, raising=False)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(win, "_index_file_live", lambda _path: None)
    monkeypatch.setattr(win, "_maybe_show_version_intro", lambda: None)
    monkeypatch.setattr(win, "_schedule_version_shield_refresh", lambda: None)
    win._cur = FileResult(
        file_id=1,
        path="C:/current.pptx",
        name="current.pptx",
        ext=".pptx",
        mtime=1,
        size=1,
        page_count=1,
        status="ok",
        score=1,
        name_hit=False,
    )
    detail_calls = []
    dot_calls = []
    monkeypatch.setattr(win, "_update_detail", lambda **kwargs: detail_calls.append(kwargs.get("force")))
    monkeypatch.setattr(win, "_refresh_detail_dot", lambda: dot_calls.append("dot"))

    win.on_version_snapshot("C:/current.pptx", "v1")
    win.on_version_snapshot("C:/current.pptx", "v2")
    win.on_version_snapshot("C:/current.pptx", "v3")

    assert detail_calls == []
    assert dot_calls == []
    qtbot.waitUntil(
        lambda: detail_calls == [True] and dot_calls == ["dot"],
        timeout=1000,
    )


def test_first_snapshot_intro_once(qtbot, tmp_path):
    (config.data_dir() / "version_intro.flag").unlink(missing_ok=True)  # 清首次标记
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=1), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(30)
    win.on_version_snapshot("C:/x.pptx", "v1")
    assert getattr(win, "_spotlight", None) is not None     # 首次 → 聚光灯
    assert "自动给你改过的 PPT 留了底" in win._spotlight._text
    assert "任意历史版本" in win._spotlight._text
    assert not any(token in win._spotlight._text for token in ("鏁", "鍚", "锛", "鈥", "馃", "\ufffd"))
    win._spotlight = None
    win.on_version_snapshot("C:/y.pptx", "v2")
    assert getattr(win, "_spotlight", None) is None         # 之后永久静默


# ---------- P0-2 详情按钮红点 ----------
def test_detail_dot_shows_on_versions(qtbot, tmp_path):
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: not win._detail_dot.isHidden(), timeout=1000)  # 有版本 + 未看版本 Tab → 红点
    win.detail_panel.tabs.setCurrentIndex(win.detail_panel.version_tab_index())  # 切到「版本」Tab
    assert win._detail_dot.isHidden()             # 已在看版本 → 红点隐藏


def test_detail_dot_hidden_no_versions(qtbot, tmp_path):
    vm = _StubVer(docs=0, versions=[])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert win._detail_dot.isHidden()             # 无版本 → 无红点


# ---------- P0-3 首次搜索框 coachmark ----------
def test_search_coach_targets_searchbox(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(20)
    win._welcome = None
    win._show_search_coach()
    assert getattr(win, "_spotlight", None) is not None
    assert win._spotlight._target is win.search_box
    assert "输入你 PPT 里写过的字" in win._spotlight._text
    assert "在哪个文件、第几页" in win._spotlight._text
    assert not any(token in win._spotlight._text for token in ("鏁", "鍚", "锛", "鈥", "馃", "\ufffd"))


def test_show_spotlight_replaces_old(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(20)
    win._show_spotlight(win.search_box, "a")
    first = win._spotlight
    tab_bar = win.detail_panel.tabs.tabBar()
    win._show_spotlight(tab_bar, "b")             # 弹新的应替换旧的
    assert win._spotlight is not first
    assert win._spotlight._target is tab_bar


# ---------- P1-1 索引完成庆祝 ----------
def test_index_celebration_once(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(20)
    win._on_index_done({})
    assert win._index_celebrated is True
    qtbot.waitUntil(lambda: "PPT" in win._toast_label.text(), timeout=2000)
    assert "PPT" in win._toast_label.text()


# ---------- P1-2 空白起步引导 ----------
def test_start_hint_when_no_recent(qtbot, tmp_path):
    from pptx_finder import db
    conn = db.connect(tmp_path / "empty.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._do_search()                              # 空查询 + 无最近文件 → 起步引导
    qtbot.waitUntil(lambda: not win.empty_hint.isHidden(), timeout=2000)
    assert not win.empty_hint.isHidden()
    assert "整理" in win._empty_query_label.text()


# ---------- P1-3 恢复确认 ----------
def test_restore_requires_confirm(qtbot, tmp_path, monkeypatch):
    calls = []
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    vm.restore_to = lambda *a, **k: (calls.append(a), True)[1]
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(win, "_confirm_restore", lambda: False)
    win._act_restore_version("C:/nonexist.pptx", "v1")   # 取消 → 不恢复
    assert calls == []
    monkeypatch.setattr(win, "_confirm_restore", lambda: True)
    win._act_restore_version("C:/nonexist.pptx", "v1")   # 确认 → 后台恢复（不阻塞主线程）
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=3000)              # 等后台线程执行恢复
    qtbot.waitUntil(lambda: "✓" in win._toast_label.text(), timeout=3000)  # 等结果回主线程刷新


# ---------- P1-4 详情首开提示 ----------
def test_restore_file_probe_runs_in_background(qtbot, tmp_path, monkeypatch):
    import builtins

    probes = []
    restores = []
    queued = []
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    vm.restore_to = lambda *a, **k: (restores.append(a), True)[1]
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(win, "_confirm_restore", lambda: True)

    def fake_exists(path):
        probes.append(("exists", path))
        return True

    class FakeOpen:
        def __enter__(self):
            probes.append(("open-enter", None))
            return self

        def __exit__(self, *_args):
            probes.append(("open-exit", None))
            return False

    def fake_open(path, mode):
        probes.append(("open", path, mode))
        return FakeOpen()

    def fake_run_bg(fn, on_done=None, label=""):
        queued.append((fn, on_done, label))
        return True

    monkeypatch.setattr(main_window_mod.os.path, "exists", fake_exists)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    win._act_restore_version("C:/maybe-open.pptx", "v1")

    assert probes == []
    assert restores == []
    assert queued and queued[-1][2] == "restore"

    result = queued[-1][0]()

    assert result is True
    assert probes[:2] == [("exists", "C:/maybe-open.pptx"), ("open", "C:/maybe-open.pptx", "r+b")]
    assert restores == [("C:/maybe-open.pptx", "v1")]


def test_restore_skips_confirm_when_heavy_op_active(qtbot, tmp_path, monkeypatch):
    confirms = []
    queued = []
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win._active_heavy_op = "open"
    monkeypatch.setattr(win, "_confirm_restore", lambda: confirms.append("confirm") or True)
    monkeypatch.setattr(win, "_run_bg", lambda *args, **kwargs: queued.append((args, kwargs)) or False)

    win._act_restore_version("C:/deck-a.pptx", "v1")

    assert confirms == []
    assert queued == []


def test_detail_first_open_hint(qtbot, tmp_path):
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    win.detail_panel.tabs.setCurrentIndex(win.detail_panel.version_tab_index())  # 首次切到版本 Tab + 有版本 → 提示
    qtbot.waitUntil(lambda: "历史版本" in win._toast_label.text(), timeout=1000)


def test_detail_first_open_hint_checks_versions_in_background(qtbot, tmp_path, monkeypatch):
    tasks = []
    calls = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                if value is None:
                    callback()
                else:
                    callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class CountingVersions:
        def list_versions(self, path):
            calls.append(path)
            return [{"version_id": "v1", "ts": 1, "page_count": 3}]

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win.detail_panel.tabs.setCurrentIndex(win.detail_panel.version_tab_index())  # 「打开方式」= 切到版本 Tab

    win._maybe_hint_detail_versions()

    assert calls == []
    assert tasks and tasks[-1].label == "detail-hint-check"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)
    tasks[-1].finished.emit()

    assert calls == ["C:/deck-a.pptx"]
    assert "历史版本" in win._toast_label.text()


def test_detail_first_open_hint_reuses_inflight_check(qtbot, tmp_path, monkeypatch):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class CountingVersions:
        def list_versions(self, path):
            calls.append(path)
            return [{"version_id": "v1", "ts": 1, "page_count": 3}]

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.tabs.setCurrentIndex(win.detail_panel.version_tab_index())  # 「打开方式」= 切到版本 Tab
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    tasks.clear()

    win._maybe_hint_detail_versions()
    first_task = tasks[-1]
    win._maybe_hint_detail_versions()
    win._maybe_hint_detail_versions()

    hint_tasks = [task for task in tasks if task.label == "detail-hint-check"]
    assert hint_tasks == [first_task]
    assert calls == []

    _finish_fake_task(first_task)

    assert calls == ["C:/deck-a.pptx"]
    assert "历史版本" in win._toast_label.text()


def test_detail_first_open_hint_new_path_allows_new_check(qtbot, tmp_path, monkeypatch):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class VersionsByPath:
        def list_versions(self, path):
            calls.append(path)
            if path.endswith("deck-b.pptx"):
                return [{"version_id": "v1", "ts": 1, "page_count": 3}]
            return []

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=VersionsByPath(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.tabs.setCurrentIndex(win.detail_panel.version_tab_index())  # 「打开方式」= 切到版本 Tab
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    tasks.clear()

    win._maybe_hint_detail_versions()
    old_task = tasks[-1]
    win._cur = FileResult(file_id=2, path="C:/deck-b.pptx", name="deck-b.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win._maybe_hint_detail_versions()
    new_task = tasks[-1]

    assert new_task is not old_task

    _finish_fake_task(new_task)
    assert "历史版本" in win._toast_label.text()

    _finish_fake_task(old_task)
    assert calls == ["C:/deck-b.pptx", "C:/deck-a.pptx"]
    assert "历史版本" in win._toast_label.text()


def test_detail_first_open_without_versions_does_not_consume_hint(qtbot, tmp_path, monkeypatch):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class VersionsByPath:
        def list_versions(self, path):
            calls.append(path)
            if path.endswith("deck-b.pptx"):
                return [{"version_id": "v1", "ts": 1, "page_count": 3}]
            return []

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=VersionsByPath(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.tabs.setCurrentIndex(win.detail_panel.version_tab_index())  # 「打开方式」= 切到版本 Tab
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    tasks.clear()

    win._maybe_hint_detail_versions()
    first_task = tasks[-1]
    _finish_fake_task(first_task)

    assert "历史版本" not in win._toast_label.text()

    win._cur = FileResult(file_id=2, path="C:/deck-b.pptx", name="deck-b.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win._maybe_hint_detail_versions()
    assert tasks[-1] is not first_task
    _finish_fake_task(tasks[-1])

    assert calls == ["C:/deck-a.pptx", "C:/deck-b.pptx"]
    assert "历史版本" in win._toast_label.text()
