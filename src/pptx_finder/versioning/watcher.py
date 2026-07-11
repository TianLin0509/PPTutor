"""Filesystem watcher for PPTX saves, creates, and moves."""
from __future__ import annotations

import logging
import os
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..config import CONTENT_EXTS, PPTX_EXT

DEBOUNCE_SEC = 1.5
SAVE_RETRY_DELAYS_SEC = (0.75, 2.0, 5.0)
log = logging.getLogger(__name__)

_SKIP_SEGS = (
    "\\windows\\", "\\program files", "\\programdata\\", "\\$recycle.bin\\",
    "\\appdata\\", "\\node_modules\\", "\\.git\\", "\\__pycache__\\",
)


def default_watch_paths() -> list[str]:
    from ..scanner import fixed_drives
    return fixed_drives()


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _under(path: str, root: str) -> bool:
    path = _norm_path(path)
    root = _norm_path(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class _Handler(FileSystemEventHandler):
    def __init__(
        self,
        on_saved,
        on_moved=None,
        roots: list[str] | None = None,
        on_content_saved=None,
    ):
        self._on_saved = on_saved
        self._on_moved = on_moved
        self._on_content_saved = on_content_saved
        self._explicit_skip_roots = [
            _norm_path(r) for r in (roots or [])
            if any(seg in _norm_path(r).lower() for seg in _SKIP_SEGS)
        ]
        self._timers: dict[str, threading.Timer] = {}
        self._retry_delays = SAVE_RETRY_DELAYS_SEC
        self._lock = threading.Lock()

    def _skip_path(self, path: str) -> bool:
        low = _norm_path(path).lower()
        if not any(seg in low for seg in _SKIP_SEGS):
            return False
        return not any(_under(path, root) for root in self._explicit_skip_roots)

    def _trigger(self, path: str) -> None:
        if os.path.splitext(path)[1].lower() not in CONTENT_EXTS:
            return
        if os.path.basename(path).startswith("~$"):
            return
        if self._skip_path(path):
            return
        with self._lock:
            old = self._timers.get(path)
            if old:
                old.cancel()
            t = threading.Timer(DEBOUNCE_SEC, self._fire, args=(path,))
            self._timers[path] = t
            t.start()

    def _schedule_retry(self, path: str, attempt: int) -> None:
        if attempt >= len(self._retry_delays):
            log.warning("watcher save retry exhausted: %s", path)
            return
        with self._lock:
            old = self._timers.get(path)
            if old:
                old.cancel()
            timer = threading.Timer(
                self._retry_delays[attempt],
                self._fire,
                args=(path, attempt + 1),
            )
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str, attempt: int = 0) -> None:
        with self._lock:
            self._timers.pop(path, None)
        ext = os.path.splitext(path)[1].lower()
        if ext not in CONTENT_EXTS:
            return
        callback = self._on_saved if ext == PPTX_EXT else self._on_content_saved
        if callback is None:
            return
        # PowerPoint 保存常用“临时文件 -> 原子替换”，事件到达时目标路径可能
        # 短暂不存在。只对 PPTX 做有限重试；Word/PDF 的 missing 回调承担删索引。
        if ext == PPTX_EXT and not os.path.exists(path):
            self._schedule_retry(path, attempt)
            return
        try:
            callback(path)
        except Exception:  # noqa: BLE001
            self._schedule_retry(path, attempt)

    def on_modified(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.src_path)

    def on_created(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.src_path)

    def on_moved(self, e):  # noqa: N802
        if not e.is_directory:
            if self._on_content_saved is not None and os.path.splitext(e.src_path)[1].lower() in CONTENT_EXTS:
                try:
                    self._on_content_saved(e.src_path)  # 删除旧路径的搜索索引
                except Exception:  # noqa: BLE001
                    pass
            if self._on_moved is not None and os.path.splitext(e.dest_path)[1].lower() == PPTX_EXT:
                try:
                    self._on_moved(e.src_path, e.dest_path)
                except Exception:  # noqa: BLE001
                    pass
            self._trigger(e.dest_path)

    def on_deleted(self, e):  # noqa: N802
        if e.is_directory or self._on_content_saved is None:
            return
        if os.path.splitext(e.src_path)[1].lower() not in CONTENT_EXTS:
            return
        try:
            self._on_content_saved(e.src_path)
        except Exception:  # noqa: BLE001
            pass


class VaultWatcher:
    def __init__(self, roots: list[str], on_saved, on_moved=None, on_content_saved=None):
        self._obs = Observer()
        handler = _Handler(on_saved, on_moved, roots, on_content_saved)
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
