"""无边框主窗口的「按住空白处拖动」与边缘缩放判定（v2.0 重写）。

旧实现两处毛病，合起来就是用户说的「经常拖不动、界面发硬」：

1. 命中判定读的是 ``QCursor.pos()``（此刻光标在哪），而不是 Windows 随
   WM_NCHITTEST 给出的坐标。远程桌面、触屏/触控板、高负载下光标位置还没同步，
   或别的程序发来的命中查询，都会被判成「普通内容区」→ 按下去窗口不动。
   隐藏桌面实测：整条标题栏 100% 被判成内容区。
2. 能拖的只有左上角品牌文字那一小段；标题栏其余全是搜索框和下拉，侧栏、
   状态栏、面板留白都不能拖。

现在：坐标取自消息本身（物理像素 → 逻辑像素按窗口自己的缩放比换算）；
「空白处」的判定看光标下面那个控件是不是交互控件，侧栏、顶栏、状态栏、
面板留白都可以按住拖动，双击空白处最大化/还原，贴边 Snap 由系统接管。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QAbstractItemView, QAbstractSlider, QAbstractSpinBox,
    QComboBox, QGraphicsView, QLabel, QLineEdit, QMenuBar, QPlainTextEdit,
    QSizeGrip, QSplitterHandle, QTabBar, QTextEdit, QWidget,
)

HTCLIENT = 1
HTCAPTION = 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

#: 缩放边宽度（逻辑像素）。Windows 自带窗口约 8 物理像素，这里取 6 逻辑像素。
RESIZE_BORDER = 6

#: 设了这个动态属性为真的控件（含其子控件）不参与窗口拖动——给那些点击逻辑写在
#: eventFilter 里、从类型上看不出来的控件用（例如预览图：双击切缩放、滚轮翻页）。
NO_DRAG_PROP = "noWindowDrag"

_INTERACTIVE_TYPES = (
    QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractSlider,
    QAbstractItemView, QTextEdit, QPlainTextEdit, QGraphicsView, QTabBar,
    QSplitterHandle, QSizeGrip, QMenuBar,
)

_MOUSE_HANDLERS = ("mousePressEvent", "mouseReleaseEvent", "mouseDoubleClickEvent")


def forbid_native_siblings() -> None:
    """一个子控件变成原生窗口时，不许 Qt 顺手把它的兄弟控件也变成原生窗口。

    无边框主窗口的拖动靠 WM_NCHITTEST 回 HTCAPTION，而这个消息只发给鼠标下面那个
    原生窗口。一旦中央区、状态栏被连带变成原生子窗口，它们就盖住整个主窗口、一律回
    「普通内容区」——哪里都拖不动（v2.0.0/2.0.1 实际发生过，见 PITFALLS 2026-10-14）。
    """
    QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, True)


def native_children(top: QWidget) -> list[QWidget]:
    """top 里已经成了原生窗口的非顶层子控件（应当为空；自检与测试用）。"""
    return [w for w in top.findChildren(QWidget) if not w.isWindow() and w.internalWinId()]


def _defines_mouse_handler(w: QWidget) -> bool:
    """Python 子类自己重写了鼠标处理（或实例上直接挂了处理函数）= 这是个可点的控件。"""
    for name in _MOUSE_HANDLERS:
        try:
            if name in vars(w):
                return True
        except TypeError:
            pass
    for cls in type(w).__mro__:
        mod = getattr(cls, "__module__", "") or ""
        if mod.startswith(("PySide6", "shiboken6", "builtins")):
            break
        if any(name in cls.__dict__ for name in _MOUSE_HANDLERS):
            return True
    return False


def is_interactive(w: QWidget | None, top: QWidget) -> bool:
    """从光标下的控件一路往上看到顶层：任何一层是交互控件就不拖。"""
    while w is not None and w is not top:
        if w.property(NO_DRAG_PROP):
            return True
        if isinstance(w, _INTERACTIVE_TYPES):
            return True
        if w.testAttribute(Qt.WA_TransparentForMouseEvents):
            w = w.parentWidget()
            continue
        if w.testAttribute(Qt.WA_SetCursor) and w.cursor().shape() != Qt.ArrowCursor:
            # 可点的标签会设成小手（状态栏热键、无权限提示等），可选文字是 I 形
            return True
        if isinstance(w, QLabel):
            # QLabel 默认就带 LinksAccessibleByMouse：只有文字里真有链接才算可点
            if w.textInteractionFlags() & Qt.TextSelectableByMouse:
                return True
            if "<a " in w.text().lower():
                return True
        if w.toolTip():
            # 有说明的控件要能悬停出提示；拖动区只留真正的空白
            return True
        if _defines_mouse_handler(w):
            return True
        w = w.parentWidget()
    return False


_user32 = None


def _screen_to_client():
    """独立的 user32 句柄 + 显式参数类型：HWND 是指针宽度，默认按 C int 传会在
    句柄值较大时抛 OverflowError——而 nativeEvent 里的异常会直接把进程带崩。"""
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32")
        _user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        _user32.ScreenToClient.restype = wintypes.BOOL
    return _user32.ScreenToClient


def _logical_client_pos(win: QWidget, lparam: int) -> QPoint | None:
    """把 WM_NCHITTEST 的 lParam（屏幕物理像素）换算成窗口内逻辑坐标。"""
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    pt = wintypes.POINT(x, y)
    try:
        if not _screen_to_client()(wintypes.HWND(int(win.winId())), ctypes.byref(pt)):
            return None
    except Exception:  # noqa: BLE001
        return None
    dpr = win.devicePixelRatioF() or 1.0
    return QPoint(int(pt.x / dpr), int(pt.y / dpr))


def hit_test(win: QWidget, pos: QPoint, *, maximized: bool) -> int:
    """给一个窗口内逻辑坐标，返回 Windows 命中码。纯函数，单测可直接喂坐标。"""
    x, y, w, h, b = pos.x(), pos.y(), win.width(), win.height(), RESIZE_BORDER
    if not (0 <= x < w and 0 <= y < h):
        return HTCLIENT
    if not maximized:
        left, right, top, bottom = x < b, x >= w - b, y < b, y >= h - b
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
    child = win.childAt(pos)
    if child is None:
        return HTCAPTION
    if is_interactive(child, win):
        return HTCLIENT
    return HTCAPTION


def native_hit_test(win: QWidget, message) -> tuple[bool, int] | None:
    """nativeEvent 入口：是 WM_NCHITTEST 就给出命中码，否则 None（交回 Qt）。"""
    if sys.platform != "win32":
        return None
    try:
        msg = wintypes.MSG.from_address(int(message))
        if msg.message != 0x0084:  # WM_NCHITTEST
            return None
        pos = _logical_client_pos(win, int(msg.lParam or 0))
        if pos is None:
            return None
        return True, hit_test(win, pos, maximized=win.isMaximized())
    except Exception:  # noqa: BLE001 nativeEvent 里抛异常会让整个进程崩溃，宁可交回 Qt
        return None
