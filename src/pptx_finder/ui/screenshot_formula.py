"""A white formula workspace: original, rendered result and two copy formats."""
from PySide6.QtCore import Signal,Qt,QSignalBlocker
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPlainTextEdit,QPushButton
from .. import __version__


class ScreenshotFormula(QDialog):
    copy_requested=Signal(str)
    refresh_requested=Signal()
    retry_requested=Signal()
    install_requested=Signal()

    def __init__(self,parent):
        super().__init__(parent)
        self.setWindowTitle(f'识别公式 · PPT Doctor {__version__}');self.setModal(False)
        self.setAttribute(Qt.WA_StyledBackground,True)
        self.setStyleSheet('QDialog { background: #fff; color: #243249; } QLabel { color: #243249; } QPlainTextEdit { background: #f7f9fc; color: #243249; border: 1px solid #dfe5ed; border-radius: 5px; padding: 6px; } QPushButton { color: #2867bd; background: #edf3fc; padding: 8px 12px; border: 1px solid #dfe5ed; border-radius: 5px; } QPushButton:disabled { color: #87909e; background: #f3f5f8; }')
        layout=QVBoxLayout(self);layout.setContentsMargins(22,18,22,18);layout.setSpacing(10)
        self.status=QLabel('请框选单个清晰公式。识别在本机完成；请对照原图核对。');self.status.setWordWrap(True);layout.addWidget(self.status)
        pictures=QHBoxLayout()
        self.source=QLabel('原图');self.preview=QLabel('识别预览')
        for title,label in [('截图原图',self.source),('公式预览',self.preview)]:
            column=QVBoxLayout();column.addWidget(QLabel(title))
            label.setAlignment(Qt.AlignCenter);label.setMinimumSize(160,130);label.setMaximumHeight(180)
            label.setStyleSheet('background: #f7f9fc; color: #626b78; border: 1px solid #dfe5ed; border-radius: 6px;')
            column.addWidget(label);pictures.addLayout(column,1)
        layout.addLayout(pictures)
        layout.addWidget(QLabel('LaTeX · 可修改后更新预览'))
        self.editor=QPlainTextEdit();self.editor.setMaximumHeight(105);layout.addWidget(self.editor)
        layout.addWidget(QLabel('Word 公式输入 · UnicodeMath'))
        self.word=QPlainTextEdit();self.word.setReadOnly(True);self.word.setMaximumHeight(75);layout.addWidget(self.word)
        guide=QLabel('Word：Alt+= 插入公式框 → 选择 UnicodeMath → Ctrl+V → 转换为“专业”格式。');guide.setWordWrap(True);layout.addWidget(guide)
        row=QHBoxLayout()
        self.latex_btn=QPushButton('复制 LaTeX');self.latex_btn.setStyleSheet('background: #2867bd; color: #fff;')
        self.word_btn=QPushButton('复制 Word 公式');self.refresh_btn=QPushButton('更新预览');self.retry_btn=QPushButton('重新识别')
        self.install_btn=QPushButton('下载公式识别组件')
        self.latex_btn.clicked.connect(lambda:self.copy_requested.emit('latex'));self.word_btn.clicked.connect(lambda:self.copy_requested.emit('word'))
        self.refresh_btn.clicked.connect(self.refresh_requested);self.retry_btn.clicked.connect(self.retry_requested);self.install_btn.clicked.connect(self.install_requested)
        for button in [self.latex_btn,self.word_btn,self.refresh_btn,self.retry_btn,self.install_btn]:row.addWidget(button)
        layout.addLayout(row)
        self.editor.textChanged.connect(self.modified)
        self._busy=False;self.install_btn.hide();self.update_controls();self.resize(860,600)
        if self.screen():
            available=self.screen().availableGeometry();self.resize(min(860,available.width()-32),min(600,available.height()-32))

    def modified(self):
        self.word.clear();self.preview.clear();self.preview.setText('内容已修改，请更新预览')
        self.status.setText('内容已修改，请更新预览并核对后复制。');self.update_controls()

    def set_latex(self,text):
        with QSignalBlocker(self.editor):self.editor.setPlainText(text)

    def set_image(self,label,image):
        label.setPixmap(QPixmap.fromImage(image).scaled(max(160,label.width()-16),150,Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def update_controls(self):
        self.latex_btn.setEnabled(not self._busy and bool(self.editor.toPlainText().strip()))
        self.word_btn.setEnabled(not self._busy and bool(self.word.toPlainText().strip()))
        self.refresh_btn.setEnabled(not self._busy and bool(self.editor.toPlainText().strip()))
        for button in [self.retry_btn,self.install_btn]:button.setEnabled(not self._busy)
        self.editor.setEnabled(not self._busy)

    def set_busy(self,busy):self._busy=busy;self.update_controls()
