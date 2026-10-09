"""On-demand OCR process reuse, bounded waits and automatic idle cleanup."""
from __future__ import annotations

import atexit
from collections import deque
import json
import os
import queue
import subprocess
import threading
import time
import uuid


class OcrSession:
    def __init__(self, *, idle_seconds=45, timeout=120):
        self.idle_seconds, self.timeout = idle_seconds, timeout
        self._lock = threading.RLock()
        self._process = None
        self._command = None
        self._timer = None
        self._generation = 0
        self._job = None

    def _stop(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        process, self._process = self._process, None
        if process:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        if self._job:
            self._job.Close()
            self._job = None

    def close(self):
        with self._lock:
            self._stop()

    def _idle(self, generation):
        with self._lock:
            if generation == self._generation:
                self._stop()

    def _start(self, command):
        self._stop()
        self._responses = queue.Queue()
        self._logs = deque(maxlen=8)
        process = subprocess.Popen([*command, '--serve'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', bufsize=1,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                   env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self._process, self._command = process, list(command)
        try:
            # Release the OCR child even if the parent is killed during an update.
            if __import__('sys').platform == 'win32':
                import win32job, win32api, win32con
                job = win32job.CreateJobObject(None, 'PPTDoctorOcr-' + uuid.uuid4().hex)
                self._job = job
                info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
                handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
                try:
                    win32job.AssignProcessToJobObject(job, handle)
                finally:
                    handle.Close()
        except Exception:
            self._stop()
            raise
        responses, logs = self._responses, self._logs
        def read_output():
            try:
                for line in process.stdout:
                    if line.startswith('PPTDOCTOR_OCR:'):
                        responses.put(line[len('PPTDOCTOR_OCR:'):])
                    else:
                        logs.append(line[-400:])
            except (OSError, ValueError) as exc:
                logs.append(f'读取文字识别响应失败：{exc}')
            finally:
                responses.put(None)
        def read_errors():
            try:
                for line in process.stderr:
                    logs.append(line[-400:])
            except (OSError, ValueError) as exc:
                logs.append(f'读取文字识别日志失败：{exc}')
        threading.Thread(target=read_output, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()

    def recognize(self, command, paths, *, cancelled=None):
        from .imgtext_ocr import OcrUnavailable
        from .screenshots.encoding import CaptureCancelled
        with self._lock:
            if cancelled is not None and cancelled.is_set():
                raise CaptureCancelled()
            if self._timer:
                self._timer.cancel(); self._timer = None
            try:
                if (self._process is None or self._process.poll() is not None
                        or self._command != list(command)):
                    self._start(command)
                request_id = uuid.uuid4().hex
                self._process.stdin.write(json.dumps({'id': request_id, 'images': paths}, ensure_ascii=False) + '\n')
                self._process.stdin.flush()
                deadline = time.monotonic() + self.timeout
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise CaptureCancelled()
                    if time.monotonic() >= deadline:
                        raise OcrUnavailable('本地文字识别超时，请缩小选区后重试')
                    try:
                        response = self._responses.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if response is None:
                        raise OcrUnavailable('识别进程退出：' + ''.join(self._logs)[-500:])
                    payload = json.loads(response)
                    if payload.get('id') != request_id:
                        raise OcrUnavailable('识别组件返回了不匹配的请求')
                    if not payload.get('ok'):
                        raise OcrUnavailable('本地文字识别失败：' + str(payload.get('error', '未知错误')))
                    results = payload.get('results')
                    if not isinstance(results, dict) or any(p not in results for p in paths):
                        raise OcrUnavailable('文字识别结果不完整')
                    return results
            except CaptureCancelled:
                self._stop()
                raise
            except Exception as exc:
                self._stop()
                if isinstance(exc, OcrUnavailable):
                    raise
                raise OcrUnavailable(f'识别组件通信失败：{exc}') from exc
            finally:
                if self._process is not None:
                    self._generation += 1
                    self._timer = threading.Timer(self.idle_seconds, self._idle, (self._generation,))
                    self._timer.daemon = True
                    self._timer.start()


session = OcrSession()
atexit.register(session.close)
