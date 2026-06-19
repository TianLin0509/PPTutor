"""设置面板：版本管理是全盘自动的，这里只放说明 + 开机自启开关（零配置）。

全局 QSS（主窗主题）会自动套用到这些标准控件上。
"""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QLabel, QVBoxLayout

from ..versioning import autostart


class SettingsDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self.setWindowTitle("设置 · 版本管理")
        self.resize(480, 280)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)

        title = QLabel("版本管理")
        title.setStyleSheet("font-weight:700;font-size:15px;")
        lay.addWidget(title)

        desc = QLabel(
            "全盘自动守护——你用 PowerPoint 改过、保存过的 PPT 会自动留版本，"
            "无需任何设置，没动过的旧文件不占空间。\n\n"
            "只有两种文件进入管理：① 之后新建的 PPT　② 之后改存过的老 PPT"
            "（改后这一版作为第 1 版，之前的不追踪）。"
        )
        desc.setWordWrap(True)
        lay.addWidget(desc)

        try:
            n = len(self._mgr.list_docs())
        except Exception:  # noqa: BLE001
            n = 0
        self.stat = QLabel(f"目前已在守护 {n} 个你改过的文件。")
        lay.addWidget(self.stat)

        self.auto = QCheckBox("开机自动启动（建议开启，这样关机后也持续守护）")
        self.auto.setChecked(autostart.is_enabled())
        self.auto.toggled.connect(self._toggle_auto)
        lay.addWidget(self.auto)

        lay.addStretch(1)

    def _toggle_auto(self, on: bool) -> None:
        autostart.set_enabled(on)
