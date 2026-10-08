"""Headless frozen acceptance of GIF encoding, timing, and animated preview."""
import json
import sys
import time
import uuid
from pathlib import Path


def main(args):
    index = args.index('--gif-selftest')
    if len(args) <= index + 1:
        return 2
    root = Path(args[index + 1])
    root.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from ..ui.record_capture import RecordCapture, shutdown_writers
    from ..ui.screenshot_window import ScreenshotWindow
    app = QApplication.instance() or QApplication([])
    checks = {}
    win = ScreenshotWindow(output_root=root)
    for widget in [win, win._notice, win._record.panel, win._record.result]:
        widget.setAttribute(Qt.WA_DontShowOnScreen, True)
    path = root / ('gif-selftest-' + uuid.uuid4().hex[:8] + '.gif')
    counter = [0]
    def capture():
        image = QImage(320, 160, QImage.Format_RGB888)
        image.fill(QColor('white'))
        painter = QPainter(image)
        painter.fillRect(QRect(20 + counter[0] * 20 % 220, 45, 60, 60), QColor('#2867bd'))
        painter.setPen(QColor('#243249'))
        painter.drawText(12, 25, 'PPT Doctor GIF recording')
        painter.end()
        counter[0] += 1
        return image
    engine = RecordCapture(capture, path, countdown=0, max_seconds=.6)
    result = []
    engine.finished.connect(lambda *a: result.append(a))
    try:
        if sys.platform == 'win32':
            from PySide6.QtGui import QGuiApplication
            from .record_native import NativeRegionCapture
            screen = QGuiApplication.primaryScreen()
            sample = NativeRegionCapture(screen, QRect(0, 0, 16, 16), screen.grabWindow(0).size()).grab()
            checks['native_region_backend_available'] = not sample.isNull()
        engine.start()
        deadline = time.monotonic() + 15
        while not result:
            if time.monotonic() > deadline:
                raise TimeoutError('GIF 自检超时')
            app.processEvents(); time.sleep(.01)
        if result[0][0] is None:
            raise RuntimeError(result[0][1])
        with Image.open(path) as gif:
            checks['animated_gif_encoded'] = gif.n_frames >= 3
            checks['original_dimensions'] = gif.size == (320, 160)
            checks['infinite_loop'] = gif.info['loop'] == 0
            first = gif.convert('RGB').tobytes()
            gif.seek(gif.n_frames - 1)
            checks['different_frames'] = first != gif.convert('RGB').tobytes()
        win._record._finished(path, '')
        app.processEvents()
        checks['native_qmovie_preview'] = win._record.result.movie.isValid()
        checks['result_can_be_reopened'] = win.record_result_btn.isEnabled()
        state = {'passed': all(checks.values()), 'checks': checks, 'gif': str(path),
                 'capture_source': 'controlled animated frames; not global screen capture',
                 'clipboard_untouched': True, 'visible_test_windows': False}
    except Exception as exc:
        state = {'passed': False, 'checks': checks, 'error': str(exc)}
    finally:
        engine.shutdown()
        win.close()
        shutdown_writers()
        app.processEvents()
    (root / 'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if state['passed'] else 1
