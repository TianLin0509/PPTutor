"""Small recording controls and an animated, file-backed result preview."""
from pathlib import Path

from PySide6.QtCore import QSaveFile, QIODevice, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMovie
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


STYLE = '''QDialog { background: #fff; color: #243249; }
QLabel { color: #243249; background: transparent; }
QPushButton { color: #2867bd; background: #edf3fc; border: 1px solid #dfe5ed;
              border-radius: 5px; padding: 8px 12px; }
QPushButton:hover { background: #e1ecfb; }
QPushButton:disabled { color: #8b94a2; background: #f2f4f7; }'''


class RecordPanel(QDialog):
    pause_requested = Signal()
    stop_requested = Signal()
    discard_requested = Signal()

    def __init__(self, owner=None):
        super().__init__(owner, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('录制 GIF · PPT Doctor')
        self.setStyleSheet(STYLE)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        root = QVBoxLayout(self)
        self.label = QLabel('准备录制')
        root.addWidget(self.label)
        self.details = QLabel('10 帧/秒目标 · 最长 2 分钟 · 原始分辨率 · GIF 无声音')
        self.details.setStyleSheet('color: #68758a; font-size: 12px;')
        root.addWidget(self.details)
        row = QHBoxLayout()
        self.pause_btn = QPushButton('暂停')
        self.stop_btn = QPushButton('停止并保存')
        self.discard_btn = QPushButton('放弃录制')
        for button in [self.pause_btn, self.stop_btn, self.discard_btn]:
            row.addWidget(button)
        root.addLayout(row)
        self.pause_btn.clicked.connect(self.pause_requested)
        self.stop_btn.clicked.connect(self.stop_requested)
        self.discard_btn.clicked.connect(self.discard_requested)
        self.resize(460, 125)

    def update_state(self, state, text):
        self.label.setText(text)
        self.pause_btn.setEnabled(state in ('recording', 'paused'))
        self.pause_btn.setText('继续' if state == 'paused' else '暂停')
        self.stop_btn.setEnabled(state in ('countdown', 'recording', 'paused'))
        self.discard_btn.setEnabled(state in ('countdown', 'recording', 'paused', 'saving'))

    def reject(self):
        # Esc or the native close button stops and saves instead of losing work.
        self.stop_requested.emit()

    def closeEvent(self, event):
        self.stop_requested.emit()
        event.ignore()


class RecordResult(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle('GIF 录制结果 · PPT Doctor')
        self.setStyleSheet(STYLE)
        self.path = None
        self.movie = None
        root = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(480, 260)
        self.preview.setStyleSheet('background: #f3f5f8; border: 1px solid #dfe5ed;')
        root.addWidget(self.preview)
        self.location = QLabel()
        self.location.setWordWrap(True)
        self.location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.location)
        row = QHBoxLayout()
        self.open_btn = QPushButton('打开 GIF')
        self.folder_btn = QPushButton('打开文件夹')
        self.save_btn = QPushButton('另存为…')
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
