"""设置面板 _add 大目录保护：盘符根拒绝 / 普通目录登记（防 C 盘全盘 catch_up 卡死回归）。

conftest 已把 PPTX_FINDER_DATA_DIR 指向临时目录，不碰生产 vault。
"""
from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture
def mgr():
    m = VersionManager()
    yield m
    m.stop()


def test_add_rejects_drive_root(qtbot, monkeypatch, mgr):
    """选盘符根（如 C:\\）→ 弹警告并拒绝登记（防全盘 catch_up 卡死）。"""
    root = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep  # 当前盘根，如 C:\
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: root)
    warned: list[int] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(1))
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg._add()
    assert warned, "选盘符根必须弹警告拦截"
    assert root not in mgr.list_roots(), "盘符根绝不能被登记"


def test_add_accepts_normal_dir(qtbot, monkeypatch, mgr, tmp_path):
    """选普通目录 → 立即登记（首版 catch_up 在后台，不阻塞）。"""
    d = str(tmp_path / "work")
    os.makedirs(d)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: d)
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg._add()
    assert os.path.abspath(d) in mgr.list_roots(), "普通目录应被登记"
