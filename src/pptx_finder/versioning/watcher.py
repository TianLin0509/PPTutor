"""Filesystem watcher for PPTX saves, creates, and moves."""
from __future__ import annotations

import logging
import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..config import CONTENT_EXTS, PPTX_EXT, data_dir
from ..path_policy import explicit_project_output_roots, is_project_output_path

DEBOUNCE_SEC = 1.5
SAVE_RETRY_DELAYS_SEC = (0.75, 2.0, 5.0)
log = logging.getLogger(__name__)

_SKIP_SEGS = (
    "\\windows\\", "\\program files", "\\programdata\\", "\\$recycle.bin\\",
    "\\appdata\\local\\temp\\", "\\node_modules\\", "\\.git\\", "\\__pycache__\\",
    "\\.venv\\", "\\venv\\", "\\env\\", "\\.selftest\\", "\\.arena\\", "\\.ai-team\\",
)


class _PendingCall:
    """One logical debounce entry; many entries share a single wake timer."""

    def __init__(self, owner, path: str, deadline: float, attempt: int):
        self.owner = owner
        self.path = path
        self.deadline = float(deadline)
        self.attempt = int(attempt)
        self.cancelled = False

    def cancel(self) -> None:
        # Preserve the old timer-like testing/debugging surface without creating
        # a native thread for every changed file.
        self.cancelled = True


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
        on_removed=None,
        allowed_exts=None,
    ):
        self._on_saved = on_saved
        self._on_moved = on_moved
        self._on_content_saved = on_content_saved
        self._on_removed = on_removed
        self._allowed_exts_source = allowed_exts
        self._explicit_skip_roots = [
            _norm_path(r) for r in (roots or [])
            if any(seg in _norm_path(r).lower() for seg in _SKIP_SEGS)
        ]
        self._explicit_project_output_roots = list(
            explicit_project_output_roots(roots)
        )
        self._always_skip_roots = [_norm_path(str(data_dir()))]
        self._timers: dict[str, _PendingCall] = {}
        self._retry_delays = SAVE_RETRY_DELAYS_SEC
        self._lock = threading.RLock()
        self._wake_timer: threading.Timer | None = None
        self._wake_deadline = 0.0
        self._stopped = False

    def _allowed_exts(self) -> set[str]:
        source = self._allowed_exts_source
        values = source() if callable(source) else source
        return {e.lower() for e in (values if values is not None else CONTENT_EXTS)}

    def _skip_path(self, path: str) -> bool:
        if any(_under(path, root) for root in self._always_skip_roots):
            return True
        if is_project_output_path(
            path,
            explicit_output_roots=self._explicit_project_output_roots,
        ):
            return True
        low = _norm_path(path).lower()
        if not any(seg in low for seg in _SKIP_SEGS):
            return False
        return not any(_under(path, root) for root in self._explicit_skip_roots)

    def _trigger(self, path: str) -> None:
        if os.path.splitext(path)[1].lower() not in self._allowed_exts():
            return
        if os.path.basename(path).startswith("~$"):
            return
        if self._skip_path(path):
            return
        self._schedule(path, DEBOUNCE_SEC, 0)

    def _schedule(self, path: str, delay: float, attempt: int) -> None:
        with self._lock:
            if self._stopped:
                return
            old = self._timers.get(path)
            if old is not None:
                old.cancelled = True
            pending = _PendingCall(
                self,
                path,
                time.monotonic() + max(0.0, float(delay)),
                attempt,
            )
            self._timers[path] = pending
            self._arm_wake_timer_locked()

    def _arm_wake_timer_locked(self) -> None:
        active = [entry for entry in self._timers.values() if not entry.cancelled]
        if not active or self._stopped:
            if self._wake_timer is not None:
                self._wake_timer.cancel()
            self._wake_timer = None
            self._wake_deadline = 0.0
            return
        deadline = min(entry.deadline for entry in active)
        # A timer already due no later than the new earliest entry can service
        # the whole batch. Reusing it is what turns 1,000 saves into one thread.
        if self._wake_timer is not None and self._wake_deadline <= deadline + 0.001:
            return
        if self._wake_timer is not None:
            self._wake_timer.cancel()
        timer = threading.Timer(
            max(0.0, deadline - time.monotonic()),
            self._drain_due,
        )
        timer.daemon = True
        self._wake_timer = timer
        self._wake_deadline = deadline
        timer.start()

    def _drain_due(self) -> None:
        ready: list[_PendingCall] = []
        with self._lock:
            self._wake_timer = None
            self._wake_deadline = 0.0
            if self._stopped:
                return
            now = time.monotonic()
            for path, entry in list(self._timers.items()):
                if entry.cancelled:
                    if self._timers.get(path) is entry:
                        self._timers.pop(path, None)
                    continue
                if entry.deadline <= now + 0.01:
                    if self._timers.get(path) is entry:
                        self._timers.pop(path, None)
                        ready.append(entry)
            self._arm_wake_timer_locked()
        for entry in ready:
            self._fire(entry.path, entry.attempt, _scheduled=True)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for entry in self._timers.values():
                entry.cancelled = True
            self._timers.clear()
            if self._wake_timer is not None:
                self._wake_timer.cancel()
            self._wake_timer = None
            self._wake_deadline = 0.0

    def _schedule_retry(self, path: str, attempt: int) -> None:
        if attempt >= len(self._retry_delays):
            log.warning("watcher save retry exhausted: %s", path)
            return
        self._schedule(path, self._retry_delays[attempt], attempt + 1)

    def _fire(self, path: str, attempt: int = 0, *, _scheduled: bool = False) -> None:
        with self._lock:
            if self._stopped:
                return
            if not _scheduled:
                self._timers.pop(path, None)
        ext = os.path.splitext(path)[1].lower()
        if ext not in self._allowed_exts():
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
            src_skipped = self._skip_path(e.src_path)
            dest_skipped = self._skip_path(e.dest_path)
            if (
                not src_skipped
                and self._on_content_saved is not None
                and os.path.splitext(e.src_path)[1].lower() in self._allowed_exts()
            ):
                try:
                    self._on_content_saved(e.src_path)  # 删除旧路径的搜索索引
                except Exception:  # noqa: BLE001
                    pass
            if (
                not src_skipped
                and not dest_skipped
                and self._on_moved is not None
                and os.path.splitext(e.dest_path)[1].lower() == PPTX_EXT
            ):
                try:
                    self._on_moved(e.src_path, e.dest_path)
                except Exception:  # noqa: BLE001
                    pass
            elif (
                not src_skipped
                and dest_skipped
                and self._on_removed is not None
                and os.path.splitext(e.src_path)[1].lower() == PPTX_EXT
            ):
                try:
                    self._on_removed(e.src_path)
                except Exception:  # noqa: BLE001
                    pass
            if not dest_skipped:
                self._trigger(e.dest_path)

    def on_deleted(self, e):  # noqa: N802
        if e.is_directory or self._skip_path(e.src_path):
            return
        ext = os.path.splitext(e.src_path)[1].lower()
        if ext not in self._allowed_exts():
            return
        if self._on_content_saved is not None:
            try:
                self._on_content_saved(e.src_path)
            except Exception:  # noqa: BLE001
                pass
        if ext == PPTX_EXT and self._on_removed is not None:
            try:
                self._on_removed(e.src_path)
            except Exception:  # noqa: BLE001
                pass


class VaultWatcher:
    def __init__(
        self,
        roots: list[str],
        on_saved,
        on_moved=None,
        on_content_saved=None,
        on_removed=None,
        allowed_exts=None,
    ):
        self._obs = Observer()
        handler = _Handler(
            on_saved,
            on_moved,
            roots,
            on_content_saved,
            on_removed,
            allowed_exts,
        )
        self._handler = handler
        for r in roots:
            if os.path.isdir(r):
                self._obs.schedule(handler, r, recursive=True)

    def start(self) -> None:
        self._obs.start()

    def stop(self) -> None:
        try:
            self._handler.stop()
            self._obs.stop()
            self._obs.join(timeout=3)
        except Exception:  # noqa: BLE001
            pass
