"""增量自动更新的 Qt 封装：后台检查线程 + 下载线程 + 标题栏 chip 状态机。

与纯逻辑层 updater.py 解耦——这里只管线程与 UI 状态流转，不含网络/文件细节。
chip 单按钮循环：发现新版 → 下载中 NN% → 就绪·重启 →（失败则）重试。全程非模态、不打断搜索。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import updater

log = logging.getLogger(__name__)


class _CancelableNetworkThread:
    """Close an active urllib response so shutdown wakes a blocked read."""

    def _init_response_cancel(self) -> None:
        self._response_lock = threading.Lock()
        self._response = None

    def _set_response(self, response) -> None:
        with self._response_lock:
            self._response = response

    def _close_response(self) -> None:
        with self._response_lock:
            response = self._response
            self._response = None
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 cancellation is best-effort
                pass


def _staging_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pptutor_update"


class _CheckThread(QThread, _CancelableNetworkThread):
    found = Signal(object)  # UpdateInfo
    checked = Signal()
    failed = Signal(str)

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self._url = base_url
        self._cancel = False
        self._init_response_cancel()

    def stop(self) -> None:
        self._cancel = True
        self._close_response()

    def run(self) -> None:
        try:
            info = updater.check_for_update(
                self._url,
                timeout=5.0,
                response_callback=self._set_response,
            )
            if self._cancel:
                return
            if info is not None:
                self.found.emit(info)
            else:
                self.checked.emit()
        except Exception as exc:  # noqa: BLE001 网络/解析失败静默，但交给诊断记录
            if self._cancel:
                return
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _DownloadThread(QThread, _CancelableNetworkThread):
    progress = Signal(int)   # 0-100
    done = Signal()
    failed = Signal(str)

    def __init__(self, base_url: str, info, staging: Path, parent=None):
        super().__init__(parent)
        self._url = base_url
        self._info = info
        self._staging = staging
        self._cancel = False
        self._init_response_cancel()

    def stop(self) -> None:
        self._cancel = True
        self._close_response()

    def run(self) -> None:
        try:
            shutil.rmtree(self._staging, ignore_errors=True)
            updater.download_delta(
                self._url, self._info, self._staging,
                progress=lambda d, t: self.progress.emit(int(d * 100 / t) if t else 100),
                timeout=5.0,
                cancel=lambda: self._cancel,
                response_callback=self._set_response,
            )
            if self._cancel:
                return
            self.done.emit()
        except Exception as e:  # noqa: BLE001
            if self._cancel:
                return
            self.failed.emit(str(e))


class UpdateController(QObject):
    """驱动标题栏更新 chip 的全生命周期。

    chip: 一个 QPushButton（main_window 放在玻璃标题栏）。
    quit_fn: 触发「真正退出」（绕过最小化到托盘）的回调，apply 时调用让 helper 接管。
    """

    def __init__(self, chip, base_url: str, quit_fn, parent=None):
        super().__init__(parent)
        self._chip = chip
        self._url = base_url
        self._quit = quit_fn
        self._state = "idle"
        self._info = None
        self._check_status = "never"
        self._check_error = ""
        self._download_error = ""
        self._staging = _staging_dir()
        self._check: _CheckThread | None = None
        self._dl: _DownloadThread | None = None
        self._download_token = 0
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        chip.hide()
        chip.clicked.connect(self._on_click)

    def _track_thread(self, thread, label: str) -> None:
        try:
            thread._label = label
        except Exception:  # noqa: BLE001
            pass
        if self._parent_bg_tasks is not None and thread not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(thread)
        finished = getattr(thread, "finished", None)
        connect = getattr(finished, "connect", None)
        if callable(connect):
            connect(lambda thread=thread: self._forget_thread(thread))

    def _forget_thread(self, thread) -> None:
        tasks = self._parent_bg_tasks
        if tasks is not None and thread in tasks:
            tasks.remove(thread)

    def _ui_alive(self) -> bool:
        try:
            if not _qt_is_valid(self) or not _qt_is_valid(self._chip):
                return False
            return not getattr(self.parent(), "_closing", False)
        except RuntimeError:
            return False

    def shutdown(self, wait_ms: int = 3000) -> None:
        """关窗收尾：停下并等待检查/下载线程，超时则 terminate。

        修复 P1：这两个线程原先无 stop()，关窗只按 light 任务等 1s 且不 terminate，
        下载中关窗会触发「QThread: Destroyed while thread is still running」崩溃。
        """
        for th in (self._check, self._dl):
            stop = getattr(th, "stop", None)
            if callable(stop):
                try:
                    stop()
                except RuntimeError:
                    pass
        for th in (self._check, self._dl):
            if th is None:
                continue
            try:
                if not th.isRunning():
                    continue
                if not th.wait(wait_ms):
                    # QThread.terminate can kill Python while it owns urllib/Qt
                    # locks. stop() already closed the response; allow one
                    # bounded socket-timeout tail instead of corrupting process state.
                    if not th.wait(max(1000, min(5000, wait_ms))):
                        log.warning(
                            "update network thread still stopping: %s",
                            type(th).__name__,
                        )
            except RuntimeError:
                pass

    # ---------- 检查 ----------
    def start_check(self) -> None:
        if not self._ui_alive():
            return
        if self._state != "idle":
            return
        self._state = "checking"
        self._check_status = "checking"
        self._check_error = ""
        self._check = _CheckThread(self._url, self)
        self._check.found.connect(self._on_found)
        self._check.checked.connect(self._on_check_done)
        self._check.failed.connect(self._on_check_failed)
        self._track_thread(self._check, "update-check")
        self._check.start()

    def _on_found(self, info) -> None:
        if not self._ui_alive():
            return
        if self._state != "checking":
            return
        self._info = info
        self._state = "available"
        self._check_status = f"found v{info.version}"
        self._check_error = ""
        mb = info.total_bytes / 1024 / 1024
        self._chip.setEnabled(True)
        self._chip.setText(f"🔵 新版 v{info.version} · 更新")
        tip = (info.notes + "\n" if info.notes else "") + f"约 {mb:.1f} MB · 点击下载并重启更新"
        self._chip.setToolTip(tip.strip())
        self._chip.show()

    def _on_check_done(self) -> None:
        if not self._ui_alive():
            return
        if self._state != "checking":
            return
        self._state = "idle"
        self._check_status = "ok-no-update"
        self._check_error = ""

    def _on_check_failed(self, msg: str) -> None:
        if not self._ui_alive():
            return
        if self._state != "checking":
            return
        self._state = "idle"
        self._check_status = "failed"
        self._check_error = str(msg or "unknown")
        self._chip.hide()

    def diagnostic_lines(self) -> list[str]:
        line = f"update: state={self._state} check={self._check_status}"
        if self._check_error:
            line += f" error={self._check_error}"
        if self._download_error:
            line += f" download_error={self._download_error}"
        if self._info is not None:
            line += f" version={getattr(self._info, 'version', '')}"
        return [line]

    # ---------- 点击：状态机 ----------
    def _on_click(self) -> None:
        if not self._ui_alive():
            return
        if self._state in ("available", "error"):
            self._start_download()
        elif self._state == "ready":
            self._apply()

    def _start_download(self) -> None:
        if not self._ui_alive():
            return
        self._state = "downloading"
        self._download_token += 1
        token = self._download_token
        self._download_error = ""
        self._chip.setEnabled(False)
        self._chip.setText("下载中 0%")
        self._dl = _DownloadThread(self._url, self._info, self._staging, self)
        self._dl.progress.connect(lambda pct, token=token: self._on_progress(pct, token))
        self._dl.done.connect(lambda token=token: self._on_done(token))
        self._dl.failed.connect(lambda msg, token=token: self._on_failed(msg, token))
        self._track_thread(self._dl, "update-download")
        self._dl.start()

    def _on_progress(self, pct: int, token: int | None = None) -> None:
        if not self._ui_alive():
            return
        if token is not None and token != self._download_token:
            return
        if self._state != "downloading":
            return
        pct = max(0, min(100, int(pct)))
        self._chip.setText(f"下载中 {pct}%")

    def _on_done(self, token: int | None = None) -> None:
        if not self._ui_alive():
            return
        if token is not None and token != self._download_token:
            return
        if self._state != "downloading":
            return
        self._state = "ready"
        self._download_error = ""
        self._chip.setEnabled(True)
        self._chip.setText("✅ 就绪 · 重启更新")
        self._chip.setToolTip("新版已下载完成，点击重启应用完成更新（你的索引和版本库不受影响）")

    def _on_failed(self, msg: str, token: int | None = None) -> None:
        if not self._ui_alive():
            return
        if token is not None and token != self._download_token:
            return
        if self._state not in ("downloading", "applying"):
            return
        self._state = "error"
        self._download_error = str(msg or "unknown")
        self._chip.setEnabled(True)
        reason = self._short_error_reason(msg)
        self._chip.setText(f"⚠ {reason} · 重试")
        self._chip.setToolTip(f"更新失败：{msg}\n点击重试")

    @staticmethod
    def _short_error_reason(msg: str) -> str:
        m = (msg or "").lower()
        if "哈希" in msg or "校验" in msg or "hash" in m or "sha256" in m:
            return "校验失败"
        if "timeout" in m or "timed out" in m or "超时" in msg:
            return "网络超时"
        if "permission" in m or "access denied" in m or "拒绝访问" in msg:
            return "权限不足"
        if "urlopen" in m or "connection" in m or "http" in m or "404" in m or "403" in m:
            return "网络失败"
        return "更新失败"

    def _apply(self) -> None:
        if not self._ui_alive():
            return
        if self._state == "applying":
            return
        self._state = "applying"
        self._chip.setEnabled(False)
        self._chip.setText("正在重启更新…")
        self._chip.setToolTip("正在启动更新 helper，请稍候。")
        try:
            updater.apply_update(
                self._staging, updater.install_dir(), self._info,
                relaunch=Path(sys.executable).name)
        except Exception as e:  # noqa: BLE001
            self._on_failed(str(e))
            return
        self._quit()  # 退出主程序，helper 接管替换 + 重启
