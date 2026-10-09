import ctypes
import random
from ctypes import wintypes
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor, QImage
from PySide6.QtWidgets import QMainWindow

from pptx_finder.screenshots import encoding


def photo(w=1200, h=700):
    data = random.Random(42).randbytes(w*h*3)
    return QImage(data, w, h, w*3, QImage.Format_RGB888).copy()


def test_default_keeps_at_most_eight_and_all_original_pixels(qapp):
    image = photo()
    parts = encoding.compress(image)
    assert 1 <= len(parts) <= 8
    assert any(p.size > 50000 for p in parts)
    covered = QImage(image.size(), QImage.Format_RGB32)
    covered.fill(Qt.black)
    from PySide6.QtGui import QPainter
    painter = QPainter(covered)
    for p in parts:
        x, y, w, h = p.region
        assert QImage.fromData(p.data).size() == image.copy(x, y, w, h).size()
        painter.fillRect(x, y, w, h, Qt.white)
    painter.end()
    assert all(covered.pixelColor(x, y).name() == '#ffffff'
               for x in range(0, image.width(), 7) for y in range(0, image.height(), 7))


def test_small_photo_is_one_jpeg_without_downscaling(qapp):
    parts = encoding.compress(photo(320, 180))
    assert len(parts) == 1 and parts[0].format == 'JPEG'
    assert parts[0].size <= 50000
    assert QImage.fromData(parts[0].data).size() == photo(320, 180).size()


def test_strict_limits_keep_layout_and_use_original_before_resizing(qapp):
    image = photo()
    parts = encoding.compress(image, max_parts=3, max_bytes=20000)
    assert len(parts) <= 3 and any(p.size > 20000 for p in parts)
    result = encoding.force_limit(image, parts, max_bytes=20000)
    assert [p.region for p in result] == [p.region for p in parts]
    assert all(0 < p.size <= 20000 for p in result)
    assert all(not QImage.fromData(p.data).isNull() for p in result)
    assert any(QImage.fromData(p.data).width() < p.region[2] for p in result)
    small = encoding.compress(photo(320, 180))
    assert encoding.force_limit(photo(320, 180), small) == small


def test_strict_cancel_returns_no_partial_result(qapp):
    image = photo(320, 180)
    parts = encoding.compress(image)
    cancel = Event(); cancel.set()
    with pytest.raises(encoding.CaptureCancelled):
        encoding.force_limit(image, parts, cancelled=cancel)


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='native Windows hit test')
def test_title_drag_accepts_actual_pyside_bytes_event(qapp, monkeypatch):
    from pptx_finder.ui.main_window import MainWindow
    class BareWindow(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._title_h = 52
            self.resize(900, 600)
        def closeEvent(self, event):
            event.accept()
    win = BareWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen)
    monkeypatch.setattr(QCursor, 'pos', lambda: win.mapToGlobal(QPoint(100, 25)))
    msg = wintypes.MSG(); msg.message = 0x84
    assert win.nativeEvent(b'windows_generic_MSG', ctypes.addressof(msg)) == (True, 2)
    win.close()


def test_workbench_config_and_force_copy_use_actual_file_bytes(qtbot, tmp_path):
    from pptx_finder.ui.screenshot_window import ScreenshotWindow
    copied = []
    win = ScreenshotWindow(clipboard_writer=copied.append, output_root=tmp_path)
    qtbot.addWidget(win)
    win.setAttribute(Qt.WA_DontShowOnScreen)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen)
    win.parts_limit.setValue(2);win.size_limit.setValue(20)
    assert win.process_image(photo(), show_result=False)
    qtbot.waitUntil(lambda:len(copied)==1 and not win._busy, timeout=10000)
    assert len(copied[0])==2 and any(p.stat().st_size>20000 for p in copied[0])
    assert '超过 20 KB' in win.status.text() and win._notice.force_btn.isVisible()
    regions = [p.region for p in win._parts]
    win.force_result()
    qtbot.waitUntil(lambda:len(copied)==2 and not win._busy, timeout=10000)
    assert len(copied[1])==2 and all(0 < p.stat().st_size<=20000 for p in copied[1])
    assert regions==[p.region for p in win._parts]
    assert '每张 ≤20 KB' in win.status.text()


def test_overlay_limits_are_emitted_before_processing_this_capture(qtbot):
    from pptx_finder.ui.screenshot_overlay import ScreenshotOverlay
    from PySide6.QtCore import QRect
    overlay = ScreenshotOverlay(photo(320, 180), QRect(0, 0, 320, 180), QRect(10, 10, 200, 100))
    qtbot.addWidget(overlay)
    overlay.setAttribute(Qt.WA_DontShowOnScreen)
    overlay.show()
    values=[]
    overlay.small_limits_selected.connect(lambda n, s:values.append((n,s)))
    overlay.small_selected.connect(lambda image:values.append('image'))
    overlay.parts_limit.setValue(4);overlay.size_limit.setValue(30)
    overlay.small_btn.click()
    assert values==[(4,30),'image']
