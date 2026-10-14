# -*- coding: utf-8 -*-
r"""v2.0.2：主窗口「哪里都拖不动」与默认尺寸比例。

拖不动的根因（v2.0.0/2.0.1 实际发生）：截图快捷键为了收 WM_HOTKEY，在主窗口下面挂了个
隐形 Tool 窗口并取 winId()；Qt 随即把主窗口的中央区、状态栏也变成原生子窗口。鼠标按下时
Windows 把 WM_NCHITTEST 发给鼠标下面那个原生子窗口——它一律回「普通内容区」，主窗口自己
的拖动判定根本问不到。v2.0 的拖动测试是把消息直接发给主窗口，绕开了这层遮挡，所以没发现。

这里按 Windows 的真实路径测：先找「这个点落在哪个原生窗口上」，再问那个窗口。
"""
from __future__ import annotations

import math
import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect                          # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from pptx_finder import config                                    # noqa: E402
from pptx_finder.ui import window_drag                            # noqa: E402
from pptx_finder.ui.main_window import MainWindow                 # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("PPTX_FINDER_ROOTS", str(tmp_path / "d"))
    monkeypatch.setenv("PPTX_FINDER_SINGLETON_NAME", "dragtest-v202")
    w = MainWindow(do_index=False)
    qtbot.addWidget(w)
    w.resize(1200, 760)
    w.show()
    qtbot.waitExposed(w)
    return w


def _install_shortcuts(win, tmp_path):
    from pptx_finder.screenshots.preferences import CapturePreferences
    from pptx_finder.ui.screenshot_shortcuts import ScreenshotShortcuts
    prefs = CapturePreferences(tmp_path / "shot-prefs.json")
    return ScreenshotShortcuts(QApplication.instance(), win, preferences=prefs,
                               register=lambda *a: True, unregister=lambda *a: None)


def test_hotkey_receiver_does_not_turn_main_window_children_native(win, qtbot, tmp_path):
    shortcuts = _install_shortcuts(win, tmp_path)
    try:
        qtbot.wait(50)
        assert shortcuts._window.parentWidget() is None
        assert window_drag.native_children(win) == []
    finally:
        shortcuts.close()


def test_native_child_elsewhere_does_not_drag_siblings_along(win, qtbot):
    """兜底：哪怕以后又有控件（录制控制条、第三方控件）申请原生窗口，兄弟控件也不跟着变。"""
    from PySide6.QtWidgets import QLabel
    probe = QLabel("probe", win)
    probe.winId()
    qtbot.wait(20)
    assert window_drag.native_children(win) == [probe]
    probe.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 命中测试")
def test_real_hit_path_reaches_main_window_on_blank_title_area(win, qtbot, tmp_path):
    """按 Windows 的真实路径：点 → 落在哪个原生窗口 → 问它命中码。顶栏空白处必须是 HTCAPTION。"""
    import ctypes
    from ctypes import wintypes
    shortcuts = _install_shortcuts(win, tmp_path)
    try:
        qtbot.wait(50)
        u = ctypes.WinDLL("user32")
        u.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
        u.ChildWindowFromPointEx.restype = wintypes.HWND
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        top = int(win.winId())
        dpr = win.devicePixelRatioF() or 1.0
        captions = 0
        for y in range(8, win._title_h, 8):
            for x in range(40, win.width() - 40, 24):
                pt = wintypes.POINT(int(x * dpr), int(y * dpr))
                target = u.ChildWindowFromPointEx(top, pt, 0x0001)   # CWP_SKIPINVISIBLE
                assert (target or top) == top, f"({x},{y}) 落在原生子窗口 {target} 上，命中测试被截走"
                scr = wintypes.POINT(pt.x, pt.y)
                u.ClientToScreen(top, ctypes.byref(scr))
                lp = ((scr.y & 0xFFFF) << 16) | (scr.x & 0xFFFF)
                if u.SendMessageW(top, 0x0084, 0, lp) == window_drag.HTCAPTION:
                    captions += 1
        assert captions > 0, "顶栏没有一处可以按住拖动"
    finally:
        shortcuts.close()


# ---- 默认尺寸：和屏幕同比例、面积约 80% ----

@pytest.mark.parametrize("screen,avail", [
    (QRect(0, 0, 1707, 960), QRect(0, 0, 1707, 912)),     # 3840x2160 @225%
    (QRect(0, 0, 1280, 720), QRect(0, 0, 1280, 672)),     # 1920x1080 @150%
    (QRect(0, 0, 1920, 1200), QRect(0, 0, 1920, 1152)),   # 16:10
    (QRect(1707, 0, 2560, 1440), QRect(1707, 0, 2560, 1392)),  # 副屏
])
def test_default_size_matches_screen_ratio_and_80_percent_area(screen, avail):
    got = MainWindow._default_rect(screen, avail)
    ratio = screen.width() / screen.height()
    assert abs(got.width() / got.height() - ratio) < 0.01
    area = got.width() * got.height() / (screen.width() * screen.height())
    assert 0.78 <= area <= 0.81
    assert avail.contains(got)
    assert abs(got.center().x() - avail.center().x()) <= 1


def test_old_saved_geometry_is_replaced_once_by_new_default(win, monkeypatch):
    """老版本存的尺寸（比例怪）升级后按新默认重开一次；新格式的记录照常沿用。"""
    config.update_ui_settings(window_geometry={"x": 0, "y": 0, "w": 1097, "h": 569, "maximized": False})
    win._restore_window_geometry()
    screen = win.screen()
    expect = MainWindow._default_rect(screen.geometry(), screen.availableGeometry())
    assert (win.width(), win.height()) == (expect.width(), expect.height())

    config.set_window_geometry(30, 40, 1000, 640)
    win._restore_window_geometry()
    assert (win.width(), win.height()) == (1000, 640)
