"""Formula operations own their serial/Event and never publish a stale result."""
from threading import Event
from PySide6.QtCore import QObject,Signal
from PySide6.QtGui import QImage
from ..screenshots import formula_component,formula_preview
from ..screenshots.formula_format import clean_latex,to_word
from ..screenshots.delivery import copy_text
from .screenshot_formula import ScreenshotFormula


class ScreenshotFormulaController(QObject):
    progress=Signal(int,str)

    def __init__(self,owner,*,recognizer=None,renderer=None,writer=None):
        super().__init__(owner);self.owner=owner;self.source=QImage();self._active_serial=None
        self.recognizer=recognizer or formula_component.recognize;self.renderer=renderer or formula_preview.render;self.writer=writer
        self.dialog=ScreenshotFormula(owner)
        self.dialog.copy_requested.connect(self.copy);self.dialog.refresh_requested.connect(self.refresh)
        self.dialog.retry_requested.connect(lambda:self.start(self.source));self.dialog.install_requested.connect(self.install)
        self.dialog.rejected.connect(self.cancel);self.progress.connect(self._progress)

    def _progress(self,serial,text):
        if not self.owner._closed and serial==self.owner._serial and serial==self._active_serial:self.dialog.status.setText(text)

    def show(self):self.dialog.show();self.dialog.raise_();self.dialog.activateWindow()

    def _begin(self,status):
        owner=self.owner;owner._closed=False;owner._serial+=1;self._active_serial=owner._serial
        owner._cancel=Event();owner._set_busy(True);self.dialog.set_busy(True);self.dialog.status.setText(status)
        return owner._serial,owner._cancel,int(owner.winId())

    def _valid(self,serial):return not self.owner._closed and serial==self.owner._serial and serial==self._active_serial

    def _end(self):self._active_serial=None;self.owner._set_busy(False);self.dialog.set_busy(False)

    def _write(self,text,cancel,hwnd):
        if self.writer is None:copy_text(text,hwnd=hwnd,cancelled=cancel)
        elif not cancel.is_set():self.writer(text)

    def start(self,image):
        if self.owner._busy or image.isNull():return
        self.owner._closed=False;self.source=image.copy();self.dialog.set_latex('');self.dialog.word.clear()
        self.owner._preview_image=image.copy();self.owner._paths=[];self.owner._parts=[]
        self.owner._original_scroll_message='';self.owner._original_complete=True
        self.owner._clear_cards();self.owner._update_preview()
        self.dialog.preview.clear();self.dialog.preview.setText('识别后显示公式');self.dialog.set_image(self.dialog.source,image)
        self.dialog.update_controls();self.owner.formula_result_btn.setEnabled(False)
        self.show()
        if self.recognizer is formula_component.recognize and not formula_component.is_installed():
            bundled=formula_component.bundled_dir()
            self.dialog.install_btn.setText('安装内置公式组件' if bundled else '下载公式识别组件')
            self.dialog.install_btn.show();self.dialog.status.setText('首次使用请安装内置公式组件，无需联网。截图在本机识别。' if bundled else '首次使用请下载公式识别组件。之后在本机识别，截图不上传。');return
        self.dialog.install_btn.hide();serial,cancel,hwnd=self._begin('正在本机识别公式…首次加载模型需要稍等')
        def work():
            result=None
            try:
                latex=clean_latex(self.recognizer(image,cancelled=cancel))
                result=self._formats(latex,cancel)
                self._write(latex,cancel,hwnd)
                return result,''
            except Exception as exc:return result,str(exc)
        def done(result):
            if not self._valid(serial):return
            self._end();value,error=result or (None,'识别没有返回结果')
            if error:
                if value:self._apply(value,'已识别；自动复制失败：'+error)
                else:self.dialog.status.setText('公式识别失败：'+error)
                return
            self._apply(value,'LaTeX 已复制 · 请核对原图，再选择需要的格式。')
        self.owner._run(work,done,'screenshot-formula')

    def _formats(self,latex,cancel):
        word='';image=QImage();warnings=[]
        try:word=to_word(latex)
        except Exception as exc:warnings.append('Word：'+str(exc))
        if cancel.is_set():raise InterruptedError('已取消')
        try:image=self.renderer(latex,cancelled=cancel)
        except Exception as exc:warnings.append('预览：'+str(exc))
        if cancel.is_set():raise InterruptedError('已取消')
        return latex,word,image,'；'.join(warnings)

    def _apply(self,result,message):
        latex,word,image,warning=result;self.dialog.set_latex(latex);self.dialog.word.setPlainText(word)
        if image.isNull():self.dialog.preview.setText('预览不可用，请对照原图核对')
        else:self.dialog.set_image(self.dialog.preview,image)
        self.dialog.status.setText(message+(' '+warning if warning else ''));self.dialog.update_controls()
        self.owner.formula_result_btn.setEnabled(bool(latex))

    def refresh(self):
        if self.owner._busy:return
        try:latex=clean_latex(self.dialog.editor.toPlainText())
        except ValueError as exc:self.dialog.status.setText(str(exc));return
        serial,cancel,hwnd=self._begin('正在更新公式预览和 Word 输入格式…')
        def work():
            try:return self._formats(latex,cancel),''
            except Exception as exc:return None,str(exc)
        def done(result):
            if not self._valid(serial):return
            self._end();value,error=result or (None,'更新没有返回结果')
            if error:self.dialog.status.setText('更新失败：'+error)
            else:self._apply(value,'已更新 · 请对照原图核对后复制。')
        self.owner._run(work,done,'screenshot-formula-preview')

    def copy(self,mode):
        if self.owner._busy:return
        try:text=clean_latex(self.dialog.editor.toPlainText()) if mode=='latex' else self.dialog.word.toPlainText()
        except ValueError as exc:self.dialog.status.setText(str(exc));return
        if not text.strip():return
        serial,cancel,hwnd=self._begin('正在复制公式…')
        def work():
            try:self._write(text,cancel,hwnd);return ''
            except Exception as exc:return str(exc)
        def done(error):
            if not self._valid(serial):return
            self._end();self.dialog.status.setText('复制失败：'+(error or '请重试') if error is None or error else ('LaTeX 已复制 · Ctrl+V 粘贴' if mode=='latex' else 'Word 公式已复制 · Alt+= 进入 UnicodeMath 公式框后粘贴'))
        self.owner._run(work,done,'screenshot-formula-copy')

    def install(self):
        if self.owner._busy:return
        serial,cancel,hwnd=self._begin('正在准备公式识别组件…');progress=self.progress
        def work():
            try:formula_component.install(cancelled=cancel,progress=lambda a,b:progress.emit(serial,f'下载公式组件：{a/1000000:.1f} / {b/1000000:.1f} MB'));return ''
            except Exception as exc:return str(exc)
        def done(error):
            if not self._valid(serial):return
            self._end()
            if error is None or error:self.dialog.status.setText('组件安装失败：'+(error or '请重试'))
            else:self.start(self.source)
        self.owner._run(work,done,'screenshot-formula-install')

    def cancel(self):
        if self.owner._busy and self._active_serial==self.owner._serial:
            self.owner._cancel.set();self.owner._serial+=1;self.owner._set_busy(False)
        self._active_serial=None;self.dialog.set_busy(False)

    def close(self):self.dialog.close()
