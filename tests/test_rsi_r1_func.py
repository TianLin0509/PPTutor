"""RSI 首轮功能回归：真实 Qt 控件，下载与索引事件使用可控夹具。"""
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel

from pptx_finder import db, imgtext_ocr, namestore
from pptx_finder.ui import component_center as cc
from pptx_finder.ui.main_window import MainWindow
from test_ui import StubRender


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    widget = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(widget)
    yield widget
    widget.close()
    conn.close()


def test_imports_use_independent_copy():
    import pptx_finder.ui.main_window as module
    assert Path(module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("event", ["full", "live", "hot", "reconcile"])
@pytest.mark.parametrize("all_files", [False, True])
def test_index_events_refresh_current_query(win, monkeypatch, event, all_files):
    win.search_box.setText("原有查询")
    win._debounce.stop()
    monkeypatch.setattr(win, "_all_files_scope", lambda: all_files)
    calls = []
    monkeypatch.setattr(win, "_do_search", lambda: calls.append(1))
    win._last_user_input_ts = win._last_bg_research_ts = 0
    if event == "full":
        win._on_index_done({"name_index_entries": 0})
    elif event == "live":
        win._on_live_indexed("x.pptx")
    elif event == "hot":
        win._on_hot_written(1)
    else:
        win._on_inventory_reconciled({"dirs": 1})
    win._live_refresh.stop()
    if event in ("full", "live") or all_files:
        assert win._live_refresh_pending
        win._do_live_refresh()
        assert calls == [1]
    else:
        assert not win._live_refresh_pending


def test_name_error_survives_status_refresh_until_success(win, monkeypatch):
    stats = {"file_count": 3, "page_count": 7}
    win._apply_status_stats({"name_index_error": "磁盘空间不足"}, stats)
    assert "PPT 内容可搜 · 文件名盘点失败：磁盘空间不足" in win.status_label.text()
    assert "索引就绪" not in win.status_label.text()
    assert not win.name_index_retry_btn.isHidden()
    calls = []
    monkeypatch.setattr(win, "_request_full_rescan", lambda: calls.append(1))
    win.name_index_retry_btn.click()
    assert calls == [1]
    win._apply_status_stats(None, stats)
    assert "磁盘空间不足" in win.status_label.text()
    win._search_keep_selection = False
    win._finish_search("暂无结果", [])
    assert "磁盘空间不足" in win.status_label.text()
    win._apply_status_stats({"error": "其他建库失败"}, stats)
    assert "磁盘空间不足" in win.status_label.text()
    win._on_index_progress(1, 2, "重试中")
    assert "磁盘空间不足" in win.status_label.text()
    win._on_index_done({"error": "本轮内容扫描失败"})
    assert "磁盘空间不足" in win.status_label.text()
    win._apply_status_stats({"name_index_entries": 0}, stats)
    assert "索引就绪" in win.status_label.text()
    assert win.name_index_retry_btn.isHidden()


def test_all_files_pending_inventory_differs_from_no_matches(win, monkeypatch):
    monkeypatch.setattr(win, "_all_files_scope", lambda: True)
    monkeypatch.setattr(namestore, "peek_header", lambda: None)
    win._show_empty_hint("abc")
    assert "全盘文件名盘点还没完成" in win._empty_tip.text()
    assert "完成后会自动刷新" in win._empty_tip.text()
    monkeypatch.setattr(namestore, "peek_header", lambda: (0, 1))
    win._show_empty_hint("abc")
    assert win._empty_tip.text() == "换个说法试试"


def test_welcome_features_settings_entry(win, monkeypatch):
    calls = []
    monkeypatch.setattr(win, "open_settings_page", calls.append)
    win.maybe_show_welcome()
    ov = win._welcome
    text = " ".join(label.text() for label in ov.findChildren(QLabel))
    assert "版本留存" in text and "智能归组" in text and "可在设置里开启" in text
    ov._settings_btn.click()
    assert calls == ["功能"]
    assert win._welcome is None


def test_welcome_progress_and_terminal_states(win, qtbot):
    win.resize(1440, 900)
    win.show()
    win.maybe_show_welcome()
    ov = win._welcome
    win._on_index_progress(2, 9, "x.pptx")
    assert "已处理 2 / 9 个文件" in ov._progress_label.text()
    win._on_index_progress(0, -1, "发现 25 个文件")
    assert "已发现 25 个文件" in ov._progress_label.text()
    win._on_index_done({"name_index_error": "磁盘满"})
    assert "首次索引完成" in ov._progress_label.text() and "磁盘满" in ov._progress_label.text()
    qtbot.waitExposed(win)
    target = Path(__file__).resolve().parents[1] / "artifacts" / "rsi-welcome.png"
    assert ov.grab().save(str(target))
    win._on_index_done({"error": "读取失败"})
    assert "读取失败" in ov._progress_label.text() and "正在首次扫描" not in ov._progress_label.text()
    win._on_index_done({"cancelled": 1})
    assert "已暂停" in ov._progress_label.text()


def test_welcome_opened_after_index_completion(win):
    win._on_index_done({"name_index_entries": 0})
    win.maybe_show_welcome()
    assert "首次索引完成" in win._welcome._progress_label.text()


def test_failed_hot_write_does_not_schedule_search(win, monkeypatch):
    monkeypatch.setattr(win, "_all_files_scope", lambda: True)
    win._on_hot_written(None)
    assert not win._live_refresh_pending
    assert win._hot_write_again


@pytest.fixture
def center(qapp, monkeypatch):
    instance = cc.ComponentCenter()
    monkeypatch.setattr(cc, "component_center", lambda: instance)
    return instance


def test_imgtext_subscribes_before_own_download(qtbot, monkeypatch, center):
    from pptx_finder.ui.imgtext_window import ImgTextWindow
    installed = [False]
    monkeypatch.setattr(imgtext_ocr, "is_installed", lambda: installed[0])
    center._apply("ocr", {"phase": "downloading"})
    widget = ImgTextWindow({})
    qtbot.addWidget(widget)
    widget._source = "retained.png"
    assert widget._component_btn.text() == "查看下载进度"
    installed[0] = True
    center._apply("ocr", {"phase": "done"})
    assert widget._component_box.isHidden() and widget._convert_btn.isEnabled()
    installed[0] = False
    # 卸载必须由真实设置回调广播；仅模拟发信号会漏掉入口接线。
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager
    settings = SettingsDialog(VersionManager())
    qtbot.addWidget(settings)
    monkeypatch.setattr(settings, "_refresh_components", lambda **kw: None)
    settings._on_component_uninstalled("ocr", 123)
    assert not widget._component_box.isHidden() and not widget._convert_btn.isEnabled()
    center._apply("ocr", {"phase": "error", "error": "网络超时"})
    assert "网络超时" in widget._component_label.text()


def test_screenshot_download_failure_cancel_and_retry(qtbot, tmp_path, monkeypatch, center):
    from test_screenshot_productivity import window, image
    from pptx_finder import components
    monkeypatch.setattr(imgtext_ocr, "is_installed", lambda: False)
    monkeypatch.setattr(components, "bundled", lambda key: False)
    widget = window(qtbot, tmp_path, lambda text: None, None)
    widget._text_selected(image())
    controller = widget._text
    source = controller.source.copy()
    calls = []
    monkeypatch.setattr(center, "request", lambda *a, **kw: calls.append(kw))
    controller.install()
    center._apply("formula", {"phase": "error", "error": "无关错误"})
    assert "无关错误" not in controller.dialog.status.text()
    center._apply("ocr", {"phase": "error", "error": "网络超时"})
    assert "网络超时" in controller.dialog.status.text()
    assert "重试" in controller.dialog.install_btn.text()
    controller.dialog.install_btn.click()
    assert len(calls) == 2
    center._apply("ocr", {"phase": "cancelled"})
    assert "已取消" in controller.dialog.status.text()
    assert controller.source == source and not widget._busy
    center._apply("ocr", {"phase": "done"})
    assert "已就绪" in controller.dialog.status.text()
    assert controller.dialog.install_btn.isHidden() and controller.source == source


@pytest.mark.parametrize("event", ["full", "hot", "reconcile"])
def test_background_events_keep_selected_file(qtbot, tmp_path, monkeypatch, event):
    from test_ui import _index_multi
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    conn = _index_multi(tmp_path, {"共同词-a.pptx": ["共同词 甲"], "共同词-b.pptx": ["共同词 乙"]})
    widget = MainWindow(conn=conn, render_worker=StubRender(), do_index=False,
                        smart_grouping_enabled=False)
    qtbot.addWidget(widget)
    widget.search_box.setText("共同词")
    widget._do_search()
    widget._select_path(widget._results[1].path)
    path = widget._cur.path
    monkeypatch.setattr(widget, "_all_files_scope", lambda: True)
    if event == "full":
        widget._on_index_done({"name_index_entries": 2})
    elif event == "hot":
        widget._on_hot_written(1)
    else:
        widget._on_inventory_reconciled({"dirs": 1})
    widget._live_refresh.stop()
    widget._last_user_input_ts = widget._last_bg_research_ts = 0
    widget._do_live_refresh()
    assert widget._cur is not None and widget._cur.path == path
    widget.close()
    conn.close()


def test_name_layer_updates_do_not_starve_result_refresh(qtbot, tmp_path, monkeypatch):
    """热层每秒重写时，结果刷新计时器不能被一直往后推（2026-11-22 Claude 审查补）。"""
    from pptx_finder import db
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    conn = db.connect(tmp_path / "ui.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, do_index=False, index_all_files_enabled=True)
    qtbot.addWidget(win)
    win._after_name_index_update()
    first_left = win._live_refresh.remainingTime()
    qtbot.wait(300)
    win._after_name_index_update()
    assert win._live_refresh.remainingTime() <= first_left - 200
    conn.close()
