"""主题与本机深浅模式脱钩：colorScheme 钉住 + 全局 QPalette 来自应用 token。

根因：Qt 6.5+ 在 Windows 默认跟随系统深浅模式；本应用只设了全局 QSS，
未覆盖的控件回退到系统调色板——本机深色主题下出现"黑框深字看不清"。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLineEdit

from pptx_finder.ui import theme


def _color_scheme_supported(app) -> bool:
    """当前 QPA 插件是否真的实现 setColorScheme。

    offscreen 插件不实现，读回恒为 Unknown——这条断言会在任何 headless 跑法下
    （CI、smoke 脚本）稳定变红，而原生 Windows 下是绿的。按「平台名」跳过不够准，
    直接探一次能力：探不到就只验 palette（那部分与 QPA 无关，仍是本用例的主证据）。
    """
    before = app.styleHints().colorScheme()
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
        return app.styleHints().colorScheme() == Qt.ColorScheme.Light
    finally:
        app.styleHints().setColorScheme(before)


def _contrast(c1, c2) -> float:
    def lum(c):
        def chan(v):
            v = v / 255.0
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * chan(c.red()) + 0.7152 * chan(c.green()) + 0.0722 * chan(c.blue())
    l1, l2 = sorted((lum(c1), lum(c2)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def test_apply_to_app_pins_color_scheme_and_palette(qtbot):
    app = QApplication.instance()
    old_scheme, old_pal, old_qss = app.styleHints().colorScheme(), app.palette(), app.styleSheet()
    scheme_supported = _color_scheme_supported(app)
    try:
        theme.apply_to_app(app, "atelier")       # 静白（light）
        if scheme_supported:
            assert app.styleHints().colorScheme() == Qt.ColorScheme.Light
        pal = app.palette()
        t = theme.tok("atelier")
        assert pal.color(QPalette.Window).name() == t["win"].lower()
        assert pal.color(QPalette.Text).name() == t["ink1"].lower()

        theme.apply_to_app(app, "aurora")        # 深色玻璃
        if scheme_supported:
            assert app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        pal = app.palette()
        t = theme.tok("aurora")
        assert pal.color(QPalette.Window).name() == t["win"].lower()
    finally:
        app.setPalette(old_pal)
        app.styleHints().setColorScheme(old_scheme)
        app.setStyleSheet(old_qss)


def test_all_themes_unstyled_field_keeps_readable_contrast(qtbot):
    """无 objectName 的通用输入框不在任何 QSS 选择器内，只能吃全局 palette：
    12 套主题下其前景/背景对比度必须全部达标——本机深色主题渗不进来的判据。"""
    app = QApplication.instance()
    old_pal, old_scheme = app.palette(), app.styleHints().colorScheme()
    try:
        for name, _label in theme.THEMES:
            theme.apply_to_app(app, name)
            w = QLineEdit()
            fg = w.palette().color(w.foregroundRole())
            bg = w.palette().color(w.backgroundRole())
            assert _contrast(fg, bg) >= 3.0, f"{name}: 未覆盖控件对比度不足"
    finally:
        app.setPalette(old_pal)
        app.styleHints().setColorScheme(old_scheme)
