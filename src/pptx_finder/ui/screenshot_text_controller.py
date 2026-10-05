"""Background local recognition, editable results and explicit component install."""
from threading import Event
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from .. import imgtext_ocr
from ..screenshots.text import recognize_image
from ..screenshots.delivery import copy_text
from .screenshot_text import ScreenshotText


class ScreenshotTextController(QObject):
    progress = Signal(int,str)

    def __init__(self,owner,*,writer=None,recognizer=None):
        super().__init__(owner)
        self.owner=owner
        self.writer=writer
        self.recognizer=recognizer
        self.source=QImage()
        self._active_serial=None
        self.dialog=ScreenshotText(owner)
        self.dialog.copy_requested.connect(self.copy)
        self.dialog.retry_requested.connect(lambda:self.start(self.source))
        self.dialog.install_requested.connect(self.install)
        self.dialog.rejected.connect(self.cancel)
        self.progress.connect(self._progress)
        owner._notice.text_requested.connect(self.show)

    def _progress(self,serial,text):
        if not self.owner._closed and serial==self.owner._serial and serial==self._active_serial:
            self.dialog.status.setText(text)
            self.owner._notice.label.setText(text)

    def show(self):
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def start(self,image):
        owner=self.owner
        if owner._busy or image.isNull():return
        owner._closed=False
        self.source=image.copy()
        self.dialog.editor.clear()
        owner._preview_image=image.copy()
        owner._paths=[];owner._parts=[];owner._clear_cards();owner._update_preview()
        if self.recognizer is None and not imgtext_ocr.is_installed():
            self.dialog.install_btn.show()
            self.dialog.status.setText('需要本地识别组件（约 80 MB）。点击下载后自动识别；截图留在本机。')
            self.show()
            return
        self.dialog.install_btn.hide()
        owner._serial+=1
        serial=owner._serial
        self._active_serial=serial
        owner._cancel=Event();cancel=owner._cancel
        owner._set_busy(True);self.dialog.set_busy(True)
        owner._notice.progress('正在本机提取文字…')
        self.dialog.status.setText('正在本机提取文字…')
        recognizer=self.recognizer
        hwnd=int(owner.winId())
        def work():
            text=''
            try:
                text=recognize_image(image,cancelled=cancel,recognizer=recognizer)
                if not text.strip():raise ValueError('未识别到文字，可重新框选更清晰的区域')
                if cancel.is_set():return text,'已取消'
                self._write(text,cancel,hwnd)
                return text,''
            except Exception as exc:return text,str(exc)
        def done(result):
            if owner._closed or serial!=owner._serial:return
            self._active_serial=None
            owner._set_busy(False);self.dialog.set_busy(False)
            text,error=result or ('','识别失败，请重试')
            self.dialog.editor.setPlainText(text)
            owner._set_busy(False)
            if error:
                self.dialog.status.setText('文字处理失败：'+error)
                owner._notice.hide();self.show()
            else:
                self.dialog.status.setText('文字已复制。数字、公式和代码请核对后使用；可修改后再复制。')
                owner.status.setText(f'已复制 {len(text)} 字 · Ctrl+V 粘贴')
                owner._notice.complete(owner.status.text(),allow_text=True)
        owner._run(work,done,'screenshot-text')

    def copy(self,text):
        owner=self.owner
        if owner._busy:return
        owner._closed=False
        owner._serial+=1;serial=owner._serial
        self._active_serial=serial
        owner._cancel=Event()
        cancel=owner._cancel
        owner._set_busy(True);self.dialog.set_busy(True)
        hwnd=int(owner.winId())
        def work():
            try:
                if cancel.is_set():return '已取消'
                self._write(text,cancel,hwnd)
                return ''
            except Exception as exc:return str(exc)
        def done(error):
            if owner._closed or serial!=owner._serial:return
            self._active_serial=None
            owner._set_busy(False);self.dialog.set_busy(False)
            self.dialog.status.setText('复制失败：'+(error or '请重试') if error is None or error else '修改后的文字已复制 · Ctrl+V 粘贴')
        owner._run(work,done,'screenshot-text-copy')

    def install(self):
        owner=self.owner
        if owner._busy:return
        owner._closed=False
        owner._serial+=1;serial=owner._serial
        self._active_serial=serial
        owner._cancel=Event();cancel=owner._cancel
        owner._set_busy(True);self.dialog.set_busy(True)
        self.dialog.status.setText('正在下载本地识别组件…')
        progress=self.progress
        def work():
            try:
                imgtext_ocr.install(progress=lambda done,total:progress.emit(serial,f'下载识别组件：{done/1000000:.1f} / {total/1000000:.1f} MB'),cancel=cancel.is_set)
                return ''
            except Exception as exc:return str(exc)
        def done(error):
            if owner._closed or serial!=owner._serial:return
            self._active_serial=None
            owner._set_busy(False);self.dialog.set_busy(False)
            if error is None or error:self.dialog.status.setText('组件安装失败：'+(error or '请重试'))
            else:self.start(self.source)
        owner._run(work,done,'screenshot-ocr-install')

    def _write(self,text,cancel,hwnd):
        if self.writer is None:
            copy_text(text,hwnd=hwnd,cancelled=cancel)
        elif not cancel.is_set():
            self.writer(text)

    def close(self):
        self.dialog.close()

    def cancel(self):
        if self.owner._busy and self._active_serial==self.owner._serial:
            self.owner._cancel.set()
            self.owner._serial+=1
            self.owner._set_busy(False)
            self.owner._notice.hide()
        # Closing this result view always releases its own controls. The
        # shared image task is cancelled only when it belongs to this view.
        self.dialog.set_busy(False)
        self._active_serial=None
