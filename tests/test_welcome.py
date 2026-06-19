"""欢迎引导页：首次运行标记 + 欢迎覆盖层。"""
from __future__ import annotations

import pptx_finder.config as config
from pptx_finder.ui.welcome_overlay import WelcomeOverlay


def test_first_run_true_then_false(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path))
    assert config.is_first_run() is True
    config.mark_welcomed()
    assert config.is_first_run() is False


def test_overlay_start_callback(qtbot):
    started = []
    ov = WelcomeOverlay(on_start=lambda: started.append(1),
                        on_pick_theme=lambda n: None, current_theme="raycast")
    qtbot.addWidget(ov)
    ov._start_btn.click()
    assert started == [1]


def test_overlay_theme_pick_calls_back(qtbot):
    picked = []
    ov = WelcomeOverlay(on_start=lambda: None,
                        on_pick_theme=lambda n: picked.append(n), current_theme="cloud")
    qtbot.addWidget(ov)
    ov._theme_btns["cinema"].click()
    assert picked == ["cinema"]


def test_overlay_progress_shows_count(qtbot):
    ov = WelcomeOverlay(on_start=lambda: None, on_pick_theme=lambda n: None, current_theme="raycast")
    qtbot.addWidget(ov)
    ov.update_progress(1234)
    assert "1234" in ov._progress_label.text()


def test_mainwindow_shows_welcome_on_first_run(qtbot, tmp_path, monkeypatch):
    from test_ui import StubRender, _index
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert config.is_first_run() is True            # 全新 data_dir，无标记
    win.maybe_show_welcome()
    assert win._welcome is not None
    win._dismiss_welcome()                          # 点「开始使用」
    assert win._welcome is None
    assert config.is_first_run() is False           # 已写「看过」标记


def test_mainwindow_no_welcome_when_seen(qtbot, tmp_path, monkeypatch):
    from test_ui import StubRender, _index
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    config.mark_welcomed()                          # 预先标记看过
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.maybe_show_welcome()
    assert getattr(win, "_welcome", None) is None
