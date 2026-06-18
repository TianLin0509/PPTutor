"""watchdog 监听受管目录的 .pptx 保存事件 + 防抖，回调快照。

PowerPoint 保存是「写临时文件 → 替换原文件」的原子操作，会产生 created/moved/modified
事件序列；用防抖（事件后等文件稳定）合并成一次「保存完成」再触发快照。
"""
from __future__ import annotations

import os
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DEBOUNCE_SEC = 1.5  # 等 PowerPoint 原子保存完成、文件稳定


class _Handler(FileSystemEventHandler):
    def __init__(self, on_saved):
        self._on_saved = on_saved
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _trigger(self, path: str) -> None:
        if not path.lower().endswith(".pptx"):
            return
        if os.path.basename(path).startswith("~$"):  # Office 锁文件
            return
        with self._lock:
            old = self._timers.get(path)
            if old:
                old.cancel()
            t = threading.Timer(DEBOUNCE_SEC, self._fire, args=(path,))
            self._timers[path] = t
            t.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        try:
            self._on_saved(path)
        except Exception:  # noqa: BLE001 回调失败不能杀监听线程
            pass

    def on_modified(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.src_path)

    def on_created(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.src_path)

    def on_moved(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.dest_path)  # 原子保存 rename 到目标名


class VaultWatcher:
    """监听一组目录（递归），保存 .pptx 时回调 on_saved(path)。"""

    def __init__(self, roots: list[str], on_saved):
        self._obs = Observer()
        handler = _Handler(on_saved)
        for r in roots:
            if os.path.isdir(r):
                self._obs.schedule(handler, r, recursive=True)

    def start(self) -> None:
        self._obs.start()

    def stop(self) -> None:
        try:
            self._obs.stop()
            self._obs.join(timeout=3)
        except Exception:  # noqa: BLE001
            pass
