"""RSI 第 5 轮（2026-11-22）观感：报告首帧数字、浮层下 Toast、设置页对齐、去彩色 emoji。"""
from __future__ import annotations

import re
from pathlib import Path

from pptx_finder.ui.report_overlay import RollNumber
from pptx_finder.ui.settings_dialog import SettingsDialog

UI = Path(__file__).resolve().parent.parent / "src" / "pptx_finder" / "ui"
EMOJI_TITLE = re.compile('["\'][\U0001F300-\U0001FAFF\u2600-\u27BF\u23F1]\uFE0F? ')


def test_roll_number_first_frame_is_not_zero(qtbot):
    n = RollNumber(4, fmt="int")
    qtbot.addWidget(n)
    assert not n.text().startswith("0")


def test_report_and_health_titles_have_no_color_emoji():
    for name in ("report_overlay.py", "health_window.py"):
        assert not EMOJI_TITLE.search((UI / name).read_text(encoding="utf-8")), name


def test_font_custom_scale_box_only_for_custom(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    dlg._font_scale.setCurrentIndex(1)            # 标准（100%）
    assert dlg._font_scale_custom.isHidden()
    dlg._font_scale.setCurrentIndex(dlg._font_scale.count() - 1)   # 自定义…
    assert not dlg._font_scale_custom.isHidden()


def test_version_page_offers_enable_link_when_off(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    dlg.version_feature.setChecked(False)
    dlg._sync_feature_controls()
    assert not dlg._version_enable_link.isHidden()
