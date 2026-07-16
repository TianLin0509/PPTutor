"""Regression tests for the shared desktop visual system."""
from __future__ import annotations

import shutil

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QVBoxLayout, QWidget

from pptx_finder.config import resource_path
from pptx_finder.ui import theme


def test_scrollbars_cover_both_axes_and_all_interaction_states():
    qss = theme.build_qss("cloud")

    assert "QScrollBar:vertical" in qss
    assert "QScrollBar:horizontal" in qss
    assert "QScrollBar::handle:vertical:pressed" in qss
    assert "QScrollBar::handle:horizontal:pressed" in qss
    assert "QScrollBar::add-line:vertical" in qss
    assert "QScrollBar::add-line:horizontal" in qss
    assert "QAbstractScrollArea::corner" in qss


def test_shared_controls_have_polished_popup_focus_and_editor_states():
    qss = theme.build_qss("cloud")

    assert "QComboBox::down-arrow" in qss
    assert "QComboBox:on" in qss
    assert "QComboBox QAbstractItemView::item:hover" in qss
    assert "QPlainTextEdit" in qss
    assert "QTextEdit" in qss
    assert "QTabWidget::pane" in qss
    assert "QPushButton:pressed" in qss
    assert "padding-top: 8px" not in qss


def test_packaged_control_icons_exist_and_are_referenced():
    qss = theme.build_qss("cloud")
    expected = (
        resource_path("assets", "ui-chevron-dark.svg"),
        resource_path("assets", "ui-chevron-light.svg"),
        resource_path("assets", "ui-check.svg"),
    )
    for path in expected:
        assert path.is_file(), path

    assert "ui-chevron-dark.svg" in qss
    assert "ui-check.svg" in qss
    assert "file:///" not in qss  # Qt QSS 把 file: URL 当相对路径；Windows 盘符绝对路径已实测可用


def test_all_themes_build_the_same_complete_control_surface():
    for name, _label in theme.THEMES:
        qss = theme.build_qss(name)
        assert "QScrollBar:horizontal" in qss, name
        assert "QComboBox::down-arrow" in qss, name
        assert "QCheckBox::indicator:checked" in qss, name
        assert "QPlainTextEdit" in qss, name


def test_qss_icons_render_from_path_with_spaces_and_chinese(qtbot, tmp_path, monkeypatch):
    """Exercise Qt's real QSS loader; string-only assertions miss URL parsing failures."""
    fake_root = tmp_path / "中文 用户" / "PPT Doctor"
    asset_dir = fake_root / "assets"
    asset_dir.mkdir(parents=True)
    for name in ("ui-chevron-dark.svg", "ui-chevron-light.svg", "ui-check.svg"):
        shutil.copyfile(resource_path("assets", name), asset_dir / name)

    monkeypatch.setattr(theme, "resource_path", lambda *parts: fake_root.joinpath(*parts))
    messages: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(str(message))
    )
    app = QApplication.instance()
    assert app is not None
    old_qss = app.styleSheet()
    try:
        app.setStyleSheet(theme.build_qss("cloud"))
        host = QWidget()
        layout = QVBoxLayout(host)
        combo = QComboBox()
        combo.addItems(["PPT", "全部"])
        checkbox = QCheckBox("开机自动启动")
        checkbox.setChecked(True)
        layout.addWidget(combo)
        layout.addWidget(checkbox)
        qtbot.addWidget(host)
        host.show()
        qtbot.wait(30)
        assert not host.grab().isNull()
    finally:
        app.setStyleSheet(old_qss)
        qInstallMessageHandler(previous_handler)

    failures = [message for message in messages if "Cannot open file" in message]
    assert failures == []
