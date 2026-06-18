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

from .config import GLOBAL_HOTKEY
from .ui.main_window import MainWindow
from .versioning.manager import VersionManager

WM_HOTKEY = 0x0312
_MODS = {"CTRL": 0x0002, "CONTROL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004, "WIN": 0x0008}
HOTKEY_ID = 1


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


def main() -> int:
    multiprocessing.freeze_support()  # PyInstaller 下多进程必需
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

    # 版本管理：后台守护（保存即自动版本 / 离线补记 / 监听），不阻塞启动
    version_mgr = VersionManager()
    app._version_manager = version_mgr  # 防 GC
    threading.Thread(target=version_mgr.start, daemon=True).start()

    def _on_singleton_conn() -> None:
        sock = singleton_server.nextPendingConnection()
        if sock is not None:
            sock.readyRead.connect(lambda: _show_window(win))
    singleton_server.newConnection.connect(_on_singleton_conn)
    app._singleton_server = singleton_server  # 防 GC

    # 托盘
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("pptx-finder · PPTX 查询助手")
    menu = QMenu()
    act_show = QAction("显示主窗口", app)
    act_show.triggered.connect(lambda: _toggle_window(win))

    win._version_windows = []  # 防 GC 已打开的版本窗口

    def _open_version_mgr() -> None:
        from .ui.version_window import VersionWindow
        w = VersionWindow(version_mgr)
        win._version_windows.append(w)
        w.show()
        w.raise_()
        w.activateWindow()

    def _open_settings() -> None:
        from .ui.settings_dialog import SettingsDialog
        SettingsDialog(version_mgr, win).exec()

    act_versions = QAction("版本管理…", app)
    act_versions.triggered.connect(_open_version_mgr)
    act_settings = QAction("设置…", app)
    act_settings.triggered.connect(_open_settings)
    act_quit = QAction("退出", app)

    def _real_quit() -> None:
        win._to_tray_on_close = False
        win._shutdown()
        version_mgr.stop()
        tray.hide()
        app.quit()

    act_quit.triggered.connect(_real_quit)
    menu.addAction(act_show)
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

    # 全局热键
    try:
        mods, vk = _parse_hotkey(GLOBAL_HOTKEY)
        if vk is not None:
            hwnd = int(win.winId())
            if ctypes.windll.user32.RegisterHotKey(hwnd, HOTKEY_ID, mods, vk):
                filt = _HotkeyFilter(HOTKEY_ID, lambda: _toggle_window(win))
                app.installNativeEventFilter(filt)
                app._hotkey_filter = filt  # 防 GC
            else:
                log.warning("RegisterHotKey failed (maybe taken): %s", GLOBAL_HOTKEY)
                win.hotkey_label.setText(f"⚠ 热键 {GLOBAL_HOTKEY} 被占用")
    except Exception as e:  # noqa: BLE001
        log.warning("hotkey setup error: %s", e)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
