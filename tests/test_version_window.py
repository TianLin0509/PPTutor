from __future__ import annotations

import pptx_finder.ui.version_window as version_window_mod
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from pptx_finder.ui.version_window import VersionWindow


class FakeVersionManager:
    def __init__(self):
        self.calls: list[object] = []

    def list_docs(self):
        self.calls.append("list_docs")
        return [
            {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
            {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "deleted"},
        ]

    def list_docs_details(self):
        self.calls.append(("list_docs_details",))
        return [
            {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
            {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "deleted"},
        ]

    def list_versions_by_doc(self, doc_id: str):
        self.calls.append(("list_versions_by_doc", doc_id))
        return [{"version_id": "v1", "ts": 1_700_000_000, "page_count": 12}]

    def list_versions_by_doc_details(self, doc_id: str, limit: int | None = None):
        self.calls.append(("list_versions_by_doc_details", doc_id, limit))
        return [{"version_id": f"{doc_id}-v1", "ts": 1_700_000_000, "page_count": 12}]

    def search_history(self, query: str):
        self.calls.append(("search_history", query))
        return [{"doc_id": "doc1", "version_id": "v1", "page_no": 3}]

    def get_doc(self, doc_id: str):
        self.calls.append(("get_doc", doc_id))
        return {"doc_id": doc_id, "path": "C:/deck-a.pptx", "status": "active"}

    def get_version(self, version_id: str):
        self.calls.append(("get_version", version_id))
        return {"version_id": version_id, "doc_id": "doc1", "ts": 1_700_000_000}

    def search_history_details(self, query: str, limit: int = 200):
        self.calls.append(("search_history_details", query, limit))
        return {
            "query": query,
            "total": 1,
            "rows": [
                {
                    "doc_path": "C:/deck-a.pptx",
                    "ts": 1_700_000_000,
                    "page_no": 3,
                    "version_id": "v1",
                }
            ],
        }

    def restore_to(self, path: str, version_id: str):
        self.calls.append(("restore_to", path, version_id))
        return True

    def describe_version_diff(self, version_id: str):
        self.calls.append(("describe_version_diff", version_id))
        return {"lines": ["文本改动 1 页：P1。"]}

    def export(self, path: str, version_id: str, dest: str):
        self.calls.append(("export", path, version_id, dest))
        return True

    def recover(self, doc_id: str):
        self.calls.append(("recover", doc_id))
        return True


def test_version_window_constructor_schedules_doc_load(qtbot, monkeypatch):
    scheduled: list[tuple[int, object]] = []
    tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()

    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    assert mgr.calls == []
    assert scheduled
    assert win.doc_list.count() == 1
    assert "加载" in win.doc_list.item(0).text()

    scheduled.pop(0)[1]()

    assert mgr.calls == []
    assert tasks and tasks[-1].label == "version-doc-list-load"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert mgr.calls == [("list_docs_details",)]
    assert win.doc_list.count() == 2
    assert "deck-a.pptx" in win.doc_list.item(0).text()


def test_version_window_background_tasks_registered_for_parent_shutdown(qtbot, monkeypatch):
    scheduled: list[tuple[int, object]] = []
    tasks = []
    parent_bg_tasks = []

    class FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            scheduled.append((delay_ms, callback))

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self._label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "QTimer", FakeTimer, raising=False)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)

    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._parent_bg_tasks = parent_bg_tasks

    scheduled.pop(0)[1]()
    doc_task = tasks[-1]

    assert doc_task in win._docs_tasks
    assert doc_task in parent_bg_tasks

    doc_task.finished.emit()

    assert doc_task not in win._docs_tasks
    assert doc_task not in parent_bg_tasks
    assert win._docs_inflight_token is None

    win._run_file_op("version-export", lambda: True, "导出", "busy", "ok", "fail")
    file_task = tasks[-1]

    assert file_task in win._file_tasks
    assert file_task in parent_bg_tasks

    file_task.finished.emit()

    assert file_task not in win._file_tasks
    assert file_task not in parent_bg_tasks


def test_version_window_file_actions_disabled_until_version_selected(qtbot, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    assert not win.btn_restore.isEnabled()
    assert not win.btn_export.isEnabled()


def test_version_window_populate_docs_auto_selects_first_doc_and_loads_versions(qtbot, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
        {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "deleted"},
    ])

    assert win.doc_list.currentRow() == 0
    assert win._cur_doc == ("doc1", "C:/deck-a.pptx", "active")
    assert tasks and tasks[-1].label == "version-list-load"


def test_version_window_auto_selects_first_loaded_version_and_enables_actions(qtbot, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._cur_doc = ("doc1", "C:/deck-a.pptx", "active")

    win._on_versions_loaded(0, "doc1", [{"version_id": "v1", "ts": 1_700_000_000, "page_count": 12}])

    assert win.ver_list.currentRow() == 0
    assert win.btn_restore.isEnabled()
    assert win.btn_export.isEnabled()


def test_version_window_filters_documents_by_name_and_status(qtbot, monkeypatch):
    class FakeSignal:
        def connect(self, _callback):
            return None

    class FakeTask:
        def __init__(self, *_args, **_kwargs):
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            return None

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/work/deck-a.pptx", "status": "active"},
        {"doc_id": "doc2", "path": "D:/archive/deck-b.pptx", "status": "deleted"},
    ])

    win.doc_filter.setText("archive")
    win._apply_doc_filter()
    assert win.doc_list.count() == 1
    assert "deck-b.pptx" in win.doc_list.item(0).text()

    win.doc_filter.clear()
    win.doc_scope.setCurrentIndex(win.doc_scope.findData("active"))
    assert win.doc_list.count() == 1
    assert win.doc_list.item(0).data(Qt.UserRole)[0] == "doc1"

    win._on_versions_loaded(win._versions_load_token, "doc1", [{
        "version_id": "v1",
        "ts": 1_700_000_000,
        "page_count": 12,
    }])
    assert win.btn_restore.isEnabled()

    win.doc_filter.setText("does-not-exist")
    win._apply_doc_filter()
    assert win._cur_doc is None
    assert win.ver_list.count() == 0
    assert not win.btn_restore.isEnabled()
    assert not win.btn_export.isEnabled()


def test_version_window_quarantines_unhealthy_restore_point(qtbot, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._cur_doc = ("doc1", "C:/deck-a.pptx", "active")

    win._on_versions_loaded(0, "doc1", [{
        "version_id": "bad-v1",
        "ts": 1_700_000_000,
        "page_count": 12,
        "health": "invalid",
        "health_error": "bad zip",
    }])

    item = win.ver_list.item(0)
    assert "无效恢复点" in item.text()
    assert item.toolTip() == "bad zip"
    assert win.version_preview.text() == "恢复点已隔离"
    assert win.version_preview.toolTip() == "bad zip"
    assert not win.btn_restore.isEnabled()
    assert not win.btn_export.isEnabled()
    assert not win.btn_preview.isEnabled()


def test_version_window_version_row_shows_summary_and_cached_preview(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    image = tmp_path / "version.png"
    pm = QPixmap(48, 27)
    pm.fill(Qt.green)
    assert pm.save(str(image))
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._cur_doc = ("doc1", "C:/deck-a.pptx", "active")

    win._on_versions_loaded(
        0,
        "doc1",
        [{
            "version_id": "v1",
            "ts": 1_700_000_000,
            "page_count": 12,
            "changed": "summary-one",
            "thumb_path": str(image),
        }],
    )

    assert "summary-one" in win.ver_list.item(0).text()
    pixmap = win.version_preview.pixmap()
    assert pixmap is not None and not pixmap.isNull()


def test_version_window_preview_runs_in_background_and_updates_item(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    image = tmp_path / "preview.png"
    pm = QPixmap(48, 27)
    pm.fill(Qt.yellow)
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

    class PreviewMgr(FakeVersionManager):
        def ensure_version_preview(self, version_id: str):
            calls.append(version_id)
            return str(image)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = VersionWindow(PreviewMgr())
    qtbot.addWidget(win)
    win._cur_doc = ("doc1", "C:/deck-a.pptx", "active")
    win._on_versions_loaded(0, "doc1", [{"version_id": "v1", "ts": 1_700_000_000, "page_count": 12}])
    tasks.clear()

    win.btn_preview.click()

    assert len(tasks) == 1
    assert tasks[0].label == "version-preview"
    result = tasks[0].fn()
    tasks[0].done.emit(result)
    data = win.ver_list.item(0).data(Qt.UserRole)
    assert data["thumb_path"] == str(image)
    pixmap = win.version_preview.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    tasks[0].finished.emit()
    assert calls == ["v1"]


def test_version_window_history_search_auto_selects_first_result_and_enables_actions(qtbot, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    win._on_history_search_done(
        0,
        "旧内容",
        {
            "total": 1,
            "rows": [{
                "doc_path": "C:/deck-a.pptx",
                "ts": 1_700_000_000,
                "page_no": 3,
                "version_id": "v1",
            }],
        },
    )

    assert win.ver_list.currentRow() == 0
    assert win.btn_restore.isEnabled()
    assert win.btn_export.isEnabled()


def test_version_window_history_search_quarantines_unhealthy_hit(qtbot, monkeypatch):
    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    win._on_history_search_done(
        0,
        "旧内容",
        {
            "total": 1,
            "rows": [{
                "doc_path": "C:/deck-a.pptx",
                "ts": 1_700_000_000,
                "page_no": 3,
                "version_id": "bad-v1",
                "health": "invalid",
                "health_error": "deep: corrupt object",
            }],
        },
    )

    item = win.ver_list.item(0)
    assert "已隔离" in item.text()
    assert item.toolTip() == "deep: corrupt object"
    assert not win.btn_restore.isEnabled()
    assert not win.btn_export.isEnabled()


def test_version_window_scheduled_doc_load_populates(qtbot):
    mgr = FakeVersionManager()

    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    qtbot.waitUntil(
        lambda: mgr.calls[:1] == [("list_docs_details",)] and win.doc_list.count() == 2,
        timeout=1000,
    )
    assert "deck-b.pptx" in win.doc_list.item(1).text()
    assert win.doc_list.currentRow() == 0
    qtbot.waitUntil(lambda: ("list_versions_by_doc_details", "doc1", None) in mgr.calls, timeout=1000)


def test_version_window_reload_docs_runs_in_background(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    tasks.clear()
    mgr.calls.clear()

    win.reload_docs()

    assert mgr.calls == []
    assert tasks and tasks[-1].label == "version-doc-list-load"
    assert "加载" in win.doc_list.item(0).text()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert mgr.calls == [("list_docs_details",)]
    assert win.doc_list.count() == 2


def test_version_window_reload_docs_reuses_inflight_same_token(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    token = win._prepare_doc_reload()
    mgr.calls.clear()

    win._run_reload_docs(token)
    first_task = tasks[-1]
    win._run_reload_docs(token)
    win._run_reload_docs(token)

    doc_tasks = [task for task in tasks if task.label == "version-doc-list-load"]
    assert doc_tasks == [first_task]
    assert mgr.calls == []

    first_task.done.emit(first_task.fn())
    first_task.finished.emit()

    assert mgr.calls == [("list_docs_details",)]
    assert win.doc_list.count() == 2


def test_version_window_reload_docs_new_token_allows_new_load(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    old_token = win._prepare_doc_reload()
    win._run_reload_docs(old_token)
    old_task = tasks[-1]
    new_token = win._prepare_doc_reload()
    win._run_reload_docs(new_token)
    new_task = tasks[-1]

    assert new_task is not old_task


def test_version_window_history_search_runs_in_background(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    mgr.calls.clear()

    win.search.setText("旧内容")
    win._do_search()

    assert ("search_history", "旧内容") not in mgr.calls
    assert not any(call[0] == "search_history_details" for call in mgr.calls if isinstance(call, tuple))
    assert tasks and tasks[-1].label == "version-history-search"
    assert "正在搜索历史" in win.right_title.text()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert ("search_history_details", "旧内容", 200) in mgr.calls
    assert ("search_history", "旧内容") not in mgr.calls
    assert not any(call[0] in {"get_doc", "get_version"} for call in mgr.calls if isinstance(call, tuple))
    assert "命中 1 处" in win.right_title.text()
    assert win.ver_list.count() == 1
    assert "第 3 页" in win.ver_list.item(0).text()


def test_version_window_history_search_reuses_inflight_same_query(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    mgr.calls.clear()

    win.search.setText("旧内容")
    win._do_search()
    first_task = tasks[-1]
    win._do_search()
    win._do_search()

    search_tasks = [task for task in tasks if task.label == "version-history-search"]
    assert search_tasks == [first_task]
    assert not any(call[0] == "search_history_details" for call in mgr.calls if isinstance(call, tuple))

    result = first_task.fn()
    first_task.done.emit(result)
    first_task.finished.emit()

    assert ("search_history_details", "旧内容", 200) in mgr.calls
    assert "命中 1 处" in win.right_title.text()


def test_version_window_history_result_carries_doc_path_for_actions(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QFileDialog, "getSaveFileName", lambda *args: ("C:/out.pptx", ""))
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    win.search.setText("旧内容")
    win._do_search()
    result = tasks[-1].fn()
    tasks[-1].done.emit(result)
    win.ver_list.setCurrentRow(0)
    mgr.calls.clear()

    win._export()

    assert not any(call[0] in {"get_doc", "get_version"} for call in mgr.calls if isinstance(call, tuple))
    result = tasks[-1].fn()
    assert result is True
    assert ("export", "C:/deck-a.pptx", "v1", "C:/out.pptx") in mgr.calls


def test_version_window_history_search_ignores_stale_results(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    win.search.setText("old")
    win._do_search()
    old_task = tasks[-1]

    win.search.setText("new")
    win._do_search()
    new_task = tasks[-1]

    old_task.done.emit(old_task.fn())
    assert "old" not in win.right_title.text()

    new_task.done.emit(new_task.fn())
    assert "new" in win.right_title.text()
    assert win.ver_list.count() == 1


def test_version_window_empty_search_cancels_inflight_history_result(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    tasks.clear()
    win._cur_doc = ("doc1", "C:/deck-a.pptx", "active")
    win.right_title.setText("deck-a.pptx")
    win.ver_list.clear()
    win.ver_list.addItem("current timeline")

    win.search.setText("old")
    win._do_search()
    old_task = tasks[-1]

    win.search.setText("")
    win._do_search()
    old_task.done.emit(old_task.fn())

    assert win._search_inflight_token is None
    assert win._search_inflight_query is None
    assert "old" not in win.right_title.text()
    assert all(
        not isinstance(win.ver_list.item(i).data(version_window_mod.Qt.UserRole), dict)
        for i in range(win.ver_list.count())
    )
    assert any(task.label == "version-list-load" for task in tasks)


def test_version_window_doc_selection_cancels_inflight_history_result(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    win.search.setText("old")
    win._do_search()
    old_search_task = tasks[-1]

    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
        {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "active"},
    ])
    version_task = tasks[-1]
    version_task.done.emit(version_task.fn())

    old_search_task.done.emit(old_search_task.fn())

    assert win._search_inflight_token is None
    assert win._search_inflight_query is None
    assert "old" not in win.right_title.text()
    assert win.ver_list.count() == 1
    assert win.ver_list.item(0).data(version_window_mod.Qt.UserRole)["version_id"] == "doc1-v1"


def test_version_window_doc_selection_loads_versions_in_background(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    mgr.calls.clear()

    win.reload_docs()
    doc_task = tasks[-1]
    assert doc_task.label == "version-doc-list-load"
    doc_task.done.emit(doc_task.fn())
    item = win.doc_list.item(0)
    mgr.calls.clear()

    win._on_doc(item)

    assert ("list_versions_by_doc", "doc1") not in mgr.calls
    assert tasks and tasks[-1].label == "version-list-load"
    assert "正在加载版本时间线" in win.ver_list.item(0).text()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert ("list_versions_by_doc_details", "doc1", None) in mgr.calls
    assert ("list_versions_by_doc", "doc1") not in mgr.calls
    assert win.ver_list.count() == 1
    assert win.ver_list.item(0).data(version_window_mod.Qt.UserRole)["version_id"] == "doc1-v1"


def test_version_window_doc_selection_reuses_inflight_same_doc(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
        {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "active"},
    ])
    first_task = tasks[-1]
    mgr.calls.clear()

    win._on_doc(win.doc_list.item(0))
    win._on_doc(win.doc_list.item(0))

    version_tasks = [task for task in tasks if task.label == "version-list-load"]
    assert version_tasks == [first_task]
    assert not any(call[0] == "list_versions_by_doc_details" for call in mgr.calls if isinstance(call, tuple))

    first_task.done.emit(first_task.fn())
    first_task.finished.emit()

    assert ("list_versions_by_doc_details", "doc1", None) in mgr.calls
    assert win.ver_list.count() == 1


def test_version_window_doc_selection_new_doc_allows_new_version_load(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
        {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "active"},
    ])
    old_task = tasks[-1]
    win._on_doc(win.doc_list.item(1))
    new_task = tasks[-1]

    assert new_task is not old_task

    new_task.done.emit(new_task.fn())
    old_task.done.emit(old_task.fn())

    assert "doc2-v1" == win.ver_list.item(0).data(version_window_mod.Qt.UserRole)["version_id"]


def test_version_window_doc_selection_ignores_stale_version_list(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.reload_docs()
    doc_task = tasks[-1]
    assert doc_task.label == "version-doc-list-load"
    doc_task.done.emit(doc_task.fn())

    win._on_doc(win.doc_list.item(0))
    first_task = tasks[-1]
    win._on_doc(win.doc_list.item(1))
    second_task = tasks[-1]

    first_task.done.emit(first_task.fn())
    assert not any(
        isinstance(win.ver_list.item(i).data(version_window_mod.Qt.UserRole), dict)
        and win.ver_list.item(i).data(version_window_mod.Qt.UserRole).get("version_id") == "doc1-v1"
        for i in range(win.ver_list.count())
    )

    second_task.done.emit(second_task.fn())
    assert win.ver_list.count() == 1
    assert win.ver_list.item(0).data(version_window_mod.Qt.UserRole)["version_id"] == "doc2-v1"


def test_version_window_reload_docs_cancels_inflight_version_list(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
    ])
    old_version_task = tasks[-1]

    win.reload_docs()
    old_version_task.done.emit(old_version_task.fn())

    assert win._cur_doc is None
    assert win.ver_list.count() == 0
    assert "加载版本文档" in win.right_title.text()


def test_version_window_reload_docs_cancels_inflight_history_search(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)

    win.search.setText("old")
    win._do_search()
    old_search_task = tasks[-1]

    win.reload_docs()
    old_search_task.done.emit(old_search_task.fn())

    assert win._cur_doc is None
    assert win.ver_list.count() == 0
    assert "old" not in win.right_title.text()
    assert "加载版本文档" in win.right_title.text()


def test_version_window_recover_runs_in_background(qtbot, monkeypatch):
    tasks = []
    infos = []
    reloads = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: infos.append(args))
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win._cur_doc = ("doc2", "C:/deck-b.pptx", "deleted")
    monkeypatch.setattr(win, "schedule_reload_docs", lambda: reloads.append("reload"))
    mgr.calls.clear()

    win._recover()

    assert ("recover", "doc2") not in mgr.calls
    assert tasks and tasks[-1].label == "version-recover"
    assert not win.btn_recover.isEnabled()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert ("recover", "doc2") in mgr.calls
    assert infos
    assert reloads == ["reload"]
    assert win.btn_recover.isEnabled()


def test_version_window_export_runs_in_background(qtbot, monkeypatch):
    tasks = []
    infos = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: infos.append(args))
    monkeypatch.setattr(version_window_mod.QFileDialog, "getSaveFileName", lambda *args: ("C:/out.pptx", ""))
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, "v1")
    win.ver_list.setCurrentRow(0)
    mgr.calls.clear()

    win._export()

    assert not any(call[0] == "export" for call in mgr.calls if isinstance(call, tuple))
    assert tasks and tasks[-1].label == "version-export"
    assert not win.btn_export.isEnabled()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert ("export", "C:/deck-a.pptx", "v1", "C:/out.pptx") in mgr.calls
    assert infos
    assert win.btn_export.isEnabled()


def test_version_window_export_restores_title_after_background_done(qtbot, monkeypatch):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(version_window_mod.QFileDialog, "getSaveFileName", lambda *args: ("C:/out.pptx", ""))
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.right_title.setText("ready-title")
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, "v1")
    win.ver_list.setCurrentRow(0)

    win._export()

    assert tasks and tasks[-1].label == "version-export"
    assert win.right_title.text() != "ready-title"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert win.right_title.text() == "ready-title"


def test_version_window_history_result_does_not_replace_file_op_busy_title(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win.right_title.setText("ready-title")

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )

    assert tasks and tasks[-1].label == "version-export"
    assert win.right_title.text() == "exporting..."
    win._search_token = 7

    win._on_history_search_done(
        7,
        "old",
        {
            "total": 1,
            "rows": [{
                "doc_path": "C:/deck-a.pptx",
                "ts": 1_700_000_000,
                "page_no": 3,
                "version_id": "v1",
            }],
        },
    )

    assert win.right_title.text() == "exporting..."


def test_version_window_search_trigger_rechecks_file_op_busy(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win.ver_list.addItem("ready-version")

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )
    assert [task.label for task in tasks] == ["version-export"]
    win.search.setText("old content")

    win._do_search()

    assert [task.label for task in tasks] == ["version-export"]
    assert win.ver_list.count() == 1
    assert win.ver_list.item(0).text() == "ready-version"
    assert win.right_title.text() == VersionWindow._FILE_OP_BUSY_NOTICE


def test_version_window_reload_docs_defers_during_file_op(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda _delay, callback: callback())
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    tasks.clear()
    win.right_title.setText("ready-title")

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )
    file_task = tasks[-1]

    win.reload_docs()

    assert win.right_title.text() == "exporting..."
    assert [task.label for task in tasks] == ["version-export"]

    file_task.done.emit(True)
    file_task.finished.emit()

    assert [task.label for task in tasks] == ["version-export", "version-doc-list-load"]


def test_version_window_inflight_doc_result_defers_during_file_op(qtbot, monkeypatch):
    tasks = []
    scheduled = []

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

    monkeypatch.setattr(
        version_window_mod.QTimer,
        "singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    tasks.clear()
    scheduled.clear()

    win.reload_docs()
    doc_task = tasks[-1]
    assert doc_task.label == "version-doc-list-load"

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )
    file_task = tasks[-1]

    doc_task.done.emit(doc_task.fn())

    assert win.right_title.text() == "exporting..."
    assert [task.label for task in tasks] == ["version-doc-list-load", "version-export"]
    assert all(
        "deck-a.pptx" not in win.doc_list.item(i).text()
        for i in range(win.doc_list.count())
    )

    file_task.done.emit(True)
    file_task.finished.emit()

    assert scheduled
    scheduled[-1][1]()
    assert [task.label for task in tasks].count("version-doc-list-load") == 2


def test_version_window_file_op_disables_navigation_until_done(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )

    assert not win.doc_list.isEnabled()
    assert not win.search.isEnabled()
    assert not win.search_btn.isEnabled()

    tasks[-1].done.emit(True)
    tasks[-1].finished.emit()

    assert win.doc_list.isEnabled()
    assert win.search.isEnabled()
    assert win.search_btn.isEnabled()


def test_version_window_doc_selection_rechecks_file_op_busy(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
        {"doc_id": "doc2", "path": "C:/deck-b.pptx", "status": "active"},
    ])
    initial_doc = win._cur_doc
    tasks.clear()

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )
    item = win.doc_list.item(1)

    win._on_doc(item)

    assert [task.label for task in tasks] == ["version-export"]
    assert win._cur_doc == initial_doc
    assert win.right_title.text() == VersionWindow._FILE_OP_BUSY_NOTICE


def test_version_window_file_op_disables_version_selection_until_done(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )

    assert not win.ver_list.isEnabled()

    tasks[-1].done.emit(True)
    tasks[-1].finished.emit()

    assert win.ver_list.isEnabled()


def test_version_window_version_list_result_defers_during_file_op(qtbot, monkeypatch):
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

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: None)
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
    ])
    version_task = tasks[-1]
    assert version_task.label == "version-list-load"

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )
    file_task = tasks[-1]

    version_task.done.emit(version_task.fn())

    assert not any(
        isinstance(win.ver_list.item(i).data(version_window_mod.Qt.UserRole), dict)
        and win.ver_list.item(i).data(version_window_mod.Qt.UserRole).get("version_id") == "doc1-v1"
        for i in range(win.ver_list.count())
    )

    file_task.done.emit(True)
    file_task.finished.emit()

    assert win.ver_list.count() == 1
    assert win.ver_list.item(0).data(version_window_mod.Qt.UserRole)["version_id"] == "doc1-v1"


def test_version_window_skips_deferred_versions_when_doc_reload_pending(qtbot, monkeypatch):
    tasks = []
    snapshots_at_message = []

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

    def snapshot_versions(win):
        values = []
        for i in range(win.ver_list.count()):
            data = win.ver_list.item(i).data(version_window_mod.Qt.UserRole)
            if isinstance(data, dict):
                values.append(data.get("version_id"))
            else:
                values.append(data)
        return values

    monkeypatch.setattr(version_window_mod.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        version_window_mod.QMessageBox,
        "information",
        lambda *args: snapshots_at_message.append(snapshot_versions(args[0])),
    )
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win._populate_docs([
        {"doc_id": "doc1", "path": "C:/deck-a.pptx", "status": "active"},
    ])
    version_task = tasks[-1]
    assert version_task.label == "version-list-load"

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting...",
        "ok",
        "fail",
    )
    file_task = tasks[-1]

    version_task.done.emit(version_task.fn())
    win.reload_docs()

    assert win._pending_versions_after_file_op is not None
    assert win._reload_docs_after_file_op is True

    file_task.done.emit(True)

    assert snapshots_at_message
    assert "doc1-v1" not in snapshots_at_message[-1]
    assert win._pending_versions_after_file_op is None


def test_version_window_export_uses_item_path_without_db_lookup(qtbot, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QFileDialog, "getSaveFileName", lambda *args: ("C:/out.pptx", ""))
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win._cur_doc = ("doc2", "C:/selected-doc.pptx", "active")
    win._on_versions_loaded(0, "doc2", [{"version_id": "v2", "ts": 1_700_000_001, "page_count": 8}])
    win.ver_list.setCurrentRow(0)
    mgr.calls.clear()

    win._export()

    assert not any(call[0] in {"get_doc", "get_version"} for call in mgr.calls if isinstance(call, tuple))
    result = tasks[-1].fn()
    assert result is True
    assert ("export", "C:/selected-doc.pptx", "v2", "C:/out.pptx") in mgr.calls


def test_version_window_export_appends_pptx_extension(qtbot, monkeypatch):
    tasks = []

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

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QFileDialog, "getSaveFileName", lambda *args: ("C:/out-no-ext", ""))
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, "v1")
    win.ver_list.setCurrentRow(0)
    mgr.calls.clear()

    win._export()

    assert tasks and tasks[-1].label == "version-export"
    assert tasks[-1].fn() is True
    assert ("export", "C:/deck-a.pptx", "v1", "C:/out-no-ext.pptx") in mgr.calls


def test_version_window_export_skips_save_dialog_when_file_op_active(qtbot, monkeypatch):
    dialogs = []
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, "v1")
    win.ver_list.setCurrentRow(0)
    win._active_file_op = True
    monkeypatch.setattr(
        version_window_mod.QFileDialog,
        "getSaveFileName",
        lambda *args: dialogs.append(args) or ("C:/busy-export.pptx", ""),
    )

    win._export()

    assert dialogs == []


def test_version_window_restore_runs_in_background(qtbot, monkeypatch):
    tasks = []
    infos = []
    questions = []
    refreshes = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: infos.append(args))
    monkeypatch.setattr(
        version_window_mod.QMessageBox,
        "question",
        lambda *args: questions.append(args) or version_window_mod.QMessageBox.Yes,
    )
    monkeypatch.setattr(version_window_mod.os.path, "exists", lambda _path: True)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.doc_list.clear()
    win.doc_list.addItem("deck-a.pptx")
    win.doc_list.setCurrentRow(0)
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, "v1")
    win.ver_list.setCurrentRow(0)
    monkeypatch.setattr(win, "_on_doc", lambda item, *args: refreshes.append(item))
    mgr.calls.clear()

    win._restore()

    assert not any(call[0] == "restore_to" for call in mgr.calls if isinstance(call, tuple))
    assert tasks and tasks[-1].label == "version-restore"
    assert questions and "文本改动 1 页" in questions[-1][2]
    assert not win.btn_restore.isEnabled()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert ("restore_to", "C:/deck-a.pptx", "v1") in mgr.calls
    assert infos
    assert refreshes == [win.doc_list.currentItem()]
    assert win.btn_restore.isEnabled()


def test_version_window_restore_does_not_probe_path_on_ui_thread(qtbot, monkeypatch):
    tasks = []
    exists_calls = []

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

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "question", lambda *args: version_window_mod.QMessageBox.Yes)
    monkeypatch.setattr(version_window_mod.os.path, "exists", lambda path: exists_calls.append(path) or True)
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, {
        "version_id": "v1",
        "doc_path": "C:/deck-a.pptx",
    })
    win.ver_list.setCurrentRow(0)

    win._restore()

    assert exists_calls == []
    assert tasks and tasks[-1].label == "version-restore"


def test_version_window_restore_skips_confirm_when_file_op_active(qtbot, monkeypatch):
    confirms = []
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win.ver_list.addItem("v1")
    win.ver_list.item(0).setData(version_window_mod.Qt.UserRole, "v1")
    win.ver_list.setCurrentRow(0)
    win._active_file_op = True
    monkeypatch.setattr(
        version_window_mod.QMessageBox,
        "question",
        lambda *args: confirms.append(args) or version_window_mod.QMessageBox.Yes,
    )

    win._restore()

    assert confirms == []


def test_version_window_late_data_callbacks_ignored_after_closing(qtbot):
    mgr = FakeVersionManager()
    win = VersionWindow(mgr)
    qtbot.addWidget(win)
    win._closing = True
    win._docs_load_token = 7
    win._versions_load_token = 8
    win._search_token = 9
    win.doc_list.clear()
    win.doc_list.addItem("closing-docs")
    win.ver_list.clear()
    win.ver_list.addItem("closing-versions")
    win.right_title.setText("closing-title")

    win._on_docs_loaded(7, [{"doc_id": "docX", "path": "C:/late.pptx", "status": "active"}])
    win._on_versions_loaded(8, "docX", [{"version_id": "late", "ts": 1, "page_count": 1}])
    win._on_history_search_done(9, "late", {"total": 1, "rows": [{"version_id": "late"}]})

    assert win.doc_list.count() == 1
    assert win.doc_list.item(0).text() == "closing-docs"
    assert win.ver_list.count() == 1
    assert win.ver_list.item(0).text() == "closing-versions"
    assert win.right_title.text() == "closing-title"


def test_version_window_late_file_op_done_ignored_after_closing(qtbot, monkeypatch):
    tasks = []
    infos = []
    callbacks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: infos.append(args))
    win = VersionWindow(FakeVersionManager())
    qtbot.addWidget(win)
    win.right_title.setText("ready")

    win._run_file_op(
        "version-export",
        lambda: True,
        "导出",
        "正在导出版本…",
        "已导出",
        "导出失败",
        lambda: callbacks.append("ok"),
    )
    win._closing = True

    tasks[-1].done.emit(tasks[-1].fn())

    assert infos == []
    assert callbacks == []


def test_version_window_late_file_op_done_ignored_after_owner_closing(qtbot, monkeypatch):
    tasks = []
    infos = []
    callbacks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class Owner:
        _closing = False

    owner = Owner()
    monkeypatch.setattr(version_window_mod, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(version_window_mod.QMessageBox, "information", lambda *args: infos.append(args))
    win = VersionWindow(FakeVersionManager())
    win._closing_owner = owner
    qtbot.addWidget(win)
    win.right_title.setText("ready")

    win._run_file_op(
        "version-export",
        lambda: True,
        "export",
        "exporting",
        "exported",
        "export failed",
        lambda: callbacks.append("ok"),
    )
    owner._closing = True

    tasks[-1].done.emit(tasks[-1].fn())

    assert infos == []
    assert callbacks == []
