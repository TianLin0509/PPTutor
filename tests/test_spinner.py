"""方向 08：预览加载 spinner —— 渲染中柔和动画，不再干巴巴一行字。"""
from __future__ import annotations

from test_ui import StubRender, _index

from pptx_finder.ui.main_window import MainWindow


def test_spinner_active_and_text(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._start_spinner()
    assert win._spin_timer.isActive()
    assert "PowerPoint" in win.image_label.text()        # 首次预览说明（P2-1）
    win._preview_hinted = True
    win._tick_spinner()
    assert "正在等待" in win.image_label.text()
    assert "COM 原图渲染" in win.image_label.text()


def test_spinner_stops(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._start_spinner()
    win._stop_spinner()
    assert not win._spin_timer.isActive()
