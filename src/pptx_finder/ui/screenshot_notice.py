"""Small progress/completion feedback which does not take keyboard focus."""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class ScreenshotNotice(QWidget):
    stop_requested = Signal()
    small_requested = Signal()

    def __init__(self):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating,True)
        self.setAttribute(Qt.WA_StyledBackground,True)
        self.setObjectName('screenshotNotice')
        self.setStyleSheet('#screenshotNotice { background: #fff; border: 1px solid #dfe5ed; border-radius: 6px; } QLabel { color: #243249; background: transparent; } QPushButton { color: #2867bd; background: #edf3fc; border: 0; padding: 6px; }')
        row = QHBoxLayout(self)
        row.setContentsMargins(16,12,12,12)
        self.label = QLabel()
        self.label.setWordWrap(True)
        row.addWidget(self.label,1)
        self.stop_btn = QPushButton('停止')
        self.stop_btn.clicked.connect(self.stop_requested)
        row.addWidget(self.stop_btn)
        self.small_btn = QPushButton('小图模式')
        self.small_btn.clicked.connect(self.small_requested)
        row.addWidget(self.small_btn)
        self.small_btn.hide()
        self.resize(380,80)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def progress(self,text,screen=None,stoppable=False):
        self._timer.stop()
        self.label.setText(text)
        self.stop_btn.setVisible(stoppable)
        self.small_btn.hide()
        screen = screen or QGuiApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.right()-self.width()-16,area.bottom()-self.height()-16)
        self.show()

    def complete(self,text,*,allow_small=False):
        self.label.setText(text)
        self.stop_btn.hide()
        self.small_btn.setVisible(allow_small)
        self.show()
        self._timer.start(8000 if allow_small else 2800)
