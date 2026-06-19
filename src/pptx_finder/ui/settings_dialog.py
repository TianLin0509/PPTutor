"""设置面板：受管文件夹（增删）+ 开机自启。

风格 / 全局热键等设置留给主窗（避免与并发 UI 改动冲突）；本面板聚焦版本管理配置。
全局 QSS（主窗主题）会自动套用到这些标准控件上。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from ..versioning import autostart


class SettingsDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self.setWindowTitle("设置 · 版本管理")
        self.resize(540, 440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        title = QLabel("受管文件夹")
        title.setStyleSheet("font-weight:700;font-size:14px;")
        lay.addWidget(title)
        lay.addWidget(QLabel("这些文件夹里的所有 PPTX 会被自动版本管理——你正常保存即留版本，无需任何操作。"))

        self.root_list = QListWidget()
        lay.addWidget(self.root_list, 1)

        btns = QHBoxLayout()
        add = QPushButton("添加文件夹…")
        add.setObjectName("primary")
        add.clicked.connect(self._add)
        rm = QPushButton("移除所选")
        rm.clicked.connect(self._remove)
        btns.addWidget(add)
        btns.addWidget(rm)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.auto = QCheckBox("开机自动启动（后台守护版本，强烈建议开启）")
        self.auto.setChecked(autostart.is_enabled())
        self.auto.toggled.connect(self._toggle_auto)
        lay.addWidget(self.auto)

        self._refresh()

    def _refresh(self) -> None:
        self.root_list.clear()
        self.root_list.addItems(self._mgr.list_roots())

    def _add(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要做版本管理的文件夹")
        if d:
            self._mgr.add_root(d)
            self._mgr.restart_watcher()
            self._refresh()

    def _remove(self) -> None:
        it = self.root_list.currentItem()
        if it:
            self._mgr.remove_root(it.text())
            self._mgr.restart_watcher()
            self._refresh()

    def _toggle_auto(self, on: bool) -> None:
        autostart.set_enabled(on)
