"""托盘 / 截图窗「快捷键」入口：没有已开的设置窗时也要能打开到指定分类。"""
from __future__ import annotations

from pptx_finder import db


def test_open_settings_page_without_existing_dialog(qtbot, tmp_path, monkeypatch):
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    conn = db.connect(tmp_path / "s.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, do_index=False)
    qtbot.addWidget(win)
    win.open_settings_page("常规")
    assert len(win._settings_dialogs) == 1
    dlg = win._settings_dialogs[0]
    assert dlg.isVisible()
    dlg.close()
    conn.close()
