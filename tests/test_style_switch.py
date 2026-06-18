"""主界面风格切换：_apply_theme 切到新风格 + 按钮文案显示风格名。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from pptx_finder import db
from pptx_finder.ui import theme
from pptx_finder.ui.main_window import MainWindow


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def _win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    return win


def test_apply_cinema_theme(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._apply_theme("cinema")
    assert win._theme == "cinema"
    assert win._tok["acc"] == theme.tok("cinema")["acc"]
    assert "深空影院" in win.theme_btn.text()


def test_button_shows_each_style_name(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._apply_theme("morandi")
    assert "莫兰迪奶油" in win.theme_btn.text()
    win._apply_theme("aurora")
    assert "极光玻璃" in win.theme_btn.text()


def test_apply_all_themes_no_crash(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    for name, _ in theme.THEMES:
        win._apply_theme(name)
        assert win._theme == name
