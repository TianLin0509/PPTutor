"""应用入口：托盘常驻 + 全局热键唤起 + 主窗口。

形态：QSystemTrayIcon 常驻；关闭主窗 = 最小化到托盘；托盘菜单「退出」才真正退出。
全局热键：Ctrl+Alt+P（可在 config 改）。注册失败不致命，仅记录。
"""
from __future__ import annotations

import ctypes
import logging
import multiprocessing
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

from .config import get_autostart, get_hotkey
from .ui.main_window import MainWindow
from .ui.version_bridge import VersionBridge
from .versioning import autostart
from .versioning.manager import VersionManager

WM_HOTKEY = 0x0312
_MODS = {"CTRL": 0x0002, "CONTROL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004, "WIN": 0x0008}
HOTKEY_ID = 1


def _sync_autostart_preference() -> bool:
    desired = get_autostart()
    try:
        if autostart.is_enabled() == desired:
            return True
        return autostart.set_enabled(desired)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("failed to sync autostart preference", exc_info=True)
        return False


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
    dialog = dialog_cls(
        version_mgr,
        owner,
        on_rescan=getattr(owner, "_request_full_rescan", None),
    )
    return dialog.exec()


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
    _sync_autostart_preference()

    # 版本管理：后台守护（保存即自动版本 / 离线补记 / 监听），不阻塞启动
    # 留版事件经 VersionBridge 跨线程信号送回 UI 主线程（更新盾牌 / 首次告知）
    bridge = VersionBridge()
    app._version_bridge = bridge  # 防 GC
    version_mgr = VersionManager(on_snapshot=bridge.emit_snapshot)
    app._version_manager = version_mgr  # 防 GC
    win._version_mgr = version_mgr  # 详情面板版本时间线数据源
    bridge.snapshotted.connect(win.on_version_snapshot)
    win.refresh_version_shield()  # 启动即显示已守护文件数
    threading.Thread(target=version_mgr.start, daemon=True).start()

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
        _open_version_window(win, version_mgr)

    def _open_settings() -> None:
        _open_settings_dialog(win, version_mgr)

    act_versions = QAction("版本管理…", app)
    act_versions.triggered.connect(_open_version_mgr)
    act_settings = QAction("设置…", app)
    act_settings.triggered.connect(_open_settings)
    act_rescan = QAction("重新扫描全盘", app)
    act_rescan.triggered.connect(lambda: win._start_indexing(None, None))
    act_quit = QAction("退出", app)

    def _real_quit() -> None:
        win._to_tray_on_close = False
        win._shutdown()
        version_mgr.stop()
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
    win.maybe_show_welcome()  # 首次运行弹欢迎引导

    # 全局热键（可在设置里改；当前值持久化在 config.ui.json，默认 GLOBAL_HOTKEY）
    win._open_settings_cb = _open_settings          # 状态栏热键标签点击 → 打开设置
    win._apply_hotkey = lambda spec: _apply_global_hotkey(app, win, spec)  # 设置页热重绑
    _apply_global_hotkey(app, win, get_hotkey())

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
