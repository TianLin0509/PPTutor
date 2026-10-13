"""无边框主窗口的拖动/缩放命中判定（v2.0）。

回归点：旧实现按 QCursor.pos() 判定，光标与消息坐标不一致时（远程桌面、触屏、
别的程序发来的命中查询）整条标题栏都被判成内容区 → 拖不动。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

import pytest
from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from pptx_finder import db
from pptx_finder.ui import window_drag
from pptx_finder.ui.main_window import MainWindow


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def _win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.resize(1200, 760)
    win.show()
    qtbot.waitExposed(win)
    return win


def _lparam_for(win, pos: QPoint) -> int:
    """窗口内逻辑坐标 → WM_NCHITTEST 的 lParam（屏幕物理像素）。"""
    pt = wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(int(win.winId()), ctypes.byref(pt))
    dpr = win.devicePixelRatioF()
    x = pt.x + int(round(pos.x() * dpr))
    y = pt.y + int(round(pos.y() * dpr))
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _native_hit(win, pos: QPoint):
    msg = wintypes.MSG()
    msg.message = 0x84
    msg.lParam = _lparam_for(win, pos)
    return win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))


def test_interactive_classification(qapp):
    top = QWidget()
    lay = QVBoxLayout(top)
    plain = QLabel("PPT Doctor")
    selectable = QLabel("可复制路径")
    from PySide6.QtCore import Qt
    selectable.setTextInteractionFlags(Qt.TextSelectableByMouse)
    btn = QPushButton("按钮")
    container = QWidget()
    for w in (plain, selectable, btn, container):
        lay.addWidget(w)
    no_drag = QWidget()
    no_drag.setProperty(window_drag.NO_DRAG_PROP, True)
    inner = QLabel("inner", no_drag)
    lay.addWidget(no_drag)

    class Clickable(QLabel):
        def mousePressEvent(self, e):  # noqa: N802
            pass

    clickable = Clickable("点我")
    lay.addWidget(clickable)
    assert not window_drag.is_interactive(plain, top)
    assert not window_drag.is_interactive(container, top)
    assert window_drag.is_interactive(selectable, top)
    assert window_drag.is_interactive(btn, top)
    assert window_drag.is_interactive(inner, top)
    assert window_drag.is_interactive(clickable, top)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows hit test")
def test_hit_test_uses_message_coordinates_not_cursor(qtbot, tmp_path, monkeypatch):
    win = _win(qtbot, tmp_path)
    # 光标在别处（远程桌面/触屏时的真实情况）：判定必须只看消息里的坐标
    monkeypatch.setattr(QCursor, "pos", lambda *a: QPoint(-5000, -5000))
    brand = win.findChild(QLabel, "gtName")
    assert brand is not None
    center = brand.mapTo(win, brand.rect().center())
    assert _native_hit(win, center) == (True, window_drag.HTCAPTION)
    box = win.search_box.mapTo(win, win.search_box.rect().center())
    assert _native_hit(win, box) == (True, window_drag.HTCLIENT)
    assert _native_hit(win, QPoint(2, 300)) == (True, window_drag.HTLEFT)
    assert _native_hit(win, QPoint(win.width() - 2, win.height() - 2)) == (
        True, window_drag.HTBOTTOMRIGHT)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows hit test")
def test_blank_areas_beyond_title_bar_are_draggable(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    status = win.status_label
    pos = status.mapTo(win, status.rect().center())
    assert _native_hit(win, pos) == (True, window_drag.HTCAPTION)
    btn = win.rail_settings_btn
    assert _native_hit(win, btn.mapTo(win, btn.rect().center())) == (True, window_drag.HTCLIENT)
    hot = win.hotkey_label
    assert _native_hit(win, hot.mapTo(win, hot.rect().center())) == (True, window_drag.HTCLIENT)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows hit test")
def test_maximized_window_has_no_resize_border(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    assert window_drag.hit_test(win, QPoint(1, 300), maximized=True) != window_drag.HTLEFT
    assert window_drag.hit_test(win, QPoint(1, 300), maximized=False) == window_drag.HTLEFT
