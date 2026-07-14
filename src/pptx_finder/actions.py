"""打开文件 / 打开所在文件夹（并选中）/ 打开并跳到指定页。"""
from __future__ import annotations

import logging
import os
import subprocess
import time

from .config import PPT_EXTS

log = logging.getLogger(__name__)

_OPEN_ATTACH_TIMEOUT_SEC = 4.0
_OPEN_ATTACH_POLL_SEC = 0.08


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


def _normalized_path(path: object) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
    except (OSError, TypeError, ValueError):
        return ""


def _com_item(collection, index: int):
    """Read a one-based Office COM collection without depending on one wrapper style."""
    try:
        return collection(index)
    except (TypeError, AttributeError):
        return collection.Item(index)


def _goto_already_open_presentation(
    path: str,
    page_no: int,
    *,
    timeout_sec: float = _OPEN_ATTACH_TIMEOUT_SEC,
) -> bool:
    """Best-effort navigation in a document already opened by Windows.

    This deliberately uses ``GetActiveObject`` only.  It must never call
    ``Dispatch*`` or ``Presentations.Open``: doing so can expose the hidden,
    low-DPI preview automation session as the user's normal PowerPoint window.
    """
    pythoncom = None
    initialized = False
    target = _normalized_path(path)
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
        while True:
            try:
                app = win32com.client.GetActiveObject("PowerPoint.Application")
                presentations = app.Presentations
                count = int(presentations.Count)
                for index in range(1, count + 1):
                    pres = _com_item(presentations, index)
                    if _normalized_path(getattr(pres, "FullName", "")) != target:
                        continue
                    if not 1 <= int(page_no) <= int(pres.Slides.Count):
                        return False
                    windows = pres.Windows
                    if int(windows.Count) < 1:
                        return False
                    window = _com_item(windows, 1)
                    window.Activate()
                    window.View.GotoSlide(int(page_no))
                    return True
            except Exception as exc:  # noqa: BLE001 document may still be loading
                log.debug("attach-only PowerPoint navigation not ready: %s", exc)
            if time.monotonic() >= deadline:
                return False
            time.sleep(_OPEN_ATTACH_POLL_SEC)
    except Exception as exc:  # noqa: BLE001 COM is optional; shell open already succeeded
        log.debug("attach-only PowerPoint navigation unavailable: %s", exc)
        return False
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


def open_at_page(path: str, page_no: int) -> tuple[bool, bool]:
    """由 Windows 正常打开文件，再只读附着并尝试跳到 ``page_no``。

    返回 (是否已打开, 是否成功跳页)。COM 只负责导航已打开的文档，绝不
    负责启动 PowerPoint 或打开原文件，避免复用预览自动化会话污染显示质量。
    """
    if not os.path.exists(path):
        return (False, False)
    if os.path.splitext(path)[1].lower() not in PPT_EXTS:
        return (open_file(path), False)
    opened = open_file(path)
    if not opened:
        return (False, False)
    return (True, _goto_already_open_presentation(path, page_no))
