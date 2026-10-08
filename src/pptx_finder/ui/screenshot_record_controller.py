"""Recording integration, monitor binding, clean capture and result delivery."""
import ctypes
import sys
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QStandardPaths, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from .record_capture import RecordCapture, shutdown_writers
from .record_panel import RecordPanel, RecordResult
from .screenshot_overlay import crop_physical


def recording_directory():
    if sys.platform == 'win32' and Path('D:/').is_dir():
        return Path('D:/AI-Artifacts/PPT-Doctor/Recordings')
    return Path(QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)) / 'PPT-Doctor/Recordings'


def exclude_from_capture(widget):
    if sys.platform != 'win32' or sys.getwindowsversion().build < 19041:
        return False
    native = ctypes.WinDLL('user32', use_last_error=True)
    native.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    native.SetWindowDisplayAffinity.restype = ctypes.c_int
    return bool(native.SetWindowDisplayAffinity(int(widget.winId()), 0x11))


class ScreenshotRecordController(QObject):
    def __init__(self, owner, *, output_root=None):
        super().__init__(owner)
        self.owner = owner
        self.output_root = Path(output_root) if output_root else recording_directory()
        self.panel = RecordPanel(owner)
        self.result = RecordResult(owner)
        self.engine = None
        self._closed = False
        self._discarded = False
        self._excluded = False
        self.panel.pause_requested.connect(self.pause)
        self.panel.stop_requested.connect(self.stop)
        self.panel.discard_requested.connect(lambda: self.stop(discard=True))
        app = QApplication.instance()
        if not getattr(app, '_gif_shutdown_connected', False):
            app.aboutToQuit.connect(shutdown_writers)
            app._gif_shutdown_connected = True

    def start(self, local, geometry):
        try:
            self._start(local, geometry)
        except Exception as exc:
            if self.engine is not None:
                self.engine.close()
            self.panel.hide()
            if not self.owner._closed:
                self._failed(str(exc))

    def _start(self, local, geometry):
        if self.owner._closed:
            return
        screen = next((s for s in QGuiApplication.screens() if s.geometry() == geometry), None)
        if screen is None:
            self._failed('显示器配置已改变，请重新框选')
            return
        self._closed = self._discarded = False
        self.result.hide()
        self.local, self.geometry = QRect(local), QRect(geometry)
        self.screen = screen
        self._frame_size = screen.grabWindow(0).size()
        if self._frame_size.isEmpty():
            self._failed('无法读取显示器画面')
            return
        self.native_capture = None
        if sys.platform == 'win32':
            from ..screenshots.record_native import NativeRegionCapture
            self.native_capture = NativeRegionCapture(screen, local, self._frame_size)
        path = self.output_root / ('PPT录制-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.gif')
        self.panel.show()
        self.panel.adjustSize()
        target = local.translated(geometry.topLeft())
        self._place_panel(target, screen.availableGeometry())
        self._excluded = exclude_from_capture(self.panel)
        if self.engine is not None:
            self.engine.deleteLater()
        self.engine = RecordCapture(self._capture, path, parent=self)
        engine = self.engine
        engine.progress.connect(lambda text: self._progress(text) if self.engine is engine else None)
        engine.finished.connect(lambda path, text: self._finished(path, text) if self.engine is engine else None)
        self.engine.start()

    def _place_panel(self, target, bounds):
        w, h = self.panel.width(), self.panel.height()
        x = max(bounds.left(), min(target.left(), bounds.right() - w + 1))
        candidates = [QRect(x, target.bottom() + 10, w, h), QRect(x, target.top() - h - 10, w, h),
                      QRect(target.right() + 10, target.top(), w, h), QRect(target.left() - w - 10, target.top(), w, h)]
        chosen = next((r for r in candidates if bounds.contains(r) and not target.intersects(r)), None)
        if chosen is None:
            chosen = QRect(bounds.right() - w - 12, bounds.bottom() - h - 12, w, h)
        self.panel.move(chosen.topLeft())

    def _capture(self):
        if self.screen.geometry() != self.geometry:
            raise ValueError('显示器配置已改变')
        overlap = self.panel.isVisible() and self.panel.frameGeometry().intersects(self.local.translated(self.geometry.topLeft()))
        hidden = overlap and not self._excluded
        try:
            if hidden:
                self.panel.hide()
                QGuiApplication.processEvents()
            if self.native_capture is not None:
                return self.native_capture.grab()
            frame = self.screen.grabWindow(0)
            if frame.size() != self._frame_size:
                raise ValueError('显示器分辨率已改变')
            return crop_physical(frame.toImage(), self.local, self.geometry)
        finally:
            if hidden and not self._closed and self.engine.state in ('recording', 'paused'):
                self.panel.show()

    def _progress(self, text):
        if not self._closed:
            self.panel.update_state(self.engine.state, text)

    def pause(self):
        if self.engine is not None:
            self.engine.pause()

    def stop(self, *, discard=False):
        if self.engine is not None:
            self._discarded = discard or self._discarded
            self.engine.stop(discard=discard)

    def _failed(self, message):
        self.owner._set_busy(False)
        self.owner.status.setText('录制失败：' + message)
        self.owner.show()

    def _finished(self, path, message):
        self.panel.hide()
        if self._closed:
            return
        self.owner._set_busy(False)
        if path:
            self.owner.status.setText(f'GIF 已保存：{path}')
            self.result.display(path, message)
            self.owner._set_busy(False)
        elif self._discarded or '取消' in message:
            self.owner.status.setText('已取消录制；剪贴板未修改。')
        else:
            self._failed(message or '未录到画面，请重试')

    def show(self):
        if self.result.path:
            self.result.show()
            self.result.raise_()

    def close(self):
        self._closed = True
        if self.engine is not None:
            self.engine.close()
        self.panel.hide()
        self.result.hide()
