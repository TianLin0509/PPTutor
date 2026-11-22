"""首次使用：欢迎引导、空搜索、素材库和缺组件时的图片转换。"""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel

from pptx_finder import db, namestore
from pptx_finder.ui import component_center as cc, imgtext_window, main_window, theme
from pptx_finder.ui.material_window import MaterialWindow
from pptx_finder.ui.welcome_overlay import WelcomeOverlay


def test_imports_use_this_worktree():
    expected = Path(__file__).resolve().parents[1] / "src/pptx_finder/ui/main_window.py"
    assert Path(main_window.__file__).resolve() == expected


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    from test_ui import StubRender
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    file_id = db.upsert_file(conn, path="C:/slides/最近修改.pptx", mtime=1, size=12,
                             name="最近修改.pptx", ext=".pptx", content_hash="test",
                             page_count=1, status="ok", error="", indexed_at=1)
    db.replace_pages(conn, file_id, [(1, "首次使用", "")])
    conn.commit()
    widget = main_window.MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(widget)
    yield widget
    widget.close()
    conn.close()


def test_welcome_recommends_two_styles_and_explains_reuse(qtbot):
    picked = []
    overlay = WelcomeOverlay(on_start=lambda: None, on_pick_theme=picked.append,
                             current_theme="atelier")
    qtbot.addWidget(overlay)
    assert set(overlay._theme_btns) == {"atelier", "atelier_dark"}
    assert len(theme.THEMES) > 2
    overlay._theme_btns["atelier_dark"].click()
    assert picked == ["atelier_dark"]
    text = " ".join(label.text() for label in overlay.findChildren(QLabel))
    for phrase in ("复制本页", "插入到 PPT", "图片转文字", "素材库", "更多风格"):
        assert phrase in text
    assert "规模仓鼠" not in text


@pytest.mark.parametrize("style", ["atelier", "atelier_dark"])
def test_welcome_first_use_layout_fits_real_window(qtbot, style):
    from PySide6.QtWidgets import QWidget
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(1440, 900)
    overlay = WelcomeOverlay(host, on_start=lambda: None, on_pick_theme=lambda _: None,
                             current_theme=style)
    overlay.resize(host.size())
    overlay.finish_index({"name_index_error": "OSError: [Errno 28] No space left"})
    host.show()
    qtbot.waitExposed(host)
    assert overlay.rect().contains(overlay._start_btn.mapTo(overlay, overlay._start_btn.rect().bottomRight()))
    target = Path(__file__).resolve().parents[1] / "artifacts" / f"rsi-r5-welcome-{style}.png"
    assert overlay.grab().save(str(target))


@pytest.mark.parametrize("field", ["error", "name_index_error"])
@pytest.mark.parametrize("error,reason", [
    ("OSError: [Errno 28] No space left on device", "磁盘空间不足"),
    ("PermissionError: [WinError 5] Access is denied", "访问权限"),
    ("RuntimeError: unexpected <failure>", "读取失败"),
])
def test_welcome_errors_explain_reason_and_retry(qtbot, field, error, reason):
    overlay = WelcomeOverlay(on_start=lambda: None, on_pick_theme=lambda _: None)
    qtbot.addWidget(overlay)
    overlay.finish_index({field: error})
    text = overlay._progress_label.text()
    assert reason in text
    assert "开始使用" in text and "设置 → 健康诊断" in text and "重新扫描索引" in text
    assert "Error" not in text and "Errno" not in text and "状态栏" not in text


def test_clear_search_keeps_recent_list_even_after_index_finishes(win, qtbot):
    assert win._current_page_key() == "dashboard"
    win.search_box.setText("首次使用")
    win._do_search()
    assert win._current_page_key() == "search"
    win._clear_search_now()
    assert win._current_page_key() == "search"
    assert win._showing_recent
    qtbot.waitUntil(lambda: win.result_count.text() == "最近修改 · 1 个文件")
    win._on_index_done({"indexed": 1, "deleted": 0})
    assert win._current_page_key() == "search"


def test_index_progress_and_empty_search_use_plain_language(win, monkeypatch):
    win._on_index_progress(0, -1, "正在查找")
    assert win.index_phase_label.text() == "查找文件"
    assert "查找文件" in win.status_label.text()
    win._on_index_progress(1, 3, "test.pptx")
    assert win.index_phase_label.text() == "建立索引"
    assert "不影响你同时搜索" in win.status_label.text()
    monkeypatch.setattr(win, "_all_files_scope", lambda: True)
    monkeypatch.setattr(namestore, "peek_header", lambda: None)
    win._show_empty_hint("test")
    assert "查找文件" in win._empty_tip.text()
    text = win.status_label.text() + win._empty_tip.text()
    assert all(word not in text for word in ("盘点", "建库", "前台操作优先"))


def test_material_capture_is_primary_and_delete_uses_plain_language(qtbot, tmp_path, monkeypatch):
    from pptx_finder.materials import MaterialLibrary
    widget = MaterialWindow(library=MaterialLibrary(tmp_path / "materials"))
    qtbot.addWidget(widget)
    qtbot.waitUntil(lambda: not widget._busy)
    assert widget.capture_btn.objectName() == "primary"
    assert widget.pack_btn.objectName() != "primary"
    widget.setStyleSheet(theme.build_qss("atelier") +
                        f"QWidget#materialWin {{ background: {theme.tok('atelier')['appbg']}; }}")
    widget.show()
    qtbot.waitExposed(widget)
    assert widget.grab().save(str(Path(__file__).resolve().parents[1] / "artifacts/rsi-r5-material.png"))
    monkeypatch.setattr(widget, "_selected", lambda: ["example"])
    monkeypatch.setattr("pptx_finder.ui.material_window.owner_actions.ask", lambda *args: True)
    monkeypatch.setattr(widget.library, "delete", lambda _: None)
    messages = []
    monkeypatch.setattr(widget, "_refresh_after", messages.append)
    monkeypatch.setattr(widget, "_run", lambda work, done, label: done(work()))
    widget._delete()
    assert messages == ["已移出素材库；文件仍保留在素材库的回收区。"]


@pytest.fixture
def image_window(qtbot, monkeypatch):
    center = cc.ComponentCenter()
    monkeypatch.setattr(cc, "component_center", lambda: center)
    monkeypatch.setattr(imgtext_window.imgtext_ocr, "is_installed", lambda: False)
    widget = imgtext_window.ImgTextWindow({})
    qtbot.addWidget(widget)
    return widget, center


def test_preview_click_selects_image_and_busy_blocks_click(image_window, qtbot, monkeypatch):
    widget, _ = image_window
    picks = []
    monkeypatch.setattr(widget, "_pick", lambda: picks.append(1))
    qtbot.mouseClick(widget._drop, Qt.LeftButton)
    assert picks == [1]
    widget._busy = True
    qtbot.mouseClick(widget._drop, Qt.LeftButton)
    assert picks == [1]


def test_convert_requests_component_and_retains_source_on_failure(image_window, tmp_path, monkeypatch):
    widget, center = image_window
    assert not widget._convert_btn.isEnabled()
    path = tmp_path / "source.png"
    image = QImage(100, 50, QImage.Format_RGB32)
    image.fill(Qt.white)
    assert image.save(str(path))
    widget.set_source(str(path))
    assert widget._convert_btn.isEnabled()
    assert "下载" in widget._status.text() and "生成可编辑 PPT" in widget._status.text()
    widget.setStyleSheet(theme.build_qss("atelier") +
                        f"QWidget#imgTextWin {{ background: {theme.tok('atelier')['appbg']}; }}")
    widget.show()
    # 绘制就绪后的窗口用于检查缺组件提示与可点击的生成按钮。
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    assert widget.grab().save(str(Path(__file__).resolve().parents[1] / "artifacts/rsi-r5-imgtext.png"))
    calls = []
    monkeypatch.setattr(center, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(widget, "_start", lambda *args: pytest.fail("缺组件时不能开始识别"))
    widget._convert_btn.click()
    assert len(calls) == 1 and calls[0][0] == ("ocr", widget)
    for phase in ("cancelled", "error"):
        center._apply("ocr", {"phase": phase, "error": "网络超时"})
        assert widget._source == str(path) and widget._convert_btn.isEnabled()
    monkeypatch.setattr(imgtext_window.imgtext_ocr, "is_installed", lambda: True)
    center._apply("ocr", {"phase": "done"})
    assert widget._component_box.isHidden() and widget._convert_btn.isEnabled()
