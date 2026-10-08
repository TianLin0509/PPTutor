"""Small recording controls and an animated, file-backed result preview."""
from pathlib import Path

from PySide6.QtCore import QPoint, QSaveFile, QIODevice, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMovie
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from .capture_style import STYLE, icon, setup_button, shadow


class RecordPanel(QDialog):
    pause_requested = Signal()
    stop_requested = Signal()
    discard_requested = Signal()

    def __init__(self, owner=None):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('录制 GIF · PPT Doctor')
        self.setObjectName('recordPanel')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(STYLE + 'QDialog#recordPanel { background: transparent; }')
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        outer = QVBoxLayout(self); outer.setContentsMargins(12,12,12,12)
        surface = QWidget(); surface.setObjectName('captureToolbar')
        surface.setAttribute(Qt.WA_StyledBackground, True); shadow(surface)
        outer.addWidget(surface)
        root = QVBoxLayout(surface); root.setContentsMargins(18,14,18,14); root.setSpacing(10)
        header = QHBoxLayout(); header.setSpacing(8)
        emblem = QLabel(); emblem.setPixmap(icon('record', '#e5484d').pixmap(20,20))
        header.addWidget(emblem)
        title = QLabel('录制 GIF'); title.setStyleSheet('font-size: 13px; font-weight: 600;')
        header.addWidget(title)
        self.state_badge = QLabel('准备中'); self.state_badge.setProperty('role','badge')
        header.addWidget(self.state_badge); header.addStretch(1)
        self.clock = QLabel('00:00')
        self.clock.setStyleSheet('font-family: "Segoe UI"; font-size: 20px; font-weight: 600; color: #1d2939;')
        header.addWidget(self.clock); root.addLayout(header)
        self.label = QLabel('准备录制')
        self.label.setWordWrap(True)
        root.addWidget(self.label)
        self.details = QLabel('10 帧/秒目标 · 最长 2 分钟 · 原始分辨率 · 无声音')
        self.details.setWordWrap(True)
        self.details.setProperty('role', 'muted')
        root.addWidget(self.details)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.pause_btn = setup_button(QPushButton('暂停'), 'pause', role='subtle')
        self.stop_btn = setup_button(QPushButton('停止并保存'), 'stop', role='primary')
        self.discard_btn = setup_button(QPushButton('放弃'), 'close', role='danger')
        self.discard_btn.setToolTip('放弃本次录制，不保存 GIF')
        for button in [self.pause_btn, self.stop_btn, self.discard_btn]:
            row.addWidget(button)
        root.addLayout(row)
        self.pause_btn.clicked.connect(self.pause_requested)
        self.stop_btn.clicked.connect(self.stop_requested)
        self.discard_btn.clicked.connect(self.discard_requested)
        self.setFixedWidth(430)
        self._drag = None

    def update_state(self, state, text, elapsed_ms=0):
        self.label.setText(text)
        seconds = max(0, elapsed_ms//1000)
        self.clock.setText(f'{seconds//60:02d}:{seconds%60:02d}')
        self.state_badge.setText({'countdown':'准备中','recording':'录制中','paused':'已暂停','saving':'保存中'}.get(state,'已结束'))
        self.pause_btn.setEnabled(state in ('recording', 'paused'))
        self.pause_btn.setText('继续' if state == 'paused' else '暂停')
        self.pause_btn.setIcon(icon('play' if state == 'paused' else 'pause'))
        self.stop_btn.setEnabled(state in ('countdown', 'recording', 'paused'))
        self.discard_btn.setEnabled(state in ('countdown', 'recording', 'paused', 'saving'))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)

    def reject(self):
        # Esc or the native close button stops and saves instead of losing work.
        self.stop_requested.emit()

    def closeEvent(self, event):
        self.stop_requested.emit()
        event.ignore()


class RecordResult(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setObjectName('recordResult')
        self.setWindowTitle('GIF 录制结果 · PPT Doctor')
        self.setStyleSheet(STYLE)
        self.path = None
        self.movie = None
        root = QVBoxLayout(self)
        root.setContentsMargins(22,20,22,20); root.setSpacing(14)
        title = QLabel('录制已完成'); title.setStyleSheet('font-size: 20px; font-weight: 600;')
        root.addWidget(title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(480, 260)
        self.preview.setStyleSheet('background: #f6f8fb; border: 1px solid #e4e7ec; border-radius: 12px; padding: 8px;')
        root.addWidget(self.preview)
        self.location = QLabel()
        self.location.setWordWrap(True)
        self.location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.location.setProperty('role', 'muted')
        root.addWidget(self.location)
        row = QHBoxLayout()
        self.open_btn = setup_button(QPushButton('打开 GIF'), 'play', role='primary')
        self.folder_btn = setup_button(QPushButton('打开文件夹'), 'folder', role='subtle')
        self.save_btn = setup_button(QPushButton('另存为…'), 'save', role='subtle')
        for button in [self.open_btn, self.folder_btn, self.save_btn]:
            row.addWidget(button)
        root.addLayout(row)
        self.open_btn.clicked.connect(lambda: self._open(self.path))
        self.folder_btn.clicked.connect(lambda: self._open(self.path.parent if self.path else None))
        self.save_btn.clicked.connect(self.save_as)
        self.resize(640, 440)

    def _open(self, path):
        if path and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.status.setText('无法打开，请从下方路径找到 GIF 文件。')

    def display(self, path, message='', *, show=True):
        self.path = Path(path)
        if self.movie is not None:
            self.movie.stop()
            self.movie.deleteLater()
        self.movie = QMovie(str(path), parent=self)
        self.movie.setCacheMode(QMovie.CacheNone)
        from PySide6.QtGui import QImageReader
        size = QImageReader(str(path)).size()
        ratio = self.preview.devicePixelRatioF()
        size.scale(round(600 * ratio), round(310 * ratio), Qt.KeepAspectRatio)
        self.movie.setScaledSize(size)
        self.movie.frameChanged.connect(self._preview_frame)
        self.movie.start()
        self.status.setText(f'GIF 已保存 · {self.path.stat().st_size / 1024:.1f} KB'
                            + (f' · {message}' if message else ''))
        self.location.setText(str(self.path))
        if show:
            self.show()
            self.raise_()
        else:
            self.movie.setPaused(True)

    def _preview_frame(self):
        if self.movie is not None:
            pixmap = self.movie.currentPixmap()
            pixmap.setDevicePixelRatio(self.preview.devicePixelRatioF())
            self.preview.setPixmap(pixmap)

    def save_as(self):
        if self.path is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, '另存录制 GIF', str(self.path), 'GIF 动图 (*.gif)')
        if not filename:
            return
        target = Path(filename)
        if target.suffix.lower() != '.gif':
            self.status.setText('当前只导出 GIF，请将文件扩展名设为 .gif。')
            return
        source = self.path
        self.save_btn.setEnabled(False)
        self.status.setText('正在另存 GIF…')
        def work():
            try:
                output = QSaveFile(str(target))
                if not output.open(QIODevice.WriteOnly):
                    raise OSError(output.errorString())
                with source.open('rb') as data:
                    while block := data.read(1024 * 1024):
                        if output.write(block) != len(block):
                            output.cancelWriting()
                            raise OSError(output.errorString())
                if not output.commit():
                    raise OSError(output.errorString())
                return ''
            except Exception as exc:
                return str(exc)
        def done(error):
            self.save_btn.setEnabled(True)
            self.status.setText(f'另存失败：{error or "请重试"}' if error is None or error else f'已另存：{target}')
        self.parentWidget()._run(work, done, 'screenshot-gif-save-as')

    def showEvent(self, event):
        if self.movie is not None:
            self.movie.setPaused(False)
        super().showEvent(event)

    def hideEvent(self, event):
        if self.movie is not None:
            self.movie.setPaused(True)
        super().hideEvent(event)
