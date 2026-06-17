"""应用入口：托盘常驻 + 全局热键唤起 + 主窗口。

形态：QSystemTrayIcon 常驻；关闭主窗 = 最小化到托盘；托盘菜单「退出」才真正退出。
全局热键：Ctrl+Alt+P（可在 config 改）。注册失败不致命，仅记录。
"""
from __future__ import annotations

import ctypes
import logging
import multiprocessing
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .config import GLOBAL_HOTKEY
from .ui.main_window import MainWindow

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
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#0071e3"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    p.setFont(QFont("Arial", 30, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, "P")
    p.end()
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
    app.setQuitOnLastWindowClosed(False)
    icon = _make_icon()
    app.setWindowIcon(icon)

    win = MainWindow(do_index=True)
    win._to_tray_on_close = True

    # 托盘
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("pptx-finder · PPTX 查询助手")
    menu = QMenu()
    act_show = QAction("显示主窗口", app)
    act_show.triggered.connect(lambda: _toggle_window(win))
    act_quit = QAction("退出", app)

    def _real_quit() -> None:
        win._to_tray_on_close = False
        win._shutdown()
        tray.hide()
        app.quit()

    act_quit.triggered.connect(_real_quit)
    menu.addAction(act_show)
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
                win.status_label.setText(f"全局热键 {GLOBAL_HOTKEY} 注册失败（可能被其他程序占用）")
    except Exception as e:  # noqa: BLE001
        log.warning("hotkey setup error: %s", e)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
