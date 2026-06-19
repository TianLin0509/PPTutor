"""设置面板：全盘自动后只剩说明 + 自启开关，能正常构造。

conftest 已把 PPTX_FINDER_DATA_DIR 指向临时目录，不碰生产 vault。
"""
from __future__ import annotations

import pytest

from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture
def mgr():
    m = VersionManager()
    yield m
    m.stop()


def test_settings_builds_with_autostart_toggle(qtbot, mgr):
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    assert dlg.auto is not None          # 自启开关存在
    assert "守护" in dlg.stat.text()       # 显示已守护文件数
