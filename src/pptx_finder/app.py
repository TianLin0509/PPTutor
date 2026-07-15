"""应用入口：托盘常驻 + 全局热键唤起 + 主窗口。

形态：QSystemTrayIcon 常驻；关闭主窗 = 最小化到托盘；托盘菜单「退出」才真正退出。
全局热键：Alt+F（可在 config 改）。注册失败不致命，仅记录。
"""
from __future__ import annotations

import ctypes
import logging
import multiprocessing
import queue
import sys
import threading

from PySide6.QtCore import QAbstractNativeEventFilter, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .config import (
    enabled_index_exts,
    get_autostart,
    get_document_search_enabled,
    get_hotkey,
    get_smart_grouping_enabled,
    get_version_management_enabled,
    set_completed_index_feature_signature,
    set_version_management_enabled,
)
from .ui.main_window import MainWindow
from .ui.version_bridge import VersionBridge
from .versioning import autostart
from .versioning.manager import VersionManager
from .versioning.watcher import VaultWatcher, default_watch_paths

WM_HOTKEY = 0x0312
_MODS = {"CTRL": 0x0002, "CONTROL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004, "WIN": 0x0008}
HOTKEY_ID = 1


def _sync_autostart_preference() -> bool:
    desired = get_autostart()
    try:
        # A versioned green-install folder moves on every release.  Merely
        # checking whether the Startup shortcut exists leaves it pointing at
        # a deleted old executable after upgrades.  Rewriting an enabled link
        # is cheap and makes the configured target self-healing.
        if desired:
            return autostart.set_enabled(True)
        # Remove the link by filename even when its target points at a stale
        # versioned install directory. ``is_enabled`` intentionally reports
        # target mismatches as false, which is not the same as "no link".
        return autostart.set_enabled(False)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("failed to sync autostart preference", exc_info=True)
        return False


def _start_autostart_sync() -> threading.Thread:
    """Repair the Startup shortcut without delaying the first visible frame."""

    def run() -> None:
        pythoncom = None
        initialized = False
        try:
            import pythoncom as _pythoncom  # type: ignore

            pythoncom = _pythoncom
            pythoncom.CoInitialize()
            initialized = True
        except Exception:  # noqa: BLE001 WScript may still work without pywin32 COM init
            pass
        try:
            _sync_autostart_preference()
        finally:
            if initialized and pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    thread = threading.Thread(
        target=run,
        name="PPTDoctorAutostartSync",
        daemon=True,
    )
    thread.start()
    return thread


def _parse_hotkey(spec: str) -> tuple[int, int | None]:
    mods, vk = 0, None
    for part in spec.upper().split("+"):
        part = part.strip()
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1:
            vk = ord(part)
    return mods, vk


def _make_icon() -> QIcon:
    from .config import resource_path
    logo = resource_path("assets", "logo.png")
    if logo.exists():
        ic = QIcon(str(logo))
        if not ic.isNull():
            return ic
    # 回退：资源缺失时画一个蓝底 P
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#0A84FF"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Arial", 30, QFont.Bold))
    painter.drawText(pm.rect(), Qt.AlignCenter, "P")
    painter.end()
    return QIcon(pm)


def _open_version_window(owner, version_mgr, *, window_cls=None):
    windows = getattr(owner, "_version_windows", None)
    if windows is None:
        windows = []
        owner._version_windows = windows

    for window in list(windows):
        try:
            if not _qt_is_valid(window) or getattr(window, "_closing", False) or not window.isVisible():
                windows.remove(window)
                continue
            window.raise_()
            window.activateWindow()
            return window
        except RuntimeError:
            if window in windows:
                windows.remove(window)

    if window_cls is None:
        from .ui.version_window import VersionWindow
        window_cls = VersionWindow
    window = window_cls(version_mgr)
    try:
        window._closing_owner = owner
    except Exception:  # noqa: BLE001
        pass
    bg_tasks = getattr(owner, "_bg_tasks", None)
    if isinstance(bg_tasks, list):
        try:
            window._parent_bg_tasks = bg_tasks
        except Exception:  # noqa: BLE001
            pass
    windows.append(window)
    try:
        window.destroyed.connect(
            lambda _=None, w=window: windows.remove(w) if w in windows else None)
    except AttributeError:
        pass
    window.show()
    window.raise_()
    window.activateWindow()
    return window


def _open_settings_dialog(owner, version_mgr, *, dialog_cls=None):
    if dialog_cls is None:
        from .ui.settings_dialog import SettingsDialog
        dialog_cls = SettingsDialog
    kwargs = {"on_rescan": getattr(owner, "_request_full_rescan", None)}
    feature_cb = getattr(owner, "_feature_change_cb", None)
    if callable(feature_cb):
        kwargs["on_feature_change"] = feature_cb
    dialog = dialog_cls(version_mgr, owner, **kwargs)
    owner._settings_dialog = dialog
    try:
        return dialog.exec()
    finally:
        if getattr(owner, "_settings_dialog", None) is dialog:
            owner._settings_dialog = None


class _LazyVersionManager:
    """Do not even open/migrate versions.db for the 90% basic-mode path."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._instance = None
        self._lock = threading.Lock()

    def _get(self):
        instance = self._instance
        if instance is not None:
            return instance
        with self._lock:
            if self._instance is None:
                self._instance = self._factory()
            return self._instance

    def is_initialized(self) -> bool:
        return self._instance is not None

    def supports(self, name: str) -> bool:
        """Advertise capabilities without constructing the heavy backend."""
        return not str(name).startswith("_") and callable(
            getattr(VersionManager, str(name), None)
        )

    def stop(self) -> None:
        # Shutdown must not turn an unused optional feature into startup work.
        instance = self._instance
        if instance is not None:
            instance.stop()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._get(), name)


class _FeatureRuntime:
    """Own the one filesystem watcher and serialize version-service transitions.

    Search freshness must not depend on version management. The shared watcher
    always sends enabled file types to the live indexer; only the PPT snapshot
    branch is switched on for advanced users.
    """

    _STOP = object()

    def __init__(self, win, manager, bridge) -> None:
        self._win = win
        self._manager = manager
        self._bridge = bridge
        self.version_enabled = get_version_management_enabled()
        self.document_enabled = get_document_search_enabled()
        self.smart_grouping_enabled = get_smart_grouping_enabled()
        self._watcher = None
        self._transition_q: queue.Queue = queue.Queue()
        self._transition_thread = threading.Thread(
            target=self._version_transition_loop,
            name="PPTDoctorFeatureRuntime",
            daemon=True,
        )
        self._lifecycle_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._started = False
        self._watcher_error = ""
        self._version_error = ""
        self._version_running = False

    def allowed_exts(self) -> tuple[str, ...]:
        return enabled_index_exts(self.document_enabled)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._stop_requested.is_set():
                return
            self._started = True
            self._transition_thread.start()
        try:
            watcher = VaultWatcher(
                default_watch_paths(),
                self._on_ppt_saved,
                self._on_moved,
                self._on_content_saved,
                self._on_removed,
                allowed_exts=self.allowed_exts,
            )
            watcher.start()
        except Exception as exc:  # noqa: BLE001 optional live freshness must not crash app
            self._watcher_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).error(
                "filesystem watcher failed to start: %s", exc, exc_info=True,
            )
            self._report_runtime_error(
                "实时监听启动失败；搜索仍可用，稍后会靠后台扫描补齐"
            )
            if self.version_enabled and not self._stop_requested.is_set():
                # Reconcile can still protect already-known PPTs even when the
                # low-latency watcher is unavailable.
                self._transition_q.put(True)
            return
        with self._lifecycle_lock:
            stop_late_start = self._stop_requested.is_set()
            if not stop_late_start:
                self._watcher = watcher
        if stop_late_start:
            # start() runs off the GUI thread. A fast user exit can arrive while
            # watchdog is still constructing; never let that late winner leak.
            watcher.stop()
            return
        if self.version_enabled and not self._stop_requested.is_set():
            self._transition_q.put(True)

    def diagnostic_lines(self) -> list[str]:
        with self._lifecycle_lock:
            watcher_alive = self._watcher is not None
            started = self._started
        return [
            "feature_runtime: "
            f"started={started} stopping={self._stop_requested.is_set()} "
            f"watcher_alive={watcher_alive} "
            f"version={self.version_enabled} documents={self.document_enabled} "
            f"smart_grouping={self.smart_grouping_enabled} "
            f"version_running={self._version_running} "
            f"watcher_error={self._watcher_error or '-'} "
            f"version_error={self._version_error or '-'}"
        ]

    def _report_runtime_error(self, message: str) -> None:
        emit = getattr(self._bridge, "emit_runtime_error", None)
        if callable(emit):
            emit(message)

    def _on_ppt_saved(self, path: str) -> None:
        if not self.version_enabled:
            self._bridge.emit_content_changed(path)
            return
        try:
            version_id = self._manager.snapshot_now(path)
        except Exception:  # noqa: BLE001 watcher owns the bounded retry policy
            logging.getLogger(__name__).warning("version snapshot failed", exc_info=True)
            # Search freshness is independent from version retention, so still
            # enqueue a live re-index.  Re-raise afterwards: ``VaultWatcher``
            # interprets callback failure as a transient save race and retries
            # at 0.75/2/5 seconds.  Swallowing this exception silently lost the
            # historical version while making the watcher believe it succeeded.
            self._bridge.emit_content_changed(path)
            raise
        # A failed snapshot must never make the searchable index stale.
        if not version_id:
            self._bridge.emit_content_changed(path)

    def _on_content_saved(self, path: str) -> None:
        self._bridge.emit_content_changed(path)

    def _on_moved(self, old_path: str, new_path: str) -> None:
        if self.version_enabled:
            try:
                self._manager.move_path(old_path, new_path)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("version identity move failed", exc_info=True)

    def _on_removed(self, path: str) -> None:
        if self.version_enabled:
            try:
                self._manager.mark_deleted(path)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("version delete marker failed", exc_info=True)

    def set_version_enabled(self, enabled: bool) -> None:
        self.version_enabled = bool(enabled)
        self._win.set_version_manager(self._manager if self.version_enabled else None)
        if not self._stop_requested.is_set():
            self._transition_q.put(self.version_enabled)

    def set_document_enabled(self, enabled: bool) -> None:
        self.document_enabled = bool(enabled)

    def set_smart_grouping_enabled(self, enabled: bool) -> None:
        self.smart_grouping_enabled = bool(enabled)

    def _version_transition_loop(self) -> None:
        running = False
        try:
            while True:
                target = self._transition_q.get()
                if target is self._STOP:
                    return
                # Coalesce rapid clicks while preserving the final state.
                while True:
                    try:
                        newer = self._transition_q.get_nowait()
                    except queue.Empty:
                        break
                    if newer is self._STOP:
                        return
                    target = newer
                target = bool(target)
                if target == running:
                    continue
                if target:
                    try:
                        self._manager.start(watch=False)
                    except Exception as exc:  # noqa: BLE001 optional backend must not kill loop
                        self._version_error = f"{type(exc).__name__}: {exc}"
                        # start() may fail after creating one maintenance thread
                        # or opening the repository.  Clean that partial runtime
                        # here while still on the lifecycle-owner thread.
                        try:
                            self._manager.stop()
                        except Exception:  # noqa: BLE001 preserve original failure
                            logging.getLogger(__name__).warning(
                                "partial version backend cleanup failed",
                                exc_info=True,
                            )
                        # Do not leave a decorative "enabled" switch behind when
                        # the backend never started.  Otherwise every subsequent
                        # save repeatedly attempts snapshots/retries against the
                        # same broken repository while the UI falsely promises
                        # version protection.
                        self.version_enabled = False
                        set_version_management_enabled(False)
                        emit_state = getattr(self._bridge, "emit_feature_state", None)
                        if callable(emit_state):
                            emit_state("version_management", False)
                        logging.getLogger(__name__).error(
                            "version backend failed to start: %s", exc, exc_info=True,
                        )
                        self._report_runtime_error(
                            "版本管理启动失败；已停止版本保护，请到设置的健康诊断查看原因"
                        )
                        continue
                    self._version_error = ""
                    running = True
                    self._version_running = True
                else:
                    try:
                        self._manager.stop()
                    except Exception as exc:  # noqa: BLE001
                        self._version_error = f"{type(exc).__name__}: {exc}"
                        logging.getLogger(__name__).error(
                            "version backend failed to stop: %s", exc, exc_info=True,
                        )
                        self._report_runtime_error(
                            "版本管理停止异常；退出程序会再次清理，请查看健康诊断"
                        )
                        continue
                    running = False
                    self._version_running = False
        finally:
            if running:
                try:
                    self._manager.stop()
                except Exception:  # noqa: BLE001 shutdown stays best-effort
                    logging.getLogger(__name__).warning(
                        "version backend final stop failed", exc_info=True,
                    )
            self._version_running = False

    def stop(self) -> None:
        self._stop_requested.set()
        with self._lifecycle_lock:
            watcher = self._watcher
            self._watcher = None
            started = self._started
        if watcher is not None:
            watcher.stop()
        fallback_stop = not started
        if started:
            self._transition_q.put(self._STOP)
            if threading.current_thread() is not self._transition_thread:
                self._transition_thread.join(timeout=3)
                # The transition loop owns normal manager lifecycle.  Only use
                # a direct stop as a bounded-shutdown fallback when that owner is
                # genuinely stuck; routine double-stop can race backend cleanup.
                fallback_stop = self._transition_thread.is_alive()
        if fallback_stop:
            try:
                self._manager.stop()
            except Exception:  # noqa: BLE001 application exit must continue
                logging.getLogger(__name__).warning(
                    "version backend stop failed during app shutdown", exc_info=True,
                )


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, hotkey_id: int, callback):
        super().__init__()
        self._id = hotkey_id
        self._cb = callback

    def nativeEventFilter(self, etype, message):  # noqa: N802
        try:
            if etype in (b"windows_generic_MSG", "windows_generic_MSG"):
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == self._id:
                    self._cb()
        except Exception:  # noqa: BLE001 事件过滤器绝不能抛
            pass
        return False, 0


SINGLETON_NAME = "pptx-finder-singleton-v1"


def _show_window(win: MainWindow) -> None:
    win.showNormal()
    win.raise_()
    win.activateWindow()


def _toggle_window(win: MainWindow) -> None:
    if win.isVisible() and not win.isMinimized() and win.isActiveWindow():
        win.hide()
    else:
        win.showNormal()
        win.raise_()
        win.activateWindow()
        win.search_box.setFocus()
        win.search_box.selectAll()


def _apply_global_hotkey(app, win, spec: str) -> bool:
    """(重新)注册全局唤起热键：先注销旧的、移除旧 filter，再注册新的，并更新状态栏标签。
    可被设置页反复调用做热重绑。返回是否注册成功。失败不致命（仅状态栏标黄提示）。"""
    log = logging.getLogger(__name__)
    try:
        ctypes.windll.user32.UnregisterHotKey(int(win.winId()), HOTKEY_ID)
    except Exception:  # noqa: BLE001 没注册过 / 非 Windows
        pass
    old = getattr(app, "_hotkey_filter", None)
    if old is not None:
        try:
            app.removeNativeEventFilter(old)
        except Exception:  # noqa: BLE001
            pass
        app._hotkey_filter = None
    ok = False
    try:
        mods, vk = _parse_hotkey(spec or "")
        if vk is not None and mods:
            if ctypes.windll.user32.RegisterHotKey(int(win.winId()), HOTKEY_ID, mods, vk):
                filt = _HotkeyFilter(HOTKEY_ID, lambda: _toggle_window(win))
                app.installNativeEventFilter(filt)
                app._hotkey_filter = filt  # 防 GC
                ok = True
            else:
                log.warning("RegisterHotKey failed (maybe taken): %s", spec)
    except Exception as e:  # noqa: BLE001
        log.warning("hotkey setup error: %s", e)
    try:
        win.set_hotkey_status(spec, ok)
    except Exception:  # noqa: BLE001
        pass
    return ok


def main() -> int:
    multiprocessing.freeze_support()  # PyInstaller 下多进程必需
    try:  # 任务栏用窗口图标(吉祥物)而非默认 python/exe 图标，需显式 AppUserModelID
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PPTDoctor")
    except Exception:  # noqa: BLE001 非 Windows / 旧系统静默跳过
        pass
    from .logging_setup import configure_logging
    configure_logging()
    log = logging.getLogger(__name__)
    app = QApplication(sys.argv)

    # 单实例：已有实例在跑则通知其显示窗口并退出本实例（防重复全盘索引、数据库抢锁）
    probe = QLocalSocket()
    probe.connectToServer(SINGLETON_NAME)
    if probe.waitForConnected(200):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        log.info("another instance already running; activated it, exiting")
        return 0
    QLocalServer.removeServer(SINGLETON_NAME)
    singleton_server = QLocalServer()
    singleton_server.listen(SINGLETON_NAME)

    app.setQuitOnLastWindowClosed(False)
    icon = _make_icon()
    app.setWindowIcon(icon)

    win = MainWindow(do_index=True)
    win._to_tray_on_close = True

    # 基础模式只保留一个轻量文件监听器维持 PPT 索引新鲜；版本对账/维护仅在
    # 用户主动开启高阶功能后启动。
    bridge = VersionBridge()
    app._version_bridge = bridge  # 防 GC
    version_mgr = _LazyVersionManager(
        lambda: VersionManager(
            on_snapshot=bridge.emit_snapshot,
            on_content_saved=bridge.emit_content_changed,
        )
    )
    app._version_manager = version_mgr  # 防 GC
    bridge.snapshotted.connect(win.on_version_snapshot)
    bridge.content_changed.connect(win.on_content_changed)
    bridge.runtime_error.connect(win._toast)
    feature_runtime = _FeatureRuntime(win, version_mgr, bridge)
    app._feature_runtime = feature_runtime
    win._feature_runtime = feature_runtime
    win._version_backend = version_mgr
    win.set_version_manager(version_mgr if feature_runtime.version_enabled else None)
    def _on_singleton_conn() -> None:
        sock = singleton_server.nextPendingConnection()
        if sock is not None:
            sock.readyRead.connect(lambda: _show_window(win))
    singleton_server.newConnection.connect(_on_singleton_conn)
    app._singleton_server = singleton_server  # 防 GC

    # 托盘
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("PPT Doctor · PPT 查询助手")
    menu = QMenu()
    act_show = QAction("显示主窗口", app)
    act_show.triggered.connect(lambda: _toggle_window(win))

    win._version_windows = []  # 防 GC 已打开的版本窗口

    def _open_version_mgr() -> None:
        if feature_runtime.version_enabled:
            _open_version_window(win, version_mgr)

    def _open_settings() -> None:
        _open_settings_dialog(win, version_mgr)

    act_versions = QAction("版本管理…", app)
    act_versions.triggered.connect(_open_version_mgr)
    act_versions.setEnabled(feature_runtime.version_enabled)
    act_settings = QAction("设置…", app)
    act_settings.triggered.connect(_open_settings)
    act_rescan = QAction("重新扫描全盘", app)
    act_rescan.triggered.connect(lambda: win._start_indexing(None, None))
    act_quit = QAction("退出", app)

    def _request_feature_rescan() -> None:
        if not win._start_indexing(None, None):
            win._schedule_full_coverage_scan(None, "feature_change")

    def _on_feature_change(key: str, enabled: bool) -> None:
        dialog = getattr(win, "_settings_dialog", None)
        apply_runtime_state = getattr(dialog, "apply_runtime_feature_state", None)
        if callable(apply_runtime_state):
            apply_runtime_state(key, enabled)
        if key == "version_management":
            feature_runtime.set_version_enabled(enabled)
            act_versions.setEnabled(enabled)
            win._open_version_cb = (
                (lambda: _open_version_window(win, version_mgr)) if enabled else None
            )
            return
        if key == "document_search":
            feature_runtime.set_document_enabled(enabled)
            win.apply_feature_flags(document_search_enabled=enabled)
            if enabled:
                _request_feature_rescan()
            else:
                set_completed_index_feature_signature(
                    win._current_index_feature_signature()
                )
            return
        if key == "smart_grouping":
            feature_runtime.set_smart_grouping_enabled(enabled)
            win.apply_feature_flags(smart_grouping_enabled=enabled)
            if enabled:
                _request_feature_rescan()
            else:
                set_completed_index_feature_signature(
                    win._current_index_feature_signature()
                )

    win._feature_change_cb = _on_feature_change
    bridge.feature_state.connect(_on_feature_change)
    # Connect rollback/status delivery before optional services can fail.  A
    # startup failure must not race ahead and leave the tray action enabled.
    threading.Thread(
        target=feature_runtime.start,
        name="PPTDoctorWatcherStart",
        daemon=True,
    ).start()

    def _real_quit() -> None:
        win._to_tray_on_close = False
        win._shutdown()
        tray.hide()
        app.quit()

    act_quit.triggered.connect(_real_quit)
    menu.addAction(act_show)
    menu.addSeparator()
    menu.addAction(act_rescan)
    menu.addAction(act_versions)
    menu.addAction(act_settings)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: _toggle_window(win)
        if reason == QSystemTrayIcon.Trigger else None
    )
    tray.show()

    win.show()
    app._autostart_sync_thread = _start_autostart_sync()
    win.maybe_show_welcome()  # 首次运行弹欢迎引导

    # 全局热键（可在设置里改；当前值持久化在 config.ui.json，默认 GLOBAL_HOTKEY）
    win._open_settings_cb = _open_settings          # 状态栏热键标签点击 → 打开设置
    win._open_version_cb = (
        (lambda: _open_version_window(win, version_mgr))
        if feature_runtime.version_enabled else None
    )  # 搜索结果 → 版本历史（D3/D6）
    win._apply_hotkey = lambda spec: _apply_global_hotkey(app, win, spec)  # 设置页热重绑
    _apply_global_hotkey(app, win, get_hotkey())

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
