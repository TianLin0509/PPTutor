from pathlib import Path
from threading import Event

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from pptx_finder.screenshots.gif_encoding import GifStream, pil_image
from pptx_finder.ui.record_capture import GifWriter, RecordCapture
from pptx_finder.ui.screenshot_overlay import ScreenshotOverlay
from pptx_finder.ui.screenshot_window import ScreenshotWindow


def frame(colour='red', width=96, height=64):
    image = QImage(width, height, QImage.Format_RGB888)
    image.fill(QColor(colour))
    return image


def durations(path):
    with Image.open(path) as gif:
        values = []
        for i in range(gif.n_frames):
            gif.seek(i)
            values.append(gif.info['duration'])
        return values


def hidden(widget, qtbot):
    widget.setAttribute(Qt.WA_DontShowOnScreen, True)
    qtbot.addWidget(widget)
    return widget


def window(qtbot, tmp_path):
    win = hidden(ScreenshotWindow(output_root=tmp_path), qtbot)
    for item in [win._notice, win._record.panel, win._record.result]:
        item.setAttribute(Qt.WA_DontShowOnScreen, True)
    return win


def test_encoder_preserves_dimensions_colours_duration_and_loop(tmp_path, qapp):
    path = tmp_path / 'screen.gif'
    stream = GifStream(path)
    stream.append(pil_image(frame('red', 97)), 130)
    stream.append(pil_image(frame('blue', 97)), 270)
    assert not path.exists()
    assert stream.finish() == path
    with Image.open(path) as gif:
        assert gif.size == (97, 64) and gif.n_frames == 2
        assert gif.info['loop'] == 0
        assert gif.convert('RGB').getpixel((20, 20)) == (255, 0, 0)
        gif.seek(1)
        assert gif.convert('RGB').getpixel((20, 20)) == (0, 0, 255)
    assert durations(path) == [130, 270]
    assert not list(tmp_path.glob('*.partial'))


def test_encoder_discards_and_never_replaces_an_existing_file(tmp_path):
    path = tmp_path / 'screen.gif'
    stream = GifStream(path)
    stream.append(Image.new('RGB', (40, 40), 'white'), 100)
    path.write_bytes(b'keep my existing file')
    with pytest.raises(FileExistsError):
        stream.finish()
    stream.discard()
    assert path.read_bytes() == b'keep my existing file'
    assert not list(tmp_path.glob('*.partial'))


def test_encoder_rejects_changed_display_size(tmp_path):
    stream = GifStream(tmp_path / 'screen.gif')
    stream.append(Image.new('RGB', (40, 40)), 100)
    with pytest.raises(ValueError, match='分辨率'):
        stream.append(Image.new('RGB', (41, 40)), 100)
    stream.discard()


def test_toolbar_recording_routes_only_the_selected_region(qtbot):
    overlay = hidden(ScreenshotOverlay(frame(width=800, height=450), QRect(-800, 0, 800, 450)), qtbot)
    records, originals = [], []
    overlay.record_selected.connect(lambda r, g: records.append((r, g)))
    overlay.selected.connect(originals.append)
    overlay.show()
    QTest.mousePress(overlay, Qt.LeftButton, pos=QPoint(30, 40))
    QTest.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(600, 300))
    assert overlay.record_btn.isVisible()
    QTest.mouseClick(overlay.record_btn, Qt.LeftButton)
    assert records == [(QRect(30, 40, 570, 260), QRect(-800, 0, 800, 450))]
    assert originals == []


def test_recording_is_disabled_until_annotations_are_removed(qtbot):
    from pptx_finder.screenshots.annotations import Mark
    from PySide6.QtCore import QPointF
    overlay = hidden(ScreenshotOverlay(frame(), QRect(0, 0, 96, 64), QRect(0, 0, 60, 50)), qtbot)
    records = []
    overlay.record_selected.connect(lambda *a: records.append(a))
    overlay.marks.append(Mark('rect', QPointF(5, 5), QPointF(25, 25)))
    overlay._update_tools()
    assert not overlay.record_btn.isEnabled()
    overlay._confirm('record')
    assert records == []
    overlay.undo()
    assert overlay.record_btn.isEnabled()


def test_pause_does_not_add_paused_time_to_the_gif(qtbot, tmp_path):
    now = [0.0]
    colour = ['red']
    engine = RecordCapture(lambda: frame(colour[0]), tmp_path / 'screen.gif', countdown=0, clock=lambda: now[0])
    done = []
    engine.finished.connect(lambda *result: done.append(result))
    engine.start()
    engine.timer.stop()
    now[0] = .2
    engine.pause()
    assert engine.state == 'paused'
    now[0] = 10.2
    colour[0] = 'blue'
    engine.pause()
    engine.timer.stop()
    assert engine.state == 'recording' and engine.elapsed() == 200
    now[0] = 10.5
    engine.stop()
    qtbot.waitUntil(lambda: bool(done), timeout=5000)
    assert done[0] == (tmp_path / 'screen.gif', '')
    assert durations(done[0][0]) == [200, 300]


def test_countdown_cancel_creates_no_files_and_never_captures(qtbot, tmp_path):
    calls, done = [], []
    engine = RecordCapture(lambda: calls.append(1), tmp_path / 'screen.gif')
    engine.finished.connect(lambda *result: done.append(result))
    engine.start()
    assert engine.state == 'countdown'
    engine.stop()
    assert calls == [] and done[0][0] is None
    assert list(tmp_path.iterdir()) == []


def test_time_limit_stops_and_saves_recorded_content(qtbot, tmp_path):
    now = [0.0]
    engine = RecordCapture(lambda: frame(), tmp_path / 'screen.gif', countdown=0, max_seconds=1, clock=lambda: now[0])
    done = []
    engine.finished.connect(lambda *result: done.append(result))
    engine.start(); engine.timer.stop()
    now[0] = 1.0
    engine._tick()
    qtbot.waitUntil(lambda: bool(done), timeout=5000)
    assert done[0][0].is_file() and '上限' in done[0][1]
    assert durations(done[0][0]) == [1000]


def test_capture_failure_preserves_prior_frames_and_reports_reason(qtbot, tmp_path):
    frames = [frame(), RuntimeError('display disconnected')]
    def capture():
        value = frames.pop(0)
        if isinstance(value, Exception): raise value
        return value
    engine = RecordCapture(capture, tmp_path / 'screen.gif', countdown=0)
    done = []
    engine.finished.connect(lambda *result: done.append(result))
    engine.start(); engine.timer.stop(); engine._tick()
    qtbot.waitUntil(lambda: bool(done), timeout=5000)
    assert done[0][0].is_file() and 'display disconnected' in done[0][1]


def test_encoder_startup_failure_is_reported_without_hanging(qtbot, tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('keep')
    engine = RecordCapture(lambda: frame(), blocked / 'screen.gif', countdown=0)
    done = []
    engine.finished.connect(lambda *result: done.append(result))
    engine.start()
    qtbot.waitUntil(lambda: bool(done), timeout=5000)
    assert engine.state == 'done' and done[0][0] is None and done[0][1]
    assert blocked.read_text() == 'keep'


def test_writer_queue_is_bounded_and_discard_releases_frames(qtbot, tmp_path):
    writer = GifWriter(tmp_path / 'screen.gif')
    assert [writer.submit(frame(), i * 100) for i in range(4)] == [True, True, True, False]
    writer.end(400, discard=True)
    writer.start()
    assert writer.wait(5000)
    assert writer.frames.empty() and not list(tmp_path.iterdir())


def test_stop_discards_before_publishing_and_cleans_partial(qtbot, tmp_path):
    entered, release = Event(), Event()
    class SlowStream(GifStream):
        def append(self, image, duration):
            entered.set()
            assert release.wait(5)
            super().append(image, duration)
    writer = GifWriter(tmp_path / 'screen.gif', stream_factory=SlowStream)
    writer.submit(frame(), 0)
    writer.end(200)
    writer.start()
    try:
        assert entered.wait(3)
        writer.end(200, discard=True)
    finally:
        release.set()
        assert writer.wait(5000)
    assert not list(tmp_path.iterdir())


def test_identical_frames_merge_without_shortening_display_time(qtbot, tmp_path):
    writer = GifWriter(tmp_path / 'screen.gif')
    for ms in [0, 100, 200]: assert writer.submit(frame(), ms)
    writer.end(350)
    writer.start()
    assert writer.wait(5000)
    assert durations(tmp_path / 'screen.gif') == [350]


def test_saved_result_is_animated_and_can_be_reopened(qtbot, tmp_path):
    win = window(qtbot, tmp_path)
    path = tmp_path / 'screen.gif'
    stream = GifStream(path)
    stream.append(Image.new('RGB', (40, 40), 'red'), 100)
    stream.append(Image.new('RGB', (40, 40), 'blue'), 100)
    stream.finish()
    win._set_busy(True)
    win._record._finished(path, '')
    assert not win._busy and win.record_result_btn.isEnabled()
    assert win._record.result.isVisible() and win._record.result.movie.isValid()
    win._record.result.hide()
    QTest.mouseClick(win.record_result_btn, Qt.LeftButton)
    assert win._record.result.isVisible()
    win.close()


def test_result_save_as_preserves_animation(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    win = window(qtbot, tmp_path)
    path = tmp_path / 'screen.gif'
    stream = GifStream(path)
    stream.append(Image.new('RGB', (40, 40), 'red'), 100)
    stream.append(Image.new('RGB', (40, 40), 'blue'), 100)
    stream.finish()
    win._record.result.display(path)
    destination = tmp_path / 'saved.gif'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(destination), 'GIF'))
    win._record.result.save_as()
    qtbot.waitUntil(lambda: win._record.result.save_btn.isEnabled(), timeout=5000)
    assert destination.read_bytes() == path.read_bytes()
    win.close()


def test_close_during_pending_start_never_records(qtbot, tmp_path, monkeypatch):
    win = window(qtbot, tmp_path)
    win._record_selected(QRect(0, 0, 40, 40), QRect(0, 0, 800, 450))
    win.close()
    qtbot.wait(200)
    assert win._record.engine is None and not list(tmp_path.glob('*.gif'))


def test_frozen_packaging_includes_gif_encoder_and_declares_runtime_dependency():
    root = Path(__file__).resolve().parents[1]
    spec = (root / 'pptx-finder.spec').read_text('utf-8')
    excluded = spec.split('excludes=[', 1)[1].split('],', 1)[0]
    assert "'PIL'" not in excluded
    assert '"pillow>=' in (root / 'pyproject.toml').read_text('utf-8')


def test_recording_failure_releases_ui_for_another_capture(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QGuiApplication
    win = window(qtbot, tmp_path)
    win._set_busy(True)
    monkeypatch.setattr(QGuiApplication, 'screens', lambda: [])
    win._record.start(QRect(0, 0, 40, 40), QRect(0, 0, 800, 450))
    assert not win._busy and '显示器' in win.status.text() and win.capture_btn.isEnabled()
    win.close()


def test_encoder_constructor_exception_is_reported(qtbot, tmp_path):
    def failed(path):
        raise RuntimeError('no write permission')
    writer = GifWriter(tmp_path / 'screen.gif', stream_factory=failed)
    results = []
    writer.completed.connect(lambda *a: results.append(a))
    writer.start()
    assert writer.wait(5000)
    qtbot.waitUntil(lambda: bool(results), timeout=1000)
    assert results == [(None, 'no write permission')]


def test_gif_cannot_be_saved_with_a_disguised_video_extension(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    win = window(qtbot, tmp_path)
    path = tmp_path / 'screen.gif'
    stream = GifStream(path)
    stream.append(Image.new('RGB', (40, 40), 'red'), 100)
    stream.finish()
    win._record.result.display(path)
    destination = tmp_path / 'screen.mp4'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(destination), 'GIF'))
    win._record.result.save_as()
    assert not destination.exists() and 'GIF' in win._record.result.status.text()
    win.close()


def test_queue_overload_is_visible_in_recording_progress(qtbot, tmp_path):
    now = [0.0]
    class BusyWriter(GifWriter):
        def start(self, *a): pass
    engine = RecordCapture(lambda: frame(), tmp_path / 'screen.gif', countdown=0,
                           writer_factory=BusyWriter, clock=lambda: now[0])
    messages = []
    engine.progress.connect(messages.append)
    engine.start(); engine.timer.stop()
    for i in range(1, 5):
        now[0] = i / 10
        engine._tick()
    assert engine.accepted == 3 and engine.skipped == 2
    assert '处理繁忙' in messages[-1]
    engine.close()
    # Fake writer never ran: remove only this test-owned instance from tracking.
    from pptx_finder.ui.record_capture import _writers
    _writers.discard(engine.writer)


def test_frozen_gif_selftest_uses_the_runtime_encoder_and_preview(qtbot, tmp_path):
    import json
    from pptx_finder.screenshots.gif_selftest import main
    assert main(['PPT-Doctor.exe', '--gif-selftest', str(tmp_path)]) == 0
    result = json.loads((tmp_path / 'result.json').read_text('utf-8'))
    assert result['passed'] and result['clipboard_untouched'] and len(result['checks']) >= 6


@pytest.mark.parametrize('close_during_capture', [False, True])
def test_fallback_capture_hides_overlapping_controls_without_reviving_a_closed_window(qtbot, tmp_path, monkeypatch, close_during_capture):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QPixmap
    win = window(qtbot, tmp_path)
    record = win._record
    record.geometry = QRect(0, 0, 800, 450)
    record.local = QRect(0, 0, 800, 450)
    record.native_capture = None
    record._frame_size = QSize(800, 450)
    record._excluded = False
    record.panel.setGeometry(0, 0, 350, 120)
    record.panel.show()
    class Engine:
        state = 'recording'
        def close(self): self.state = 'saving'
    record.engine = Engine()
    class Screen:
        def geometry(self): return QRect(0, 0, 800, 450)
        def grabWindow(self, *a):
            assert not record.panel.isVisible()
            return QPixmap.fromImage(frame(width=800, height=450))
    record.screen = Screen()
    with monkeypatch.context() as scoped:
        if close_during_capture:
            scoped.setattr(QGuiApplication, 'processEvents', staticmethod(lambda *a: win.close()))
        image = record._capture()
    assert not image.isNull()
    assert record.panel.isVisible() != close_during_capture
    win.close()


def test_native_region_uses_physical_coordinates_on_negative_origin_display(monkeypatch, qapp):
    from PySide6.QtCore import QSize
    import win32api
    import pptx_finder.screenshots.record_native as native
    class Probe:
        def setScreen(self, screen): pass
        def setGeometry(self, geometry): pass
        def create(self): pass
        def winId(self): return 7
        def destroy(self): pass
    class Screen:
        def geometry(self): return QRect(-1280, 0, 1280, 720)
    monkeypatch.setattr(native, 'QWindow', Probe)
    monkeypatch.setattr(win32api, 'MonitorFromWindow', lambda *a: 17)
    monkeypatch.setattr(win32api, 'GetMonitorInfo', lambda *a: {'Monitor': (-1920, 0, 0, 1080)})
    capture = native.NativeRegionCapture(Screen(), QRect(10, 20, 100, 60), QSize(1920, 1080))
    assert capture.point == (-1905, 30) and capture.size == (150, 90)


def test_native_capture_failure_releases_every_owned_gdi_resource(monkeypatch, qapp):
    import win32api, win32gui, win32ui
    from pptx_finder.screenshots.record_native import NativeRegionCapture
    released = []
    class Memory:
        def SelectObject(self, item):
            if item == 'previous': released.append('restore bitmap')
            return 'previous'
        def BitBlt(self, *a): raise RuntimeError('native capture failed')
        def DeleteDC(self): released.append('memory dc')
    class Source:
        def CreateCompatibleDC(self): return Memory()
        def DeleteDC(self): released.append('screen dc')
    class Bitmap:
        def CreateCompatibleBitmap(self, *a): pass
        def GetHandle(self): return 99
    monkeypatch.setattr(win32api, 'GetMonitorInfo', lambda *a: {'Monitor': (0, 0, 800, 450)})
    monkeypatch.setattr(win32gui, 'GetDC', lambda *a: 11)
    monkeypatch.setattr(win32gui, 'ReleaseDC', lambda *a: released.append('release dc'))
    monkeypatch.setattr(win32gui, 'DeleteObject', lambda *a: released.append('bitmap'))
    monkeypatch.setattr(win32ui, 'CreateDCFromHandle', lambda *a: Source())
    monkeypatch.setattr(win32ui, 'CreateBitmap', Bitmap)
    capture = object.__new__(NativeRegionCapture)
    capture.monitor = 1; capture.bounds = (0, 0, 800, 450); capture.point = (10, 10); capture.size = (40, 40)
    with pytest.raises(RuntimeError, match='native capture failed'): capture.grab()
    assert released == ['restore bitmap', 'bitmap', 'memory dc', 'screen dc', 'release dc']
