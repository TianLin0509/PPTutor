"""Editable text result; installation and OCR run in the owner controller."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPlainTextEdit,QPushButton
from .. import __version__
from .capture_style import RESULT_STYLE,primary,result_margins


class ScreenshotText(QDialog):
    copy_requested=Signal(str)
    retry_requested=Signal()
    install_requested=Signal()

    def __init__(self,parent):
        super().__init__(parent)
        self.setWindowTitle(f'截图文字 · PPT Doctor {__version__}')
        self.setModal(False)
        self.setAttribute(Qt.WA_StyledBackground,True)
        self.setStyleSheet(RESULT_STYLE)
        layout=QVBoxLayout(self);result_margins(layout)
        self.status=QLabel('识别在本机完成。数字、公式和代码请核对后使用。')
        self.status.setWordWrap(True);layout.addWidget(self.status)
        self.editor=QPlainTextEdit();layout.addWidget(self.editor,1)
        row=QHBoxLayout()
        self.copy_btn=primary(QPushButton('复制文字'));self.copy_btn.clicked.connect(lambda:self.copy_requested.emit(self.editor.toPlainText()))
        self.retry_btn=QPushButton('重新识别');self.retry_btn.clicked.connect(self.retry_requested)
        self.install_btn=QPushButton('下载本地识别组件');self.install_btn.clicked.connect(self.install_requested)
        for button in [self.copy_btn,self.retry_btn,self.install_btn]:row.addWidget(button)
        layout.addLayout(row)
        self.resize(700,450)
        screen=self.screen()
        if screen:self.resize(min(700,screen.availableGeometry().width()-32),min(450,screen.availableGeometry().height()-32))

    def set_busy(self,busy):
        for button in [self.copy_btn,self.retry_btn,self.install_btn]:button.setEnabled(not busy)
