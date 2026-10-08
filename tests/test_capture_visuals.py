"""Recording range must remain visible without freezing or obscuring the source."""
import pytest
from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap

from pptx_finder.ui.screenshot_window import ScreenshotWindow


def test_recording_keeps_a_noninteractive_range_through_pause_and_capture(qtbot, tmp_path, monkeypatch):
    import pptx_finder.ui.screenshot_record_controller as recording
    import pptx_finder.screenshots.record_native as native
    win = ScreenshotWindow(output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    qtbot.addWidget(win)
    record = win._record
    record.panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    record.range_frame.setAttribute(Qt.WA_DontShowOnScreen, True)
    screen = QGuiApplication.primaryScreen()
    geometry = screen.geometry()
    local = QRect(50, 60, 200, 120)
    image = QImage(400, 300, QImage.Format_RGB32)
    image.fill(QColor('white'))
    monkeypatch.setattr(screen, 'grabWindow', lambda *a: QPixmap.fromImage(image))
    class Capture:
        def __init__(self, *a): pass
        def grab(self):
            assert record.range_frame.isVisible(), 'capturing must not hide the range'
            return image.copy(0, 0, 200, 120)
    class Engine(QObject):
        progress = Signal(str)
        finished = Signal(object, str)
        state = 'idle'
        def __init__(self, *a, parent=None): super().__init__(parent)
        def start(self): self.state = 'countdown'; self.progress.emit('3 秒后开始')
        def close(self): self.state = 'done'
    monkeypatch.setattr(native, 'NativeRegionCapture', Capture)
    monkeypatch.setattr(recording, 'RecordCapture', Engine)
    monkeypatch.setattr(recording, 'exclude_from_capture', lambda *a: False)
    record.start(local, geometry)
    outline = record.range_frame
    outline.setAttribute(Qt.WA_DontShowOnScreen, True)
    assert outline.isVisible()
    assert outline.windowFlags() & Qt.WindowTransparentForInput
    assert outline.windowFlags() & Qt.WindowDoesNotAcceptFocus
    assert outline.testAttribute(Qt.WA_ShowWithoutActivating)
    # The actual capture rectangle is a hole in the window mask. Even when
    # display-affinity is unavailable, no part of the frame enters the GIF.
    inner = local.translated(geometry.topLeft() - outline.geometry().topLeft())
    from PySide6.QtGui import QRegion
    assert outline.mask().intersected(QRegion(inner)).isEmpty()
    for state in ('recording', 'paused', 'saving'):
        record.engine.state = state
        record._progress(state)
        assert outline.isVisible()
        assert record._capture().size().width() == 200
        assert outline.isVisible()
    record._finished(None, '已取消')
    assert not outline.isVisible()
    record.start(local, geometry)
    assert outline.isVisible()
    win.close()


@pytest.mark.parametrize('excluded', [True, False])
def test_full_monitor_range_is_visible_even_without_capture_exclusion(qtbot, excluded):
    from PySide6.QtGui import QRegion
    from pptx_finder.ui.record_range import RecordRange
    outline = RecordRange()
    outline.setAttribute(Qt.WA_DontShowOnScreen, True)
    qtbot.addWidget(outline)
    bounds = QRect(-800, 0, 800, 450)
    outline.display(bounds, (1200,675))
    assert outline.mask().translated(outline.pos()).intersected(QRegion(bounds)).isEmpty()
    outline.ensure_on_screen(bounds, capture_excluded=excluded)
    assert not outline.mask().translated(outline.pos()).intersected(QRegion(bounds)).isEmpty()
    assert bool(outline.capture_warning) != excluded
    outline.hide()
    assert not outline.isVisible()


def test_recording_start_failure_cleans_up_the_visible_range(qtbot, tmp_path, monkeypatch):
    import pptx_finder.ui.screenshot_record_controller as recording
    win = ScreenshotWindow(output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    qtbot.addWidget(win)
    record = win._record
    record.panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    record.range_frame.setAttribute(Qt.WA_DontShowOnScreen, True)
    record.range_frame.show()
    def fail(*a): raise RuntimeError('controlled failure')
    monkeypatch.setattr(record, '_start', fail)
    record.start(QRect(), QRect())
    assert not record.range_frame.isVisible() and not record.panel.isVisible()
    assert 'controlled failure' in win.status.text()
    win.close()


def test_fullscreen_capture_warning_is_visible_before_recording_and_in_saved_result(qtbot, tmp_path, monkeypatch):
    import pptx_finder.ui.screenshot_record_controller as recording
    import pptx_finder.screenshots.record_native as native
    from PIL import Image
    win = ScreenshotWindow(output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen, True); qtbot.addWidget(win)
    record = win._record
    for widget in [record.panel, record.range_frame, record.result]:
        widget.setAttribute(Qt.WA_DontShowOnScreen, True)
    class Engine(QObject):
        progress = Signal(str); finished = Signal(object, str)
        state = 'countdown'
        def __init__(self, *a, parent=None): super().__init__(parent)
        def start(self): self.progress.emit('3 秒后开始')
        def close(self): pass
    class Capture:
        def __init__(self,*a): pass
    screen = QGuiApplication.primaryScreen(); geometry = screen.geometry()
    source = QPixmap(800,450); source.fill(QColor('white'))
    monkeypatch.setattr(screen, 'grabWindow', lambda *a: source)
    monkeypatch.setattr(native, 'NativeRegionCapture', Capture)
    monkeypatch.setattr(recording, 'RecordCapture', Engine)
    monkeypatch.setattr(recording, 'exclude_from_capture', lambda *a: False)
    record.start(QRect(0,0,geometry.width(),geometry.height()),geometry)
    assert record.range_frame.isVisible()
    assert record.engine.state == 'countdown' and '保留细边框' in record.panel.details.text()
    path = tmp_path/'fallback.gif'; Image.new('RGB',(40,40),'white').save(path)
    record._finished(path, '')
    assert '保留细边框' in record.result.status.text()
    assert not record.range_frame.isVisible()
    win.close()
