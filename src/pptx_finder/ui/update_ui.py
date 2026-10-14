"""自动更新的 Qt 封装：后台检查线程 + 下载线程 + 更新弹窗 + 提醒节奏（v2.0）。

与纯逻辑层 updater.py 解耦——这里只管线程、状态流转与提醒，不含网络/文件细节。

提醒节奏照 AI Hub 一键更新（2026-10-13 田哥确认「效果很好」）：
- 启动后后台查；发现新版本时**同一版本只自动弹一次窗**（版本号记在 ui.json）；
- 之后挂「未读」（unread_changed 信号 → 主窗口在设置入口画红点），用户从入口点开、
  或在弹窗里点了「下载更新」算已读；出了更新的版本再弹一次；
- 标题栏 chip 只在需要用户操作的时候出现：下载中、已就绪待重启、下载失败可重试。
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Signal
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import updater

log = logging.getLogger(__name__)

RELEASE_PAGE = "https://github.com/TianLin0509/PPTutor/releases/latest"
_PROMPTED_KEY = "update_prompted_version"
_SEEN_KEY = "update_seen_version"


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
    reported = Signal(object)  # updater.CheckReport（在 found/checked/failed 之前发）
    found = Signal(object)     # UpdateInfo
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
            report = updater.check_sources(
                self._url,
                timeout=6.0,
                response_callback=self._set_response,
            )
            if self._cancel:
                return
            self.reported.emit(report)
            if report.best is not None:
                self.found.emit(report.best)
            elif report.all_failed:
                self.failed.emit(report.error or "所有更新源都连不上")
            else:
                self.checked.emit()
        except Exception as exc:  # noqa: BLE001 网络/解析失败静默，但交给诊断记录
            if self._cancel:
                return
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _DownloadThread(QThread, _CancelableNetworkThread):
    progress = Signal(int)      # 0-100
    status = Signal(object)     # dict：源名、已收/总、速度、剩余秒数
    attempts = Signal(object)   # list[dict]：每个源的结果
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
            # download_update：逐个源试；每个源先增量，块取不到（用户落后好几版）就整包兜底
            updater.download_update(
                self._url, self._info, self._staging,
                progress=lambda d, t: self.progress.emit(int(d * 100 / t) if t else 100),
                timeout=8.0,
                cancel=lambda: self._cancel,
                response_callback=self._set_response,
                on_status=self.status.emit,
                on_attempts=self.attempts.emit,
            )
            if self._cancel:
                return
            self.done.emit()
        except Exception as e:  # noqa: BLE001
            if self._cancel:
                return
            self.failed.emit(str(e))


class UpdateController(QObject):
    """更新的全生命周期：检查 → 弹窗/红点 → 下载（逐源回落）→ 就绪 → 重启替换。

    chip: 一个 QPushButton（主窗口标题栏）。只在下载中 / 已就绪 / 下载失败时显示。
    quit_fn: 触发「真正退出」（绕过最小化到托盘）的回调，apply 时调用让 helper 接管。

    对外：
    - ``state_changed``：任何状态变化（弹窗据此重绘）。
    - ``unread_changed(bool, str)``：是否有未读的新版本、版本号（设置入口画红点用）。
    - ``open_dialog()``：打开更新弹窗；没查过或上次失败就立刻现查。
    - ``check_now()``：手动检查（不受「只自动弹一次」限制，结果显示在弹窗里）。
    - ``pending_version()`` / ``is_unread()``：给入口文案用。
    """

    state_changed = Signal()
    unread_changed = Signal(bool, str)

    def __init__(self, chip, base_url: str, quit_fn, parent=None, *, auto_prompt: bool = True):
        super().__init__(parent)
        self._chip = chip
        self._url = base_url
        self._quit = quit_fn
        self._auto_prompt = auto_prompt
        self._state = "idle"
        self._info = None
        self._report = None
        self._check_status = "never"
        self._check_error = ""
        self._download_error = ""
        self._status: dict = {}
        self._attempts: list = []
        self._ready_source = ""
        self._staging = _staging_dir()
        self._apply_failed = False   # 下载成功、但「立即重启」那一步没跑起来
        self._check: _CheckThread | None = None
        self._dl: _DownloadThread | None = None
        self._download_token = 0
        self._dialog = None
        self._prompt_waiting = False
        self._manual_check = False
        self._last_unread: tuple[bool, str] | None = None
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        chip.hide()
        chip.clicked.connect(self._on_click)

    # ---------- 线程登记 ----------
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
        """关窗收尾：停下并等待检查/下载线程（不 terminate）。"""
        dialog = self._dialog
        self._dialog = None
        if dialog is not None:
            try:
                dialog.close()
            except RuntimeError:
                pass
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

    # ---------- 对外查询 ----------
    def phase(self) -> str:
        """弹窗用的阶段：checking / none / available / downloading / ready / applying / error / idle。"""
        if self._state == "idle":
            if self._check_status == "ok-no-update":
                return "none"
            if self._check_status == "failed":
                return "error"
            return "idle"
        return self._state

    def snapshot(self) -> dict:
        info = self._info
        report = self._report
        checks = [c.as_dict() for c in (getattr(report, "checks", None) or [])]
        current = getattr(report, "current", "") or _current_version()
        error = self._download_error if self._state == "error" else (
            self._check_error if self.phase() == "error" else "")
        return {
            "phase": self.phase(),
            "current": current,
            "latest": getattr(info, "version", "") if info is not None else "",
            "notes": getattr(info, "notes", "") if info is not None else "",
            "bytes": getattr(info, "total_bytes", 0) if info is not None else 0,
            "files": len(getattr(info, "changed", []) or []) if info is not None else 0,
            "kind": getattr(info, "kind", "incremental") if info is not None else "",
            "source_names": [s.name for s in (getattr(info, "sources", None) or [])],
            "checks": checks,
            "attempts": list(self._attempts),
            "status": dict(self._status),
            "ready_source": self._ready_source,
            "error": _friendly_error(error),
            "release_page": RELEASE_PAGE,
            "apply_failed": self._apply_failed and self._state == "error",
        }

    def pending_version(self) -> str:
        if self._info is not None and self._state in ("available", "downloading", "ready", "error"):
            return str(self._info.version)
        return ""

    def is_unread(self) -> bool:
        version = self.pending_version()
        return bool(version) and _read_setting(_SEEN_KEY) != version

    def _notify(self) -> None:
        unread = (self.is_unread(), self.pending_version())
        if unread != self._last_unread:
            self._last_unread = unread
            self.unread_changed.emit(*unread)
        self.state_changed.emit()

    def _mark_seen(self) -> None:
        version = self.pending_version()
        if version:
            _write_setting(**{_SEEN_KEY: version, _PROMPTED_KEY: version})
        self._notify()

    # ---------- 检查 ----------
    def start_check(self, *, manual: bool = False) -> None:
        if not self._ui_alive():
            return
        if self._state != "idle":
            return
        self._manual_check = manual
        self._state = "checking"
        self._check_status = "checking"
        self._check_error = ""
        self._check = _CheckThread(self._url, self)
        reported = getattr(self._check, "reported", None)
        if reported is not None:
            reported.connect(self._on_report)
        self._check.found.connect(self._on_found)
        self._check.checked.connect(self._on_check_done)
        self._check.failed.connect(self._on_check_failed)
        self._track_thread(self._check, "update-check")
        self._check.start()
        self._notify()

    def check_now(self) -> None:
        """手动检查：已经查到更新（或在下载/就绪）就不重复查，直接显示当前状态。"""
        if self._state == "idle":
            self.start_check(manual=True)
        self._notify()

    def _on_report(self, report) -> None:
        if not self._ui_alive():
            return
        self._report = report

    def _on_found(self, info) -> None:
        if not self._ui_alive():
            return
        if self._state != "checking":
            return
        self._info = info
        self._state = "available"
        self._check_status = f"found v{info.version}"
        self._check_error = ""
        # 「有新版本」不再常驻标题栏：自动弹一次窗 + 设置入口红点
        self._chip.hide()
        self._notify()
        self._maybe_prompt_once()

    def _on_check_done(self) -> None:
        if not self._ui_alive():
            return
        if self._state != "checking":
            return
        self._state = "idle"
        self._check_status = "ok-no-update"
        self._check_error = ""
        self._notify()

    def _on_check_failed(self, msg: str) -> None:
        if not self._ui_alive():
            return
        if self._state != "checking":
            return
        self._state = "idle"
        self._check_status = "failed"
        self._check_error = str(msg or "unknown")
        self._chip.hide()
        self._notify()

    def diagnostic_lines(self) -> list[str]:
        line = f"update: state={self._state} check={self._check_status}"
        if self._check_error:
            line += f" error={self._check_error}"
        if self._download_error:
            line += f" download_error={self._download_error}"
        if self._info is not None:
            line += f" version={getattr(self._info, 'version', '')}"
        report = self._report
        for c in getattr(report, "checks", None) or []:
            line += f" | {c.name}:{'ok v' + c.version if c.ok else c.reason}"
        return [line]

    # ---------- 弹窗与提醒 ----------
    def _dialog_parent(self):
        from PySide6.QtWidgets import QWidget
        parent = self.parent()
        return parent if isinstance(parent, QWidget) else None

    def _maybe_prompt_once(self) -> None:
        """同一版本只自动弹一次；主窗口还藏在托盘里就等它第一次露面再弹。"""
        version = self.pending_version()
        if not version or self._state != "available":
            return
        if getattr(self, "_manual_check", False):
            # 用户自己点「检查更新」查到的：弹窗本来就开着，算已读
            if self._dialog is not None:
                self._mark_seen()
            return
        if not self._auto_prompt or _read_setting(_PROMPTED_KEY) == version:
            return
        parent = self._dialog_parent()
        if parent is None:
            return
        if not parent.isVisible():
            if not self._prompt_waiting:
                self._prompt_waiting = True
                parent.installEventFilter(self)
            return
        _write_setting(**{_PROMPTED_KEY: version})
        self.open_dialog(mark_seen=False)

    def eventFilter(self, obj, event):  # noqa: N802
        if self._prompt_waiting and event.type() == QEvent.Show:
            self._prompt_waiting = False
            try:
                obj.removeEventFilter(self)
            except RuntimeError:
                pass
            QTimer.singleShot(600, self._maybe_prompt_once)
        return False

    def open_dialog(self, *, mark_seen: bool = True) -> None:
        """打开更新弹窗。从入口点开 = 已读；还没查过或上次失败就立刻现查。"""
        if not self._ui_alive():
            return
        from .update_dialog import UpdateDialog
        if self._dialog is None or not _qt_is_valid(self._dialog):
            self._dialog = UpdateDialog(self, self._dialog_parent())
            self._dialog.finished.connect(lambda *_: setattr(self, "_dialog", None))
        if mark_seen:
            self._mark_seen()
        if self._state == "idle" and self._check_status in ("never", "failed"):
            self.start_check(manual=True)
        self._dialog.refresh()
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def announce_last_result(self, toast) -> str:
        """更新后首次启动：把 helper 写下的结果提示一次（toast 由主窗口给）。"""
        try:
            text = updater.describe_update_result(
                updater.consume_update_result(), _current_version())
        except Exception:  # noqa: BLE001
            text = ""
        if text and callable(toast):
            toast(text)
        return text

    # ---------- 点击 chip：状态机 ----------
    def _on_click(self) -> None:
        if not self._ui_alive():
            return
        if self._state == "error" and self._apply_failed:
            self.retry_apply()
        elif self._state in ("available", "error"):
            self._start_download()
        elif self._state == "ready":
            self._apply()
        elif self._state == "downloading":
            self.open_dialog()

    def start_download(self) -> None:
        """弹窗「下载更新」/「重试」：点了就算已读。"""
        self._mark_seen()
        if self._state in ("available", "error"):
            self._start_download()
        elif self._state == "idle":
            self.start_check(manual=True)

    def cancel_download(self) -> None:
        dl = self._dl
        if dl is not None and self._state == "downloading":
            stop = getattr(dl, "stop", None)
            if callable(stop):
                stop()
            self._download_token += 1
            self._state = "available"
            self._status = {}
            self._attempts = [dict(a, status="cancelled", reason="已取消")
                              if a.get("status") == "downloading" else a
                              for a in self._attempts]
            self._chip.hide()
            self._notify()

    def apply(self) -> None:
        if self._state == "ready":
            self._apply()

    def retry_apply(self) -> None:
        """重启那一步失败（新版本已下载好）：不重新下载，直接再试一次替换。"""
        if self._state == "error" and self._apply_failed and self._staging.exists():
            self._state = "ready"
            self._apply()
        elif self._state == "error":
            self._start_download()

    def _start_download(self) -> None:
        if not self._ui_alive():
            return
        self._state = "downloading"
        self._apply_failed = False
        self._download_token += 1
        token = self._download_token
        self._download_error = ""
        self._status = {}
        self._attempts = []
        self._ready_source = ""
        self._chip.setEnabled(False)
        self._chip.setText("下载中 0%")
        self._chip.setToolTip("正在下载新版本，点击查看进度")
        self._chip.show()
        self._dl = _DownloadThread(self._url, self._info, self._staging, self)
        self._dl.progress.connect(lambda pct, token=token: self._on_progress(pct, token))
        status = getattr(self._dl, "status", None)
        if status is not None:
            status.connect(lambda st, token=token: self._on_status(st, token))
        attempts = getattr(self._dl, "attempts", None)
        if attempts is not None:
            attempts.connect(lambda at, token=token: self._on_attempts(at, token))
        self._dl.done.connect(lambda *_a, token=token: self._on_done(token))
        self._dl.failed.connect(lambda msg, token=token: self._on_failed(msg, token))
        self._track_thread(self._dl, "update-download")
        self._dl.start()
        self._notify()

    def _on_progress(self, pct: int, token: int | None = None) -> None:
        if not self._ui_alive():
            return
        if token is not None and token != self._download_token:
            return
        if self._state != "downloading":
            return
        pct = max(0, min(100, int(pct)))
        self._chip.setText(f"下载中 {pct}%")
        self._chip.setEnabled(True)  # 下载中点 chip = 看进度

    def _on_status(self, status: dict, token: int | None = None) -> None:
        if not self._ui_alive() or (token is not None and token != self._download_token):
            return
        if self._state != "downloading":
            return
        self._status = dict(status or {})
        self.state_changed.emit()

    def _on_attempts(self, attempts: list, token: int | None = None) -> None:
        if not self._ui_alive() or (token is not None and token != self._download_token):
            return
        self._attempts = [dict(a) for a in (attempts or [])]
        self.state_changed.emit()

    def _on_done(self, token: int | None = None) -> None:
        if not self._ui_alive():
            return
        if token is not None and token != self._download_token:
            return
        if self._state != "downloading":
            return
        self._state = "ready"
        self._download_error = ""
        used = getattr(self._info, "used_source", None)
        self._ready_source = getattr(used, "name", "") or next(
            (a.get("name", "") for a in self._attempts if a.get("status") == "ok"), "")
        self._chip.setEnabled(True)
        self._chip.setText("✅ 已就绪 · 重启生效")
        self._chip.setToolTip("新版已下载并校验通过，点击重启完成更新（你的索引和版本库不受影响）")
        self._chip.show()
        self._notify()

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
        self._chip.show()
        self._notify()

    @staticmethod
    def _short_error_reason(msg: str) -> str:
        m = (msg or "").lower()
        if "网页" in msg or "拦截" in msg:
            return "被代理拦截"
        if "哈希" in msg or "校验" in msg or "hash" in m or "sha256" in m:
            return "校验失败"
        if "timeout" in m or "timed out" in m or "超时" in msg:
            return "网络超时"
        if "permission" in m or "access denied" in m or "拒绝访问" in msg:
            return "权限不足"
        if ("urlopen" in m or "connection" in m or "http" in m or "404" in m or "403" in m
                or "连不上" in msg or "解析" in msg or "中断" in msg):
            return "网络失败"
        return "更新失败"

    def _apply(self) -> None:
        if not self._ui_alive():
            return
        if self._state == "applying":
            return
        self._state = "applying"
        self._apply_failed = False
        self._chip.setEnabled(False)
        self._chip.setText("正在启动更新程序…")
        self._chip.setToolTip("正在启动负责替换文件的更新程序，确认它跑起来后本程序才会关闭。")
        self._notify()
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()  # 握手要等 1–3 秒，先把「正在启动」画出来
        except Exception:  # noqa: BLE001
            pass
        try:
            # apply_update 会等 helper 写出握手文件才返回；没跑起来就抛异常，程序留在原地
            updater.apply_update(
                self._staging, updater.install_dir(), self._info,
                relaunch=updater.relaunch_name(
                    self._info, Path(sys.executable).name))
        except Exception as e:  # noqa: BLE001
            log.warning("update helper failed to start: %s", e)
            self._apply_failed = True
            self._on_failed(f"{e}。程序没有关闭，新版本也已下载好：可以点「再试一次」，"
                            "或点「打开发布页」下载安装包手动更新。")
            return
        log.info("update helper started, quitting for v%s", getattr(self._info, "version", "?"))
        self._quit()  # helper 已确认在跑：退出主程序，由它替换文件并重新打开


# ---------- 小工具 ----------
def _current_version() -> str:
    from .. import __version__
    return __version__


def _read_setting(key: str) -> str:
    try:
        from ..config import load_ui_settings
        return str(load_ui_settings().get(key) or "")
    except Exception:  # noqa: BLE001
        return ""


def _write_setting(**changes) -> None:
    try:
        from ..config import update_ui_settings
        update_ui_settings(**changes)
    except Exception:  # noqa: BLE001
        log.debug("update reminder state not persisted", exc_info=True)


def _friendly_error(msg: str) -> str:
    """下载失败的原始消息里已经逐源列了原因；只把纯技术前缀去掉。"""
    text = str(msg or "")
    for prefix in ("AllSourcesFailed: ", "ValueError: ", "RuntimeError: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text
