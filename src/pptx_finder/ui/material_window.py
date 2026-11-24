"""素材库独立窗口：薄兼容壳，核心视图就是一级页 MaterialTabWidget。

主窗已把素材升为一级页；这个窗口只留给旧入口（托盘、命令行、旧测试）独立打开时用。
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..materials import MaterialLibrary
from .material_tab import MaterialTabWidget

log = logging.getLogger(__name__)


class MaterialWindow(QWidget):
    def __init__(self, tok=None, parent=None, *, library=None, component_center=None):
        super().__init__(parent)
        self.setObjectName("materialWin")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle("素材库 · PPT Doctor")
        self.resize(1280, 760)
        self.setMinimumSize(960, 600)
        self.library = library or MaterialLibrary()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tab = MaterialTabWidget(self, theme_tokens=tok, library=self.library,
                                     component_center=component_center)
        layout.addWidget(self.tab, 1)
        # 主窗里由主窗画通透背景；独立窗口自己铺页底色，不透出系统深色调色板
        self.setStyleSheet(f"QWidget#materialWin {{ background: {self.tab.tok['appbg']}; }}")
        # 独立窗口没有主窗的 Toast：消息落在底部一行
        self.toast_line = QLabel("")
        self.toast_line.setObjectName("emptyMeta")
        self.toast_line.setContentsMargins(20, 0, 20, 10)
        self.toast_line.setTextFormat(Qt.PlainText)
        layout.addWidget(self.toast_line)
        self.tab.toastRequested.connect(lambda message, _level: self.toast_line.setText(message))

    def closeEvent(self, event):
        if self.tab._busy_op:
            self.toast_line.setText("操作仍在进行，完成后即可关闭窗口。")
            event.ignore()
            return
        self.tab.close()
        super().closeEvent(event)
