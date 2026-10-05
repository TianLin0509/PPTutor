"""Timer-driven screen capture; stitching runs on a background thread."""
from threading import Event
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from ..screenshots.stitching import Stitcher
from .bg_task import BackgroundTask

_tasks = set()


class ScrollCapture(QObject):
    progress = Signal(str)
    finished = Signal(object,str,bool)

    def __init__(self,capture,wheel,*,parent=None,interval=450,max_frames=60):
        super().__init__(parent)
        self.capture = capture
        self.wheel = wheel
        self.interval = interval
        self.max_frames = max_frames
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)
        self.cancel = Event()
        self.stitcher = None
        self._working = False
        self._active = False
        self._unchanged = 0

    def start(self):
        if self._active:
            return
        self.stitcher = Stitcher(self.capture())
        self._active = True
        self.progress.emit('正在滚动截图 · 点击停止可结束')
        self._scroll()

    def _scroll(self):
        if not self._active:
            return
        try:
            self.wheel()
            self.timer.start(self.interval)
        except Exception as exc:
            self._finish(str(exc),False)

    def _tick(self):
        if not self._active:
            return
        try:
            frame = self.capture()
        except Exception as exc:
            self._finish(str(exc),False)
            return
        self._working = True
        stitcher = self.stitcher
        def work():
            try:
                return stitcher.add(frame),''
            except Exception as exc:
                return 0,str(exc)
        task = BackgroundTask(work,label='screenshot-scroll-stitch')
        _tasks.add(task)
        task.finished.connect(lambda: _tasks.discard(task))
        task.done.connect(self._added)
        task.start(QThread.LowPriority)

    def _added(self,result):
        self._working = False
        if not self._active:
            return
        if self.cancel.is_set():
            self._finish('已由用户停止，保留已截取部分',False)
            return
        if result is None or result[1]:
            self._finish(result[1] if result else '长图拼接失败',False)
            return
        self._unchanged = self._unchanged+1 if result[0]==0 else 0
        if self._unchanged>=3:
            moved = self.stitcher.frames > 1
            self._finish('画面连续未再移动，滚动截图结束' if moved else
                         '目标未发生滚动，保留当前画面；请框选可滚动正文', moved)
        elif self.stitcher.frames>=self.max_frames:
            self._finish('已达到滚动次数上限，保留已截取部分',False)
        else:
            self.progress.emit(f'正在滚动截图 · 已拼接 {self.stitcher.frames} 屏')
            self._scroll()

    def stop(self,*,discard=False):
        self.cancel.set()
        self.timer.stop()
        if discard:
            self._active = False
        elif not self._working:
            self._finish('已由用户停止，保留已截取部分',False)

    def _finish(self,message,complete):
        if not self._active:
            return
        self._active = False
        self.timer.stop()
        self.finished.emit(self.stitcher.result(),message,complete)
