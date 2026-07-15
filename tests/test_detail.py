"""07 详情面板：版本时间线 + 大纲 + 元信息。"""
from __future__ import annotations

import fixtures_gen as fx
import pptx_finder.ui.main_window as main_window_mod
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileDialog, QLabel, QPushButton, QWidget
from test_ui import PendingSearchWorker, StubRender, _index

from pptx_finder import db, indexer
from pptx_finder.models import FileResult
from pptx_finder.ui import theme
from pptx_finder.ui.detail_panel import DetailPanel
from pptx_finder.ui.main_window import MainWindow


def _fr(path="C:/a.pptx", page_count=7, size=2 << 20):
    return FileResult(file_id=1, path=path, name="a.pptx", ext=".pptx", mtime=0,
                      size=size, page_count=page_count, status="ok", score=1, name_hit=False)


class StubVerMgr:
    def __init__(self, versions=None, managed=True):
        self._v = versions or []
        self._m = managed

    def list_versions(self, path):
        return self._v

    def is_managed(self, path):
        return self._m

    def restore_to(self, p, v, dest=None):
        return True

    def export(self, p, v, d):
        return True


def test_page_titles(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "x.pptx", [{"body": "第一页标题\n内容"}, {"body": "第二页\n更多"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    titles = db.page_titles(conn, fid)
    assert len(titles) == 2
    assert titles[0][0] == 1


def test_detail_meta_shows_pages(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    dp.update_for(_fr(page_count=12), versions=[])
    assert "12" in dp._meta_label.text()


def test_detail_slim_button_emits_selected_path(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    fired = []
    dp.slim_requested.connect(lambda path: fired.append(path))
    dp.update_for(_fr(path="C:/deck-a.pptx"), versions=[])

    dp._slim_btn.click()

    assert fired == ["C:/deck-a.pptx"]
    dp.clear_selection()
    assert dp._slim_btn.isEnabled() is False


def test_detail_slim_button_follows_file_action_enabled(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    fired = []
    dp.slim_requested.connect(lambda path: fired.append(path))
    dp.update_for(_fr(path="C:/deck-a.pptx"), versions=[])

    dp.set_file_actions_enabled(False)
    assert dp._slim_btn.isEnabled() is False
    dp._slim_btn.click()
    assert fired == []

    dp.set_file_actions_enabled(True)
    assert dp._slim_btn.isEnabled() is True
    dp._slim_btn.click()
    assert fired == ["C:/deck-a.pptx"]

    dp.update_for(_fr(path="C:/deck-a.pdf"), versions=[])
    dp.set_file_actions_enabled(True)
    assert dp._slim_btn.isEnabled() is False


def test_detail_close_button_has_large_click_target(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    btn = dp.findChild(QPushButton, "dtClose")

    assert btn.text() == "×"
    assert btn.width() >= 32
    assert btn.height() >= 32


def test_detail_version_nodes(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    versions = [{"version_id": "v3", "ts": 3000, "page_count": 24},
                {"version_id": "v2", "ts": 2000, "page_count": 22}]
    dp.update_for(_fr(), versions)
    assert len(dp._version_nodes) == 2


def test_detail_version_node_shows_summary_preview_and_emits_request(qtbot, tmp_path):
    image = tmp_path / "version.png"
    pm = QPixmap(48, 27)
    pm.fill(Qt.red)
    assert pm.save(str(image))

    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    fired = []
    dp.preview_requested.connect(lambda version_id: fired.append(version_id))

    dp.update_for(
        _fr(),
        [{"version_id": "v9", "ts": 3000, "page_count": 24, "changed": "summary-one", "thumb_path": str(image)}],
    )

    assert any("summary-one" in label.text() for label in dp.findChildren(QLabel, "verChanged"))
    preview_labels = dp.findChildren(QLabel, "verPreview")
    assert len(preview_labels) == 1
    pixmap = preview_labels[0].pixmap()
    assert pixmap is not None and not pixmap.isNull()

    buttons = dp.findChildren(QPushButton, "verPreviewBtn")
    assert len(buttons) == 1
    buttons[0].click()
    assert fired == ["v9"]


def test_detail_no_version_no_nodes(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    dp.update_for(_fr(), versions=[])
    assert len(dp._version_nodes) == 0
    # 新文案：无版本提示「改存即留」，不再提「在设置里加目录」
    assert "无需任何设置" in dp._version_box.itemAt(0).widget().text()


def test_main_window_version_export_appends_pptx_extension(qtbot, tmp_path, monkeypatch):
    calls = []
    queued = []

    class ExportMgr(StubVerMgr):
        def export(self, p, v, d):
            calls.append((p, v, d))
            return True

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=ExportMgr(), do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("C:/detail-export", ""))

    def fake_run_bg(fn, on_done=None, label=""):
        queued.append((fn, on_done, label))
        return True

    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    win._act_export_version("C:/deck-a.pptx", "v1")

    assert queued and queued[-1][2] == "export"
    assert queued[-1][0]() is True
    assert calls == [("C:/deck-a.pptx", "v1", "C:/detail-export.pptx")]


def test_main_window_version_export_skips_save_dialog_when_heavy_op_active(qtbot, tmp_path, monkeypatch):
    dialogs = []
    queued = []
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    win._active_heavy_op = "open"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: dialogs.append(args) or ("C:/out", ""))
    monkeypatch.setattr(win, "_run_bg", lambda *args, **kwargs: queued.append((args, kwargs)) or False)

    win._act_export_version("C:/deck-a.pptx", "v1")

    assert dialogs == []
    assert queued == []


def test_main_window_version_export_skips_save_dialog_when_search_pending(qtbot, tmp_path, monkeypatch):
    dialogs = []
    queued = []
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    win._search_pending_req = 42
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: dialogs.append(args) or ("C:/out", ""))
    monkeypatch.setattr(win, "_run_bg", lambda *args, **kwargs: queued.append((args, kwargs)) or False)

    win._act_export_version("C:/deck-a.pptx", "v1")

    assert dialogs == []
    assert queued == []


def test_main_window_version_restore_skips_confirm_when_search_pending(qtbot, tmp_path, monkeypatch):
    confirms = []
    queued = []
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    win._search_pending_req = 42
    monkeypatch.setattr(win, "_confirm_restore", lambda: confirms.append("confirm") or True)
    monkeypatch.setattr(win, "_run_bg", lambda *args, **kwargs: queued.append((args, kwargs)) or True)

    win._act_restore_version("C:/deck-a.pptx", "v1")

    assert confirms == []
    assert queued == []


def test_main_window_version_restore_loads_diff_before_confirm(qtbot, tmp_path, monkeypatch):
    tasks = []
    confirms = []
    queued = []
    calls = []
    diff = {"lines": ["文本改动 1 页：P1。"]}

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

    class DiffMgr(StubVerMgr):
        def describe_version_diff(self, version_id):
            calls.append(("diff", version_id))
            return diff

        def restore_to(self, p, v, dest=None):
            calls.append(("restore", p, v, dest))
            return True

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=DiffMgr(), do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(win, "_confirm_restore", lambda got=None: confirms.append(got) or True)
    monkeypatch.setattr(win, "_run_bg", lambda fn, on_done=None, label="": queued.append((fn, on_done, label)) or True)
    tasks.clear()

    win._act_restore_version("C:/deck-a.pptx", "v1")

    assert len(tasks) == 1
    assert tasks[0].label == "version-restore-diff"
    result = tasks[0].fn()
    tasks[0].done.emit(result)
    assert confirms == [diff]
    assert queued and queued[-1][2] == "restore"
    assert queued[-1][0]() is True
    tasks[0].finished.emit()
    assert ("C:/deck-a.pptx", "v1") not in win._restore_diff_inflight
    assert calls == [("diff", "v1"), ("restore", "C:/deck-a.pptx", "v1", None)]


def test_detail_version_actions_disabled_during_search_pending(qtbot, tmp_path):
    vm = StubVerMgr(versions=[{"version_id": "v1", "ts": 1000, "page_count": 5}], managed=True)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win._toggle_detail()
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: len(win.detail_panel._version_nodes) >= 1, timeout=1000)
    buttons = [
        b for b in win.detail_panel.findChildren(QPushButton)
        if b.text() in ("恢复", "导出")
    ]
    assert buttons and all(b.isEnabled() for b in buttons)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    assert pending.requests
    assert all(not b.isEnabled() for b in buttons)
    req_id, query, _mode = pending.requests[-1]

    win._on_search_done(req_id, query, [], 12.0, "boom")

    assert all(b.isEnabled() for b in buttons)


def test_detail_outline_click_emits_page(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    fired = []
    dp.page_requested.connect(lambda p: fired.append(p))
    dp.set_outline([(1, "封面"), (2, "目录"), (3, "正文")])
    dp._outline_box.itemAt(1).widget().click()
    assert fired == [2]


def test_mainwindow_detail_toggle(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win.detail_panel.isHidden()
    win._toggle_detail()
    assert not win.detail_panel.isHidden()


def test_mainwindow_opens_slim_window(qtbot, tmp_path, monkeypatch):
    opened = []
    deck = tmp_path / "deck-a.pptx"
    fx.make_pptx(deck, [{"body": "瘦身入口"}])

    class FakeSlimWindow(QWidget):
        def __init__(self, tok, path, parent=None):
            super().__init__(parent)
            self._path = path
            self._closing = False
            opened.append(path)

        def show(self):
            super().show()

        def isVisible(self):
            return True

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    import pptx_finder.ui.slim_window as slim_window_mod

    monkeypatch.setattr(slim_window_mod, "SlimWindow", FakeSlimWindow)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win._open_slim_window(str(deck))
    win._open_slim_window(str(deck))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1500)
    assert opened == [str(deck)]
    assert len(win._slim_windows) == 1


def test_mainwindow_blocks_slim_window_when_source_missing(qtbot, tmp_path, monkeypatch):
    opened = []
    toasts = []

    class FakeSlimWindow(QWidget):
        def __init__(self, tok, path, parent=None):
            super().__init__(parent)
            opened.append(path)

    import pptx_finder.ui.slim_window as slim_window_mod

    monkeypatch.setattr(slim_window_mod, "SlimWindow", FakeSlimWindow)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._toast = lambda msg, ms=1600: toasts.append(msg)

    win._open_slim_window(str(tmp_path / "missing.pptx"))

    qtbot.waitUntil(lambda: bool(toasts), timeout=1500)
    assert opened == []
    assert "无法瘦身" in toasts[-1]


def test_mainwindow_slim_path_probe_never_runs_on_ui_thread(qtbot, tmp_path, monkeypatch):
    probes = []
    tasks = []

    def slow_exists(path):
        probes.append(path)
        return False

    def fake_run_bg(fn, on_done=None, label=""):
        tasks.append((fn, on_done, label))
        return True

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(main_window_mod.os.path, "exists", slow_exists)
    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    win._open_slim_window("Z:/offline/deck.pptx")

    assert main_window_mod.os.path.abspath("Z:/offline/deck.pptx") not in probes
    assert tasks and tasks[-1][2] == "ppt-slim-open"


def test_mainwindow_blocks_slim_window_during_pending_search(qtbot, tmp_path, monkeypatch):
    opened = []
    toasts = []
    deck = tmp_path / "deck-a.pptx"
    fx.make_pptx(deck, [{"body": "搜索 pending"}])

    class FakeSlimWindow(QWidget):
        def __init__(self, tok, path, parent=None):
            super().__init__(parent)
            opened.append(path)

    import pptx_finder.ui.slim_window as slim_window_mod

    monkeypatch.setattr(slim_window_mod, "SlimWindow", FakeSlimWindow)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._toast = lambda msg, ms=1600: toasts.append(msg)
    win._search_pending_req = 1

    win._open_slim_window(str(deck))

    assert opened == []
    assert toasts and "搜索还在进行" in toasts[-1]


def test_mainwindow_treats_slim_create_as_heavy_shutdown_task(qtbot, tmp_path):
    class FakeTask:
        _label = "ppt-slim-create"

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    assert win._bg_task_shutdown_wait_ms(FakeTask()) == win._BG_HEAVY_SHUTDOWN_WAIT_MS


def test_mainwindow_select_updates_detail(qtbot, tmp_path):
    vm = StubVerMgr(versions=[{"version_id": "v1", "ts": 1000, "page_count": 5}], managed=True)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win._toggle_detail()
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: len(win.detail_panel._version_nodes) >= 1, timeout=1000)


def test_mainwindow_version_preview_runs_in_background_and_dedupes(qtbot, tmp_path, monkeypatch):
    image = tmp_path / "preview.png"
    pm = QPixmap(48, 27)
    pm.fill(Qt.blue)
    assert pm.save(str(image))
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

    class PreviewMgr(StubVerMgr):
        def ensure_version_preview(self, version_id):
            calls.append(version_id)
            return str(image)

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=PreviewMgr(), do_index=False)
    qtbot.addWidget(win)
    win._cur = _fr(path="C:/deck-a.pptx")
    win.detail_panel.show()
    win.detail_panel.update_for(win._cur, [{"version_id": "v1", "ts": 1000, "page_count": 5}])
    tasks.clear()

    win._request_version_preview("v1")
    win._request_version_preview("v1")

    assert len(tasks) == 1
    assert tasks[0].label == "version-preview"
    result = tasks[0].fn()
    tasks[0].done.emit(result)
    pixmap = win.detail_panel._version_preview_labels["v1"].pixmap()
    assert pixmap is not None and not pixmap.isNull()
    tasks[0].finished.emit()
    assert calls == ["v1"]
    assert "v1" not in win._version_preview_inflight


def test_mainwindow_version_preview_does_not_resolve_lazy_backend_on_ui_thread(
    qtbot, tmp_path, monkeypatch
):
    tasks = []
    resolved = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class LazyManager:
        def supports(self, name):
            return name == "ensure_version_preview"

        def __getattr__(self, name):
            resolved.append(name)
            if name == "ensure_version_preview":
                return lambda _version_id: None
            raise AttributeError(name)

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    manager = LazyManager()
    win = MainWindow(
        conn=_index(tmp_path), render_worker=StubRender(), version_mgr=manager, do_index=False
    )
    qtbot.addWidget(win)
    win._cur = _fr(path="C:/deck-a.pptx")
    win.detail_panel.show()

    win._request_version_preview("v1")

    assert resolved == []
    task = next(task for task in tasks if task.label == "version-preview")
    task.fn()
    assert resolved == ["ensure_version_preview"]


def test_history_hint_capability_check_does_not_resolve_lazy_backend_on_ui_thread(
    qtbot, tmp_path
):
    resolved = []

    class LazyManager:
        def supports(self, name):
            return name == "search_history_details"

        def __getattr__(self, name):
            resolved.append(name)
            if name == "search_history_details":
                return lambda _query, limit=200: {"total": 0, "rows": []}
            raise AttributeError(name)

    manager = LazyManager()
    win = MainWindow(
        conn=_index(tmp_path), render_worker=StubRender(), version_mgr=manager, do_index=False
    )
    qtbot.addWidget(win)
    win.search_box.setText("AI")

    win._kick_history_search("AI")

    assert resolved == []
    assert win._history_hint_timer.isActive()


def test_mainwindow_detail_update_loads_versions_in_background(qtbot, tmp_path, monkeypatch):
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
            calls.append(("list_versions", path))
            return [{"version_id": "v1", "ts": 1000, "page_count": 5}]

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        main_window_mod.db,
        "page_titles",
        lambda _conn, file_id: calls.append(("page_titles", file_id)) or [(1, "封面")],
    )

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win._cur = _fr(path="C:/deck-a.pptx")
    win.detail_panel.show()

    win._update_detail()

    assert calls == []
    assert tasks and tasks[-1].label == "detail-load"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)
    tasks[-1].finished.emit()

    assert calls == [("list_versions", "C:/deck-a.pptx"), ("page_titles", 1)]
    assert len(win.detail_panel._version_nodes) == 1


def test_mainwindow_detail_update_reuses_inflight_same_file(qtbot, tmp_path, monkeypatch):
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
            calls.append(("list_versions", path))
            return [{"version_id": "v1", "ts": 1000, "page_count": 5}]

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        main_window_mod.db,
        "page_titles",
        lambda _conn, file_id: calls.append(("page_titles", file_id)) or [(1, "封面")],
    )

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win._cur = _fr(path="C:/deck-a.pptx")
    win.detail_panel.show()

    win._update_detail()
    win._update_detail()
    win._update_detail()

    detail_tasks = [task for task in tasks if task.label == "detail-load"]
    assert [task.label for task in detail_tasks] == ["detail-load"]

    result = detail_tasks[0].fn()
    detail_tasks[0].done.emit(result)
    detail_tasks[0].finished.emit()

    assert calls == [("list_versions", "C:/deck-a.pptx"), ("page_titles", 1)]

    win._update_detail()

    detail_tasks = [task for task in tasks if task.label == "detail-load"]
    assert [task.label for task in detail_tasks] == ["detail-load", "detail-load"]


def test_detail_reopen_same_file_starts_fresh_load_after_hidden(qtbot, tmp_path, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    win._cur = _fr(path="C:/deck-a.pptx")
    win.detail_panel.show()

    win._update_detail()
    first_task = tasks[-1]
    win._toggle_detail()
    win._toggle_detail()

    detail_tasks = [task for task in tasks if task.label == "detail-load"]
    assert len(detail_tasks) == 2
    assert detail_tasks[0] is first_task
    assert detail_tasks[1] is not first_task


def test_detail_reselect_same_file_starts_fresh_load_after_selection_cleared(qtbot, tmp_path, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    current = _fr(path="C:/deck-a.pptx")
    win._cur = current
    win.detail_panel.show()

    win._update_detail()
    first_task = tasks[-1]
    win._on_select(None)
    win._cur = current
    win._update_detail()

    detail_tasks = [task for task in tasks if task.label == "detail-load"]
    assert len(detail_tasks) == 2
    assert detail_tasks[0] is first_task
    assert detail_tasks[1] is not first_task


def test_mainwindow_selection_clear_resets_detail_panel(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    current = _fr(path="C:/deck-a.pptx", page_count=12)
    win._cur = current
    win.detail_panel.show()
    win.detail_panel.update_for(
        current,
        [{"version_id": "v1", "ts": 1000, "page_count": 12}],
    )
    win.detail_panel.set_outline([(1, "cover")])

    win._on_select(None)

    assert win._cur is None
    assert win.detail_panel._path is None
    assert win.detail_panel._version_nodes == []
    assert "12" not in win.detail_panel._meta_label.text()


def test_mainwindow_detail_update_allows_new_file_during_inflight(qtbot, tmp_path, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.show()

    win._cur = _fr(path="C:/deck-a.pptx")
    win._update_detail()
    win._cur = _fr(path="C:/deck-b.pptx")
    win._update_detail()

    detail_tasks = [task for task in tasks if task.label == "detail-load"]
    assert [task.label for task in detail_tasks] == ["detail-load", "detail-load"]


def test_mainwindow_detail_update_force_supersedes_same_file_inflight(qtbot, tmp_path, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)

    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=StubVerMgr(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.show()
    win._cur = _fr(path="C:/deck-a.pptx")

    win._update_detail()
    win._update_detail()
    win._update_detail(force=True)

    detail_tasks = [task for task in tasks if task.label == "detail-load"]
    assert [task.label for task in detail_tasks] == ["detail-load", "detail-load"]
