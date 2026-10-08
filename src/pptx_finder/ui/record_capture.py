"""Qt screen sampling with a bounded queue and a dedicated GIF writer."""
from __future__ import annotations

import time
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal

from ..screenshots.gif_encoding import GifStream, pil_image

_writers = set()


class GifWriter(QThread):
    completed = Signal(object, str)

    def __init__(self, path, *, stream_factory=GifStream):
        super().__init__()
        self.path = Path(path)
        self.stream_factory = stream_factory
        self.frames = Queue(maxsize=3)
        self.ending = Event()
        self.discarding = Event()
        self.published = Event()
        self.commit_lock = Lock()
        self.end_ms = 0

    def submit(self, image, timestamp_ms):
        if self.ending.is_set():
            return False
        try:
            self.frames.put_nowait((image.copy(), timestamp_ms))
            return True
        except Full:
            return False

    def end(self, timestamp_ms, *, discard=False):
        self.end_ms = timestamp_ms
        if discard:
            with self.commit_lock:
                if self.published.is_set():
                    return False
                self.discarding.set()
        self.ending.set()
        return True

    def run(self):
        stream = None
        previous = None
        try:
            stream = self.stream_factory(self.path)
            while not self.discarding.is_set():
                try:
                    current = self.frames.get(timeout=.1)
                except Empty:
                    if self.ending.is_set():
                        break
                    continue
                if previous is not None:
                    if current[0] == previous[0]:
                        continue
                    stream.append(pil_image(previous[0]), current[1] - previous[1])
                previous = current
            if self.discarding.is_set():
                if stream is not None:
                    stream.discard()
                self.completed.emit(None, '')
                return
            if previous is not None:
                stream.append(pil_image(previous[0]), max(10, self.end_ms - previous[1]))
            if self.discarding.is_set():
                if stream is not None:
                    stream.discard()
                self.completed.emit(None, '')
                return
            path = stream.finish(cancelled=self.discarding, commit_lock=self.commit_lock,
                                 published=self.published)
            self.completed.emit(path, '')
        except Exception as exc:
            try:
                if stream is not None:
                    stream.discard()
            except OSError as cleanup:
                exc = RuntimeError(f'{exc}；临时文件清理失败：{cleanup}')
            self.completed.emit(None, str(exc))
        finally:
            while not self.frames.empty():
                try:
                    self.frames.get_nowait()
                except Empty:
                    break


class RecordCapture(QObject):
    progress = Signal(str)
    finished = Signal(object, str)

    def __init__(self, capture, path, *, parent=None, fps=10, max_seconds=120,
                 countdown=3, clock=time.monotonic, writer_factory=GifWriter):
        super().__init__(parent)
        self.capture = capture
        self.path = Path(path)
        self.fps = fps
        self.max_seconds = max_seconds
        self.clock = clock
        self.countdown = countdown
        self.writer_factory = writer_factory
        self.writer = None
        self.state = 'idle'
        self.accepted = self.skipped = 0
        self.elapsed_ms = 0
        self._started_at = 0
        self._capture_error = ''
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)

    def start(self):
        if self.state not in ('idle',):
            return
        if not 1 <= self.fps <= 30 or self.max_seconds <= 0:
            raise ValueError('录制帧率或时长无效')
        self.state = 'countdown'
        if self.countdown <= 0:
            self._begin()
        else:
            self.progress.emit(f'{self.countdown} 秒后录制 · 可准备演示内容')
            self.timer.start(1000)

    def _begin(self):
        self.writer = self.writer_factory(self.path)
        _writers.add(self.writer)
        writer = self.writer
        writer.finished.connect(lambda: _writers.discard(writer))
        writer.completed.connect(self._completed)
        writer.finished.connect(lambda: QTimer.singleShot(1000, writer.deleteLater))
        writer.start(QThread.LowPriority)
        self.state = 'recording'
        self._started_at = self.clock()
        self.timer.start(round(1000 / self.fps))
        self._tick()

    def elapsed(self):
        return self.elapsed_ms + (round((self.clock() - self._started_at) * 1000)
                                  if self.state == 'recording' else 0)

    def _tick(self):
        if self.state == 'countdown':
            self.countdown -= 1
            if self.countdown <= 0:
                self._begin()
            else:
                self.progress.emit(f'{self.countdown} 秒后录制 · 可准备演示内容')
            return
        if self.state != 'recording':
            return
        elapsed = self.elapsed()
        if elapsed >= self.max_seconds * 1000:
            self.stop(message=f'达到 {self.max_seconds} 秒上限，已自动停止')
            return
        try:
            frame = self.capture()
            if self.state != 'recording':
                return
            if frame.isNull():
                raise ValueError('无法读取屏幕画面，请检查显示器或远程桌面连接')
            if self.writer.submit(frame, elapsed):
                self.accepted += 1
            else:
                self.skipped += 1
            message = f'录制中 · {elapsed / 1000:.1f} 秒 · {self.accepted} 帧'
            if self.skipped:
                message += f' · 处理繁忙，跳过 {self.skipped} 次采样'
            self.progress.emit(message)
        except Exception as exc:
            self.stop(message=f'采集已停止：{exc}；保留已录画面')

    def pause(self):
        if self.state == 'recording':
            self.elapsed_ms = self.elapsed()
            self.state = 'paused'
            self.timer.stop()
            self.progress.emit(f'已暂停 · {self.elapsed_ms / 1000:.1f} 秒 · 可继续或停止保存')
        elif self.state == 'paused':
            self._started_at = self.clock()
            self.state = 'recording'
            self.timer.start(round(1000 / self.fps))
            self._tick()

    def stop(self, *, discard=False, message=''):
        if self.state not in ('countdown', 'recording', 'paused', 'saving'):
            return
        self.timer.stop()
        self._capture_error = message or self._capture_error
        if self.writer is None:
            self.state = 'done'
            self.finished.emit(None, '已取消录制；未保存文件')
            return
        if self.state != 'saving':
            self.elapsed_ms = self.elapsed()
        self.state = 'saving'
        if self.writer.end(self.elapsed_ms, discard=discard) is False:
            self._capture_error = 'GIF 已保存，未删除；可从结果窗口查看文件'
        if not discard:
            self.progress.emit('正在保存 GIF · 请稍候…')

    def _completed(self, path, error):
        self.state = 'done'
        self.timer.stop()
        message = '；'.join(item for item in [error, self._capture_error] if item)
        self.finished.emit(path, message)

    def close(self):
        self.stop(discard=True)

    def shutdown(self):
        self.close()
        if self.state != 'done' and self.writer is not None and self.writer.isRunning():
            self.writer.wait()


def shutdown_writers():
    for writer in list(_writers):
        writer.end(0, discard=True)
        writer.wait()
