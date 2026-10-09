"""A nonmodal screenshot utility; encoding and file delivery run off the UI thread."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..config import cache_dir, data_dir
from .. import __version__
from ..screenshots.encoding import CaptureCancelled, ImagePart, compress, force_limit
from ..screenshots.delivery import copy_files, copy_image, export_files, store_parts
from .bg_task import BackgroundTask
from .screenshot_overlay import ScreenshotOverlay
from .screenshot_notice import ScreenshotNotice
from .capture_style import STYLE, setup_button

_tasks: set[BackgroundTask] = set()


class ScreenshotWindow(QDialog):
    progress = Signal(str)

    def __init__(self, parent=None, *, clipboard_writer=copy_files, image_writer=copy_image, output_root: Path | None = None, preferences=None, text_writer=None, recognizer=None, formula_recognizer=None, formula_renderer=None, formula_writer=None):
        super().__init__(parent)
        self.setObjectName('screenshotWindow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(STYLE)
        self.setWindowTitle(f'截图 · PPT Doctor {__version__}')
        self.setModal(False)
        self._clipboard_writer = clipboard_writer
        self._image_writer = image_writer
        self._notice = ScreenshotNotice()
        self._notice.stop_requested.connect(self._stop_scroll)
        self._notice.small_requested.connect(lambda: self.process_image(self._preview_image))
        self._notice.force_requested.connect(self.force_result)
        self._scroll = None
        self._silent = False
        self._output_root = output_root or cache_dir() / 'screenshots'
        self._paths: list[Path] = []
        self._parts: list[ImagePart] = []
        self._cancel = Event()
        self._closed = False
        self._serial = 0
        self._capture_pending = False
        self._overlays: list[ScreenshotOverlay] = []
        self._owner_was_visible = False
        self._preview_image = QImage()
        self._original_scroll_message = ''
        self._original_complete = True
        self._busy = False
        from ..screenshots.preferences import CapturePreferences
        from .screenshot_text_controller import ScreenshotTextController
        self._preferences = preferences or CapturePreferences((output_root or data_dir()) / 'screenshot-preferences.json')
        self._repeat = False
        self._text = ScreenshotTextController(self,writer=text_writer,recognizer=recognizer)
        from .screenshot_formula_controller import ScreenshotFormulaController
        self._formula = ScreenshotFormulaController(self,recognizer=formula_recognizer,renderer=formula_renderer,writer=formula_writer)
        from .screenshot_record_controller import ScreenshotRecordController
        self._record = ScreenshotRecordController(self,output_root=output_root)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel('截图工作台')
        title.setStyleSheet('font-size: 22px; font-weight: 600; color: #1d2939;')
        heading.addWidget(title, 1)
        self.capture_btn = setup_button(QPushButton('框选截图'), 'capture', role='primary')
        self.capture_btn.clicked.connect(lambda:self.begin_capture())
        heading.addWidget(self.capture_btn)
        self.paste_btn = setup_button(QPushButton('处理剪贴板截图'), 'small', role='subtle')
        self.paste_btn.clicked.connect(self.paste_image)
        heading.addWidget(self.paste_btn)
        root.addLayout(heading)
        tools = QHBoxLayout()
        self.repeat_btn = QPushButton('重复上次区域')
        self.repeat_btn.clicked.connect(lambda:self.begin_capture(repeat=True))
        self.shortcuts_btn = QPushButton('截图快捷键…')
        self.shortcuts_btn.clicked.connect(self._configure_shortcuts)
        self.text_result_btn = QPushButton('查看文字结果')
        self.text_result_btn.clicked.connect(self._text.show)
        self.formula_result_btn = QPushButton('查看公式结果')
        self.formula_result_btn.clicked.connect(self._formula.show)
        self.record_result_btn = QPushButton('查看录制结果')
        self.record_result_btn.clicked.connect(self._record.show)
        for button in [self.repeat_btn,self.shortcuts_btn,self.text_result_btn,self.formula_result_btn,self.record_result_btn]:tools.addWidget(button)
        tools.addStretch(1)
        root.addLayout(tools)
        self.status = QLabel('框选后选择：✓ 普通截图、滚动截图、录制 GIF、小图模式，或识别公式。')
        self.status.setWordWrap(True)
        self.status.setStyleSheet('background: #f6f8fb; color: #475467; border: 1px solid #e9edf3; border-radius: 10px; padding: 12px 14px;')
        root.addWidget(self.status)
        self.preview = QLabel('截图预览')
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(160)
        self.preview.setMaximumHeight(330)
        self.preview.setStyleSheet('background: #f6f8fb; color: #667085; border: 1px solid #e4e7ec; border-radius: 12px; padding: 10px;')
        root.addWidget(self.preview, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(170)
        scroll.setStyleSheet('QScrollArea { background: #ffffff; border: none; }')
        self.parts_scroll = scroll
        self.cards = QWidget()
        self.cards.setObjectName('screenshotCards')
        self.cards.setAttribute(Qt.WA_StyledBackground, True)
        self.cards.setStyleSheet('#screenshotCards { background: #ffffff; }')
        self.card_layout = QHBoxLayout(self.cards)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self.cards)
        root.addWidget(scroll)
        settings = QHBoxLayout()
        settings.addWidget(QLabel('小图配置'))
        settings.addWidget(QLabel('最多'))
        self.parts_limit = QSpinBox()
        self.parts_limit.setRange(1, 16)
        self.parts_limit.setSuffix(' 张')
        self.parts_limit.setValue(8)
        settings.addWidget(self.parts_limit)
        settings.addWidget(QLabel('每张目标'))
        self.size_limit = QSpinBox()
        self.size_limit.setRange(2, 1000)
        self.size_limit.setSuffix(' KB')
        self.size_limit.setValue(50)
        settings.addWidget(self.size_limit)
        settings.addWidget(QLabel('先保留分辨率；强制达标时才缩小图片'), 1)
        root.addLayout(settings)
        actions = QHBoxLayout()
        self.copy_btn = QPushButton('复制全部图片')
        self.copy_btn.setObjectName('gotoBtn')
        self.copy_btn.setStyleSheet('QPushButton { background: #2867bd; color: #ffffff; border: 1px solid #2867bd; } QPushButton:disabled { background: #e7ebf1; color: #87909e; border-color: #e1e5eb; }')
        self.copy_btn.clicked.connect(lambda: self.copy_result())
        self.save_btn = setup_button(QPushButton('保存图片'), 'save', role='subtle')
        self.save_btn.clicked.connect(self.save_result)
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.save_btn)
        self.small_btn = QPushButton('将原图转为小图')
        self.small_btn.clicked.connect(lambda: self.process_image(self._preview_image))
        actions.addWidget(self.small_btn)
        self.force_btn = QPushButton('强制达标（可降分辨率）')
        self.force_btn.clicked.connect(self.force_result)
        actions.addWidget(self.force_btn)
        actions.addStretch(1)
        root.addLayout(actions)
        hint = QLabel('复制的是 PNG / JPEG 图片文件；多图粘贴取决于目标网页支持。不支持时可保存后上传。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #626b78; font-size: 12px;')
        root.addWidget(hint)
        self.progress.connect(self._progress)
        self.resize(1000, 720)
        screen = self.screen()
        if screen:
            area = screen.availableGeometry()
            self.resize(min(1000, area.width() - 30), min(720, area.height() - 30))
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.capture_btn.setEnabled(not busy)
        self.repeat_btn.setEnabled(not busy)
        self.text_result_btn.setEnabled(not busy and bool(self._text.dialog.editor.toPlainText()))
        self.formula_result_btn.setEnabled(not busy and bool(self._formula.dialog.editor.toPlainText()))
        self.record_result_btn.setEnabled(not busy and bool(self._record.result.path))
        self.paste_btn.setEnabled(not busy)
        has_result = bool(self._paths) or not self._preview_image.isNull()
        self.copy_btn.setText('复制全部图片' if self._paths else '复制原图')
        self.copy_btn.setEnabled(not busy and has_result)
        self.save_btn.setEnabled(not busy and has_result)
        self.small_btn.setEnabled(not busy and not self._preview_image.isNull())
        self.force_btn.setEnabled(not busy and bool(self._parts))
        self.parts_limit.setEnabled(not busy)
        self.size_limit.setEnabled(not busy)
        self.parts_scroll.setVisible(bool(self._parts))
        for button in self.cards.findChildren(QPushButton):
            button.setEnabled(not busy)

    def _run(self, fn, callback, label):
        task = BackgroundTask(fn, label=label)
        _tasks.add(task)
        task.done.connect(callback)
        task.finished.connect(lambda: _tasks.discard(task))
        task.start(QThread.LowPriority)

    def _progress(self,text):
        if self._closed:
            return
        self.status.setText(text)
        if self._silent:
            self._notice.label.setText(text)

    def process_image(self, image: QImage, *, show_result=True, strict=False):
        if image.isNull() or self._busy:
            return False
        self._closed = False
        self._silent = not show_result
        self._original_scroll_message = ''
        self._original_complete = True
        self._serial += 1
        serial = self._serial
        self._cancel = Event()
        cancel = self._cancel
        previous_parts = list(self._parts)
        max_parts, max_bytes = self.parts_limit.value(), self.size_limit.value() * 1000
        self._result_limit = max_bytes
        self._paths = []
        self._parts = []
        self._clear_cards()
        self._preview_image = image.copy()
        self._update_preview()
        self._set_busy(True)
        self.status.setText('正在压缩截图…')
        if show_result:
            self.show()
        else:
            self._notice.progress('小图模式 · 正在压缩截图…')
        progress = self.progress
        root = self._output_root

        def work():
            try:
                parts = (previous_parts if strict and previous_parts and len(previous_parts) <= max_parts
                         else compress(image, max_parts=max_parts, max_bytes=max_bytes,
                                       cancelled=cancel, progress=progress.emit))
                if strict:
                    parts = force_limit(image, parts, max_bytes=max_bytes,
                                        cancelled=cancel, progress=progress.emit)
                if cancel.is_set():
                    raise CaptureCancelled()
                paths = store_parts(parts, root, max_bytes=max_bytes if strict else None)
                return parts, paths, ''
            except CaptureCancelled:
                return [], [], '已取消'
            except Exception as exc:
                return [], [], str(exc)

        def done(result):
            if serial != self._serial or self._closed:
                return
            self._set_busy(False)
            if result is None:
                self.status.setText('截图处理失败，请重试')
                return
            self._parts, self._paths, error = result
            if error:
                self.status.setText(error)
                self._notice.hide()
                self.show()
                return
            self._build_cards()
            self._set_busy(False)
            self.copy_result()

        self._run(work, done, 'screenshot-compress')
        return True

    def force_result(self):
        return self.process_image(self._preview_image, show_result=not self._silent, strict=True)

    def _set_small_limits(self, count, kb):
        self.parts_limit.setValue(count)
        self.size_limit.setValue(kb)

    def _clear_cards(self):
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _build_cards(self):
        self._clear_cards()
        for i, part in enumerate(self._parts):
            card = QFrame()
            card.setObjectName('screenshotCard')
            card.setStyleSheet('#screenshotCard { background: #ffffff; border: 1px solid #e1e5eb; border-radius: 6px; }')
            card.setMinimumWidth(230)
            lay = QVBoxLayout(card)
            label = QLabel(f'第 {i + 1} 张 · {part.size / 1000:.2f} KB · {part.format}')
            lay.addWidget(label)
            preview = QLabel()
            preview.setAlignment(Qt.AlignCenter)
            pixmap = QPixmap.fromImage(QImage.fromData(part.data))
            preview.setPixmap(pixmap.scaled(280, 75, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            lay.addWidget(preview)
            button = QPushButton('单独复制')
            button.clicked.connect(lambda checked=False, index=i: self.copy_result(index))
            lay.addWidget(button)
            self.card_layout.addWidget(card)
        self.card_layout.addStretch(1)

    def copy_result(self, index: int | None = None):
        if self._busy:
            return
        if not self._paths:
            if not self._preview_image.isNull():
                self._copy_original(self._preview_image,scroll_message=self._original_scroll_message,
                                    complete=self._original_complete)
            return
        paths = list(self._paths if index is None else [self._paths[index]])
        self._closed = False
        self._serial += 1
        self._cancel = Event()
        serial = self._serial
        cancel = self._cancel
        writer = self._clipboard_writer
        if writer is copy_files:
            hwnd = int(self.winId())
            writer = lambda paths: copy_files(paths, hwnd=hwnd, cancelled=cancel, max_bytes=None)
        self._set_busy(True)
        self.status.setText('正在复制图片文件…')

        def copy():
            try:
                if cancel.is_set():
                    return '已取消'
                writer(paths)
                return ''
            except Exception as exc:
                return str(exc)

        def done(error):
            if self._closed or serial != self._serial:
                return
            self._set_busy(False)
            if error is None or error:
                self.status.setText(f'图片已处理，复制未成功：{error or "请重试"}。可点击复制或保存图片。')
                self._notice.hide()
                self.show()
            else:
                sizes = ' / '.join(f'{p.stat().st_size / 1000:.2f}' for p in paths)
                limit = getattr(self, '_result_limit', 50000)
                oversized = sum(p.stat().st_size > limit for p in paths)
                detail = (f'{oversized} 张超过 {limit / 1000:g} KB，先尝试上传；不通过再点击强制达标。'
                          if oversized else f'每张 ≤{limit / 1000:g} KB。')
                self.status.setText(f'已复制 {len(paths)} 张图片文件 · {sizes} KB · {detail}')
                if self._silent:
                    self._notice.complete(f'已复制 {len(paths)} 张 · Ctrl+V 粘贴 · {detail}', allow_force=bool(oversized))

        self._run(copy, done, 'screenshot-copy')

    def paste_image(self):
        image = QGuiApplication.clipboard().image()
        if image.isNull():
            self.status.setText('剪贴板中没有截图。可先用 Win+Shift+S，再点“处理剪贴板截图”。')
            return
        self.process_image(image)

    def save_result(self):
        if self._busy:
            return
        if not self._paths:
            if self._preview_image.isNull():
                return
            filename,_ = QFileDialog.getSaveFileName(self,'保存原图','截图.png','PNG 图片 (*.png)')
            if not filename:
                return
            self._closed = False
            self._serial += 1
            self._cancel = Event()
            image,serial = self._preview_image.copy(),self._serial
            self._set_busy(True)
            self.status.setText('正在保存原图…')
            def save_original():
                try:
                    from ..screenshots.encoding import encode
                    data = encode(image,'PNG')
                    with Path(filename).open('xb') as output:
                        output.write(data)
                    return ''
                except Exception as exc:
                    return str(exc)
            def saved(error):
                if self._closed or serial!=self._serial:
                    return
                self._set_busy(False)
                self.status.setText(f'原图保存失败：{error}' if error is None or error else f'原图已保存：{filename}')
            self._run(save_original,saved,'screenshot-original-save')
            return
        directory = QFileDialog.getExistingDirectory(self, '选择图片保存目录')
        if not directory:
            return
        self._closed = False
        self._serial += 1
        self._cancel = Event()
        paths, serial = list(self._paths), self._serial
        self._set_busy(True)
        self.status.setText('正在保存图片…')

        def work():
            try:
                return export_files(paths, Path(directory)), ''
            except Exception as exc:
                return [], str(exc)

        def done(result):
            if self._closed or serial != self._serial:
                return
            self._set_busy(False)
            if result is None or result[1]:
                self.status.setText(f'保存失败：{result[1] if result else "请重试"}')
            else:
                self.status.setText(f'已保存 {len(result[0])} 张图片到 {directory}')

        self._run(work, done, 'screenshot-save')

    def _configure_shortcuts(self):
        from PySide6.QtWidgets import QApplication
        from .screenshot_shortcuts import install_screenshot_shortcuts
        app = QApplication.instance()
        controller = getattr(app,'_screenshot_shortcuts',None)
        if controller is None:
            controller=install_screenshot_shortcuts(app,self)
        controller.show_settings()

    def begin_capture(self, *, repeat=False):
        if self._busy or self._capture_pending or self._overlays:
            return
        self._closed = False
        self._repeat = repeat
        self._text.dialog.hide()
        self._formula.dialog.hide()
        self._record.result.hide()
        self._capture_pending = True
        self._notice.hide()
        owner = self.parentWidget()
        self._owner_was_visible = bool(owner and owner.isVisible())
        self.hide()
        if self._owner_was_visible:
            owner.hide()
        QTimer.singleShot(150, self._take_frames)

    def _take_frames(self):
        if not self._capture_pending or self._closed:
            self._restore_owner()
            return
        self._capture_pending = False
        # Freeze all displays before opening overlays. Each display keeps its
        # own geometry and pixel ratio; selections do not cross monitors.
        from ..screenshots.regions import binding, matching_region
        from ..screenshots.preferences import CapturePreferences
        frames = [(screen.grabWindow(0).toImage(), screen) for screen in QGuiApplication.screens()]
        saved = CapturePreferences(self._preferences.path).region() if self._repeat else None
        target = next(((image,screen,matching_region(saved,screen,image)) for image,screen in frames
                       if saved and matching_region(saved,screen,image) is not None),None)
        if self._repeat and target is None:
            self._restore_owner()
            self.status.setText('上次区域不可用或显示器配置已改变，请重新框选。')
            self.show()
            return
        active = None
        for image, screen in frames:
            geometry=screen.geometry()
            if image.isNull():
                continue
            initial=matching_region(saved,screen,image) if self._repeat else None
            overlay = ScreenshotOverlay(image, geometry,initial_selection=initial,
                                        small_limits=(self.parts_limit.value(), self.size_limit.value()))
            overlay.small_limits_selected.connect(self._set_small_limits)
            if initial is not None:active=overlay
            def remember(area,s=screen,im=image):
                try:self._preferences.update(region=binding(s,im,area))
                except Exception as exc:self.status.setText(f'上次区域保存失败：{exc}')
            overlay.region_selected.connect(remember)
            overlay.selected.connect(self._selected)
            overlay.small_selected.connect(self._small_selected)
            overlay.scroll_selected.connect(self._scroll_selected)
            overlay.record_selected.connect(self._record_selected)
            overlay.text_selected.connect(self._text_selected)
            overlay.formula_selected.connect(self._formula_selected)
            overlay.cancelled.connect(self._cancel_capture)
            self._overlays.append(overlay)
            overlay.show()
        if active is not None:
            active.raise_();active.activateWindow();active.setFocus()
        if not self._overlays:
            self._restore_owner()
            self.status.setText('无法读取显示器画面，请检查远程桌面或屏幕权限。')
            self.show()

    def _close_overlays(self):
        overlays, self._overlays = self._overlays, []
        for overlay in overlays:
            overlay.close()
            overlay.deleteLater()

    def _restore_owner(self):
        owner = self.parentWidget()
        if self._owner_was_visible and owner:
            owner.show()
        self._owner_was_visible = False

    def _selected(self, image):
        self._close_overlays()
        self._owner_was_visible = False
        self._copy_original(image)

    def _small_selected(self,image):
        self._close_overlays()
        self._owner_was_visible = False
        self.process_image(image,show_result=False)

    def _text_selected(self,image):
        self._close_overlays()
        self._owner_was_visible=False
        self._text.start(image)

    def _formula_selected(self,image):
        self._close_overlays()
        self._owner_was_visible=False
        self._formula.start(image)

    def _record_selected(self,local,geometry):
        self._close_overlays()
        self._owner_was_visible=False
        self._set_busy(True)
        self._serial += 1
        serial = self._serial
        def start():
            if not self._closed and serial == self._serial and self._busy:
                self._record.start(local,geometry)
        QTimer.singleShot(160,start)

    def _copy_original(self,image,*,scroll_message='',complete=True):
        self._closed = False
        self._serial += 1
        serial = self._serial
        self._cancel = Event()
        cancel = self._cancel
        self._preview_image = image.copy()
        self._original_scroll_message = scroll_message
        self._original_complete = complete
        self._paths = []
        self._parts = []
        self._clear_cards()
        self._update_preview()
        self._set_busy(True)
        self._notice.progress('正在复制原图…')
        writer = self._image_writer
        if writer is copy_image:
            hwnd = int(self.winId())
            writer = lambda image: copy_image(image, hwnd=hwnd, cancelled=cancel)
        def work():
            try:
                if cancel.is_set():
                    return '已取消'
                writer(image)
                return ''
            except Exception as exc:
                return str(exc)
        def done(error):
            if self._closed or serial != self._serial:
                return
            self._set_busy(False)
            if error is None or error:
                self.status.setText(f'原图复制失败：{error or "请重试"}')
                self._notice.hide()
                self.show()
            else:
                message = '原图已复制 · Ctrl+V 粘贴'
                if scroll_message:
                    message = ('长图已复制' if complete else '部分长图已复制')+' · '+scroll_message
                self.status.setText(message)
                self._notice.complete(message,allow_small=bool(scroll_message))
        self._run(work,done,'screenshot-original-copy')

    def _scroll_selected(self,local,geometry):
        self._close_overlays()
        self._owner_was_visible = False
        self._set_busy(True)
        QTimer.singleShot(160,lambda: self._start_scroll(local,geometry))

    def _start_scroll(self,local,geometry):
        if self._closed:
            return
        from .scroll_capture import ScrollCapture
        from .screenshot_overlay import crop_physical
        from ..screenshots.scroll_native import NativeScrollTarget
        screen = next((s for s in QGuiApplication.screens() if s.geometry()==geometry),None)
        if screen is None:
            self._set_busy(False)
            self.status.setText('显示器配置改变，请重新框选')
            self.show()
            return
        try:
            target = NativeScrollTarget(screen,local,screen.grabWindow(0).size())
            def without_notice(action):
                global_rect = local.translated(geometry.topLeft())
                visible = self._notice.isVisible()
                covered = global_rect.intersects(self._notice.geometry())
                try:
                    if covered and visible:
                        self._notice.hide()
                        QGuiApplication.processEvents()
                    if self._closed:
                        raise CaptureCancelled()
                    return action()
                finally:
                    if covered and visible and not self._closed:
                        self._notice.show()
            def capture():
                return without_notice(lambda: crop_physical(screen.grabWindow(0).toImage(),local,geometry))
            def wheel():
                return without_notice(target.wheel)
            self._scroll = ScrollCapture(capture,wheel,parent=self)
            self._scroll.progress.connect(lambda text: self._notice.progress(text,screen,stoppable=True))
            self._scroll.finished.connect(self._scroll_finished)
            self._scroll.start()
        except Exception as exc:
            if self._closed:
                return
            self._set_busy(False)
            self.status.setText(f'滚动截图无法开始：{exc}')
            self._notice.hide()
            self.show()

    def _stop_scroll(self):
        if self._scroll is not None:
            self._scroll.stop()

    def _scroll_finished(self,image,message,complete):
        if self._closed:
            return
        self._set_busy(False)
        self._copy_original(image,scroll_message=message,complete=complete)

    def _cancel_capture(self):
        self._close_overlays()
        self._restore_owner()
        self.status.setText('已取消截图；剪贴板未修改。')

    def _update_preview(self):
        if not self._preview_image.isNull():
            self.preview.setPixmap(QPixmap.fromImage(self._preview_image).scaled(
                max(1, self.preview.width() - 12), max(1, self.preview.height() - 12),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'preview'):
            self._update_preview()

    def closeEvent(self, event):
        self._closed = True
        self._serial += 1
        self._cancel.set()
        self._capture_pending = False
        if self._scroll is not None:
            self._scroll.stop(discard=True)
        self._notice.close()
        self._text.close()
        self._formula.close()
        self._record.close()
        self._close_overlays()
        self._restore_owner()
        self._set_busy(False)
        super().closeEvent(event)


def open_screenshot(parent=None, *, repeat=False):
    window = getattr(parent, '_screenshot_window', None) if parent else None
    if window is None:
        window = ScreenshotWindow(parent)
        if parent is not None:
            parent._screenshot_window = window
    if not window._busy:
        window.begin_capture(repeat=repeat)
    return window
