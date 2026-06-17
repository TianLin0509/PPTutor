"""打开文件 / 打开所在文件夹（并选中）/ 打开并跳到指定页。"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)


def open_file(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows only
        return True
    except OSError:
        return False


def open_folder(path: str) -> bool:
    """在资源管理器中定位文件；文件已不在则退而打开其父目录。"""
    if os.path.exists(path):
        try:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
            return True
        except OSError:
            return False
    parent = os.path.dirname(path)
    if os.path.isdir(parent):
        try:
            os.startfile(parent)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    return False


def open_at_page(path: str, page_no: int) -> tuple[bool, bool]:
    """用用户的 PowerPoint 打开文件并跳到第 page_no 页。
    返回 (是否已打开, 是否成功跳页)；COM 不可用时回退普通打开。"""
    if not os.path.exists(path):
        return (False, False)
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("PowerPoint.Application")
        app.Visible = True
        pres = app.Presentations.Open(
            os.path.abspath(path), ReadOnly=False, WithWindow=True
        )
        jumped = False
        try:
            if 1 <= page_no <= int(pres.Slides.Count):
                app.ActiveWindow.View.GotoSlide(page_no)
                jumped = True
        except Exception as e:  # noqa: BLE001 跳页失败不影响已打开
            log.debug("GotoSlide failed page=%s: %s", page_no, e)
        return (True, jumped)
    except Exception as e:  # noqa: BLE001 COM 不可用 → 普通打开
        log.warning("open_at_page COM failed, fallback to open_file: %s", e)
        return (open_file(path), False)
