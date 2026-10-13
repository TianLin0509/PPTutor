"""子窗口共用的文件动作：优先交给主窗口（后台执行、带提示），没有主窗口时自己后台兜底。

子窗口（版本、体检、素材、转字、截图、瘦身、报告）里的「打开」「打开所在文件夹」
「复制路径」原来各写各的，有的直接在界面线程调资源管理器——网络盘上会卡住界面。
这里统一：主窗口有 `_open_file_path` / `_open_folder_path` 就用它们（后台线程 + toast），
否则开一个守护线程跑 actions.open_file / open_folder。
"""
from __future__ import annotations

import threading

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget


def find_owner(widget) -> object | None:
    """沿父链找到带 `_open_file_path` 的主窗口。"""
    w = widget
    seen = 0
    while w is not None and seen < 20:
        if callable(getattr(w, "_open_file_path", None)):
            return w
        nxt = getattr(w, "_closing_owner", None) or getattr(w, "_owner", None)
        try:
            parent = w.parentWidget() if isinstance(w, QWidget) else None
        except RuntimeError:
            parent = None
        w = nxt if nxt is not None and nxt is not w else parent
        seen += 1
    return None


def _background(fn, path: str) -> None:
    threading.Thread(target=lambda: fn(path), daemon=True, name="file-action").start()


def open_file(widget, path: str) -> None:
    if not path:
        return
    owner = find_owner(widget)
    if owner is not None:
        owner._open_file_path(path)
        return
    from .. import actions
    _background(actions.open_file, path)


def open_folder(widget, path: str) -> None:
    if not path:
        return
    owner = find_owner(widget)
    fn = getattr(owner, "_open_folder_path", None) if owner is not None else None
    if callable(fn):
        fn(path)
        return
    from .. import actions
    _background(actions.open_folder, path)


def preview_in_search(widget, path: str) -> bool:
    owner = find_owner(widget)
    fn = getattr(owner, "preview_path_in_search", None) if owner is not None else None
    if callable(fn):
        fn(path)
        try:
            owner.raise_()
            owner.activateWindow()
        except (AttributeError, RuntimeError):
            pass
        return True
    return False


def notify(widget, text: str) -> None:
    owner = find_owner(widget)
    fn = getattr(owner, "_toast", None) if owner is not None else None
    if callable(fn):
        fn(text)


def copy_text(widget, text: str, message: str = "已复制完整路径") -> None:
    QApplication.clipboard().setText(text)
    notify(widget, message)


def popup(menu, global_pos) -> None:
    """弹出右键菜单。单独成函数：测试里替换它即可，不必真的进入菜单的模态循环。"""
    menu.exec(global_pos)


def ask(parent, title: str, text: str, yes: str = "确定", no: str = "取消",
        *, default_yes: bool = False, icon=QMessageBox.Question) -> bool:
    """中文按钮的确认框（不依赖 Qt 翻译文件）。"""
    box = QMessageBox(icon, title, text, QMessageBox.NoButton, parent)
    yes_btn = box.addButton(yes, QMessageBox.AcceptRole)
    no_btn = box.addButton(no, QMessageBox.RejectRole)
    box.setDefaultButton(yes_btn if default_yes else no_btn)
    box.setEscapeButton(no_btn)
    box.exec()
    return box.clickedButton() is yes_btn
