"""RSI 第二轮：坏索引恢复、预览失败出口与转字结果初始态。"""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import threading

import pytest
import pywintypes
import fixtures_gen as fx
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from pptx_finder import app, db, renderer, render_client, render_service
from pptx_finder.ui import imgtext_window
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.render_worker import RenderWorker
from test_ui import StubRender


def test_imports_are_from_this_worktree():
    assert Path(app.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


def test_bad_index_recovery_starts_real_window_and_preserves_files(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path))
    broken = tmp_path / "index.db"
    broken.write_bytes(b"broken SQLite index\x00" * 100)
    original = broken.read_bytes()
    untouched = [tmp_path / name for name in ("versions.db", "settings.json", "original.pptx")]
    for path in untouched:
        path.write_bytes(b"keep me")
    monkeypatch.setattr(app, "_ask_index_recovery", lambda *_: "rebuild")
    scheduled = []
    monkeypatch.setattr(MainWindow, "_start_indexing",
                        lambda self, roots, workers: scheduled.append((roots, workers)))
    factory = lambda **kw: MainWindow(render_worker=StubRender(), index_all_files_enabled=False, **kw)
    monkeypatch.setattr(app, "MainWindow", factory)
    win = app._create_main_window()
    qtbot.addWidget(win)
    win.show()
    assert win.isVisible()
    qtbot.waitUntil(lambda: len(scheduled) == 1)
    saved = list(tmp_path.glob("index.db.broken-*"))
    assert len(saved) == 1 and saved[0].read_bytes() == original
    assert win._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert win._conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert all(path.read_bytes() == b"keep me" for path in untouched)


def test_bad_index_exit_keeps_original(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path))
    path = tmp_path / "index.db"
    path.write_bytes(b"not a database")
    monkeypatch.setattr(app, "_ask_index_recovery", lambda *_: "exit")
    assert app._create_main_window() is None
    assert path.read_bytes() == b"not a database"
    assert not list(tmp_path.glob("*.broken-*"))


def test_recovered_index_automatically_reindexes_real_ppt(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    data = tmp_path / "data"
    data.mkdir()
    (data / "index.db").write_bytes(b"invalid SQLite header")
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "example.pptx"
    fx.make_pptx(source, [{"body": "重新建立搜索内容 ALPHA"}])
    original = source.read_bytes()
    factory = lambda **kw: MainWindow(
        render_worker=StubRender(), roots=[str(docs)], workers=1,
        index_all_files_enabled=False, smart_grouping_enabled=False, **kw)
    monkeypatch.setattr(app, "MainWindow", factory)
    monkeypatch.setattr(app, "_ask_index_recovery", lambda *_: "rebuild")
    win = app._create_main_window()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win._conn.execute("SELECT count(*) FROM pages_raw").fetchone()[0] == 1,
                    timeout=15000)
    assert "ALPHA" in win._conn.execute("SELECT raw_text FROM pages_raw").fetchone()[0]
    assert source.read_bytes() == original


def test_preserve_index_includes_wal_and_shm(tmp_path):
    path = tmp_path / "index.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).write_bytes((suffix or "db").encode())
    saved = db.preserve_broken_index(path)
    assert saved.name.startswith("index.db.broken-")
    for suffix in ("", "-wal", "-shm"):
        assert Path(str(saved) + suffix).read_bytes() == (suffix or "db").encode()
        assert not Path(str(path) + suffix).exists()


def test_startup_does_not_offer_rebuild_for_locked_index(qtbot, monkeypatch):
    def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app, "MainWindow", locked)
    with pytest.raises(sqlite3.OperationalError):
        app._create_main_window()


def test_recovery_dialog_view_logs_does_not_close(qtbot, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "_open_log_directory", lambda: opened.append(True))
    def interact():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        by_text = {b.text(): b for b in dialog.findChildren(QPushButton)}
        assert "保留故障索引并重新建立" in by_text
        by_text["查看日志"].click()
        assert dialog.isVisible()
        by_text["退出"].click()
    QTimer.singleShot(0, interact)
    assert app._ask_index_recovery(tmp_path / "index.db", "文件已损坏") == "exit"
    assert opened == [True]


@pytest.mark.parametrize("ipc", [False, True])
def test_missing_source_has_reason_in_both_render_paths(tmp_path, monkeypatch, ipc):
    monkeypatch.setattr(renderer, "_state", threading.local())
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: ipc)
    assert renderer.render_page(str(tmp_path / "moved.pptx"), 1) is None
    assert renderer.last_error_category() == "missing_file"


@pytest.mark.parametrize("error,category", [
    (pywintypes.com_error(-2147221164, "localized", None, None), "powerpoint_unavailable"),
    (pywintypes.com_error(-2147418111, "localized", None, None), "busy_timeout"),
    (TimeoutError("slow"), "busy_timeout"),
    (RuntimeError("document failure"), "other"),
])
def test_direct_renderer_classifies_failures(tmp_path, monkeypatch, error, category):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(renderer, "_state", threading.local())
    monkeypatch.setattr(renderer, "_failed_until", {})
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_: False)
    def fail(**_):
        raise error
    monkeypatch.setattr(renderer, "_app_for_render", fail)
    monkeypatch.setattr(renderer, "_release_local_app_reference", lambda: None)
    assert renderer._render_page_direct(str(source), 1) is None
    assert renderer.last_error_category() == category


@pytest.mark.parametrize("category", ["missing_file", "powerpoint_unavailable", "busy_timeout", "other"])
def test_ipc_transports_failure_category(tmp_path, monkeypatch, category):
    monkeypatch.setattr(renderer, "render_page", lambda *_, **__: None)
    monkeypatch.setattr(renderer, "last_error", lambda: "localized reason")
    monkeypatch.setattr(renderer, "last_error_category", lambda: category)
    client = render_client.RendererProcessClient()
    monkeypatch.setattr(client, "request", render_service.handle_request)
    assert client.render_page("deck.pptx", 1, cache_key=None, long_edge=720,
                              hi_priority=True, priority=0) is None
    assert client.last_error_category() == category


def test_ipc_old_empty_reply_and_timeout(monkeypatch):
    client = render_client.RendererProcessClient()
    args = dict(cache_key=None, long_edge=720, hi_priority=True, priority=0)
    monkeypatch.setattr(client, "request", lambda _: {"ok": True, "path": ""})
    assert client.render_page("deck.pptx", 1, **args) is None
    assert client.last_error_category() == "other"
    def timeout(_):
        raise TimeoutError("renderer timed out")
    monkeypatch.setattr(client, "request", timeout)
    assert client.render_page("deck.pptx", 1, **args) is None
    assert client.last_error_category() == "busy_timeout"


@pytest.mark.parametrize("one_shot", [False, True])
def test_ipc_wrapper_passes_reason_back_to_worker_thread(tmp_path, monkeypatch, one_shot):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(renderer, "_state", threading.local())
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: True)
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    client = render_client.RendererProcessClient()
    payloads = []
    def reply(payload):
        payloads.append(payload)
        return {"ok": True, "path": "", "reason": "cannot activate COM",
                "category": "powerpoint_unavailable"}
    monkeypatch.setattr(client, "request", reply)
    monkeypatch.setattr(render_client, "_client", client)
    assert renderer.render_page(str(source), 1, one_shot=one_shot, retry=True) is None
    assert renderer.last_error_category() == "powerpoint_unavailable"
    assert renderer.last_error() == "cannot activate COM"
    assert payloads[0]["retry"] is True
    assert payloads[0]["op"] == ("render_once" if one_shot else "render")


def test_explicit_retry_renders_after_document_failure(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(renderer, "_state", threading.local())
    monkeypatch.setattr(renderer, "_failed_until", {})
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_: False)
    monkeypatch.setattr(renderer, "_release_local_app_reference", lambda: None)
    calls = []
    def fail(**_):
        calls.append(True)
        raise RuntimeError("document unavailable")
    monkeypatch.setattr(renderer, "_app_for_render", fail)
    assert renderer._render_page_direct(str(source), 1) is None
    assert renderer._render_page_direct(str(source), 1) is None
    assert len(calls) == 1
    assert renderer._render_page_direct(str(source), 1, retry=True) is None
    assert len(calls) == 2


@pytest.mark.parametrize("retry", [False, True])
def test_worker_preserves_legacy_signal_and_supplies_reason(qtbot, monkeypatch, retry):
    render_calls = []
    monkeypatch.setattr(renderer, "render_page", lambda *_, **kwargs: render_calls.append(kwargs))
    monkeypatch.setattr(renderer, "last_error_category", lambda: "powerpoint_unavailable")
    monkeypatch.setattr(renderer, "release_session", lambda: True)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)
    worker = RenderWorker()
    old, new = [], []
    worker.rendered.connect(lambda *args: old.append(args))
    worker.rendered_result.connect(lambda *args: new.append(args))
    worker.start()
    try:
        if retry:
            worker.retry_next()
        worker.request(42, "deck.pptx", 1)
        qtbot.waitUntil(lambda: bool(new))
        assert new == [(42, "", "powerpoint_unavailable")]
        assert old == [(42, "")]
        assert bool(render_calls[0].get("retry")) == retry
    finally:
        worker.stop()
        assert worker.wait(3000)


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "index.db")
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win._recent_load_inflight_token is None)
    win._switch_page("search")
    win._hide_empty_hint()
    win.detail_panel.tabs.setCurrentIndex(0)
    return win


@pytest.mark.parametrize("category,text", [
    ("missing_file", "文件不存在或已被移动"),
    ("powerpoint_unavailable", "PowerPoint 暂时不可用"),
    ("busy_timeout", "PowerPoint 正忙或等待超时"),
    ("other", "暂时无法生成原图预览"),
    ("", "暂时无法生成原图预览"),
])
def test_preview_failure_actions_and_legacy_fallback(win, monkeypatch, tmp_path, category, text):
    win._req_id = 7
    win._req_path = None
    win._on_rendered(7, "", category)
    assert win.grab().save(str(tmp_path / f"preview-{category or 'legacy'}.png"))
    assert text in win.image_label.text()
    assert win.preview_retry_btn.isVisible()
    assert win.preview_powerpoint_btn.isVisible() == (category == "powerpoint_unavailable")
    retried, settings = [], []
    monkeypatch.setattr(win, "_request_preview", lambda: retried.append(True))
    monkeypatch.setattr(win, "open_settings_page", settings.append)
    win.preview_retry_btn.click()
    assert retried == [True]
    win.preview_powerpoint_btn.click()
    assert settings == ["PowerPoint"]
    win._clear_preview_empty()
    assert not win.preview_error_actions.isVisible()


def test_imgtext_result_row_follows_conversion_lifecycle(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(imgtext_window.imgtext_ocr, "is_installed", lambda: True)
    win = imgtext_window.ImgTextWindow({})
    qtbot.addWidget(win)
    win.show()
    buttons = (win._copy_btn, win._insert_btn, win._open_result_btn,
               win._open_folder_btn, win._save_as_btn)
    assert all(not b.isVisible() for b in buttons)
    result = SimpleNamespace(blocks=[1], runs=[1], skipped_small=[], skipped_shape=[],
                             skipped_busy=[], skipped_formula=[])
    win._on_converted({"dest": str(tmp_path / "result.pptx"), "result": result})
    assert all(b.isVisible() and b.isEnabled() for b in buttons)
    win._on_converted({"error": "识别失败"})
    assert all(not b.isVisible() for b in buttons)
    win._on_converted({"dest": str(tmp_path / "result.pptx"), "result": result})
    source = tmp_path / "next.png"
    image = QImage(20, 20, QImage.Format_RGB32)
    image.fill(Qt.white)
    image.save(str(source))
    win.set_source(str(source))
    assert all(not b.isVisible() for b in buttons)
