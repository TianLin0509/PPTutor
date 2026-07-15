"""方向 10：预览缩放 —— Ctrl+滚轮缩放 / 双击放大（普通滚轮翻页不变）。"""
from __future__ import annotations

from PySide6.QtGui import QPixmap

from test_ui import StubRender, _index

from pptx_finder.ui.main_window import MainWindow


def _win(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._cur_pixmap = QPixmap(200, 150)
    return win


def test_initial_zoom_is_fit(qtbot, tmp_path):
    assert _win(qtbot, tmp_path)._zoom == 1.0


def test_zoom_by_increases(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._zoom_by(1.5)
    assert win._zoom > 1.0


def test_rapid_ctrl_wheel_zoom_coalesces_expensive_pixmap_scaling(qtbot, tmp_path, monkeypatch):
    win = _win(qtbot, tmp_path)
    calls = 0

    def record_scale():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(win, "_update_pixmap", record_scale)
    for _ in range(12):
        win._zoom_by(1.05)

    assert calls == 0
    qtbot.waitUntil(lambda: calls == 1, timeout=1000)


def test_zoom_floor_is_fit(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._zoom_by(0.5)
    assert win._zoom == 1.0          # 不缩到比 fit 更小


def test_toggle_zoom(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._toggle_zoom()
    assert win._zoom == 2.0
    win._toggle_zoom()
    assert win._zoom == 1.0


def test_new_preview_resets_zoom(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._zoom = 2.5
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)   # 选中触发 _request_preview → 重置 fit
    assert win._zoom == 1.0
