"""Filesystem watcher for PPTX saves, creates, and moves."""
from __future__ import annotations

import os
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DEBOUNCE_SEC = 1.5

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
    def __init__(self, on_saved, on_moved=None, roots: list[str] | None = None):
        self._on_saved = on_saved
        self._on_moved = on_moved
        self._explicit_skip_roots = [
            _norm_path(r) for r in (roots or [])
            if any(seg in _norm_path(r).lower() for seg in _SKIP_SEGS)
        ]
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _skip_path(self, path: str) -> bool:
        low = _norm_path(path).lower()
        if not any(seg in low for seg in _SKIP_SEGS):
            return False
        return not any(_under(path, root) for root in self._explicit_skip_roots)

    def _trigger(self, path: str) -> None:
        low = path.lower()
        if not low.endswith(".pptx"):
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

    def _fire(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        try:
            self._on_saved(path)
        except Exception:  # noqa: BLE001
            pass

    def on_modified(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.src_path)

    def on_created(self, e):  # noqa: N802
        if not e.is_directory:
            self._trigger(e.src_path)

    def on_moved(self, e):  # noqa: N802
        if not e.is_directory:
            if self._on_moved is not None:
                try:
                    self._on_moved(e.src_path, e.dest_path)
                except Exception:  # noqa: BLE001
                    pass
            self._trigger(e.dest_path)


class VaultWatcher:
    def __init__(self, roots: list[str], on_saved, on_moved=None):
        self._obs = Observer()
        handler = _Handler(on_saved, on_moved, roots)
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
