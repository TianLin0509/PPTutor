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


def _explorer_exe() -> str:
    """走绝对路径，别让 PATH 上的同名程序有机会顶上来。"""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "explorer.exe")


def _select_via_shell_api(target: str) -> bool:
    r"""SHOpenFolderAndSelectItems 定位并选中，完全不经过命令行。

    explorer 的命令行解析器是非标准的：`/select,` 后面的路径必须**自己**带引号，
    整体加引号（`"/select,C:\a b\c.pptx"`）它认不出来，会默默打开「文档」——
    这正是「打开成错误的文件夹」的来源。而 subprocess 传列表时，Windows 的
    list2cmdline 见参数里有空格就会整体加引号，于是**每个带空格的路径都中招**。
    路径里有逗号则相反：不加引号时 explorer 会在逗号处截断。

    所以首选根本不拼命令行的 shell API：空格、逗号、`&`、中文一律无所谓。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
    except (AttributeError, OSError):
        return False

    # 这个函数跑在后台线程（UI 走 _run_bg），COM 必须在本线程初始化。
    COINIT_APARTMENTTHREADED = 0x2
    RPC_E_CHANGED_MODE = -2147417850  # 0x80010106：本线程已按别的模型初始化过
    hr_init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    need_uninit = hr_init in (0, 1)  # S_OK / S_FALSE 都要配对 CoUninitialize
    if hr_init < 0 and hr_init != RPC_E_CHANGED_MODE:
        return False

    def _parse(name: str):
        pidl = ctypes.c_void_p()
        hr = shell32.SHParseDisplayName(
            wintypes.LPCWSTR(name), None, ctypes.byref(pidl), 0, None)
        return pidl if hr >= 0 and pidl else None

    folder_pidl = item_pidl = None
    try:
        parent = os.path.dirname(target) or target
        folder_pidl = _parse(parent)
        item_pidl = _parse(target)
        if folder_pidl is None:
            return False
        if item_pidl is None:
            # 目录本身可定位不到子项时，退而打开该目录
            hr = shell32.SHOpenFolderAndSelectItems(folder_pidl, 0, None, 0)
            return hr >= 0
        arr = (ctypes.c_void_p * 1)(item_pidl)
        hr = shell32.SHOpenFolderAndSelectItems(folder_pidl, 1, arr, 0)
        return hr >= 0
    except (OSError, ValueError, AttributeError):
        return False
    finally:
        for pidl in (item_pidl, folder_pidl):
            if pidl:
                try:
                    ole32.CoTaskMemFree(pidl)
                except OSError:
                    pass
        if need_uninit:
            try:
                ole32.CoUninitialize()
            except OSError:
                pass


def explorer_select_command(target: str) -> str:
    r"""`explorer.exe /select,"路径"`：引号只包路径，绝不包住 `/select,`。

    这一条就是修复本身。原来传的是列表，`list2cmdline` 见参数含空格便整体加引号，
    explorer 的非标准解析器认不出来 → 默默打开默认文件夹（实测是「文档」/「桌面」）。
    """
    return f'"{_explorer_exe()}" /select,"{target}"'


def open_folder(path: str) -> bool:
    """在资源管理器中定位文件；文件已不在则退而打开其父目录。

    先起 explorer 而不是先调 shell API，是量出来的：`SHOpenFolderAndSelectItems`
    是同步的，需要新开窗口时实测阻塞 **1,465～1,742 ms**（复用已有窗口 42 ms）；
    而 explorer 在自己进程里干活，`Popen` **30～55 ms** 就返回。health / report
    那几个调用点是直接在 UI 线程上调的，同步版本会变成一次可见卡顿——旧实现用
    Popen，天然异步，所以从没暴露过这个问题，修 bug 不该顺手把它引进来。

    两条路都实测过带空格 / 逗号 / `&` / 中文的路径，结果一致；shell API 留作
    explorer 起不来时的兜底（它不经过任何命令行解析，最不挑路径）。
    """
    if os.path.exists(path):
        target = os.path.normpath(os.path.abspath(path))
        try:
            # 传字符串而不是列表：list2cmdline 的加引号规则正是 bug 本身。
            subprocess.Popen(explorer_select_command(target),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        except OSError:
            log.warning("explorer /select failed, falling back to shell API",
                        exc_info=True)
        return _select_via_shell_api(target)
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


def _powerpoint_process_running() -> bool | None:
    """Read-only fallback for distinguishing 'not running' from COM audit failure."""
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0:
            return None
        return "POWERPNT.EXE" in completed.stdout.upper()
    except (OSError, subprocess.SubprocessError):
        return None


def presentation_open_state(path: str) -> bool | None:
    """Return whether ``path`` is open in the existing PowerPoint session.

    ``None`` means PowerPoint appears to be running but its document collection
    cannot be audited.  Destructive callers must treat that state as busy.  The
    helper uses ``GetActiveObject`` only: it never starts PowerPoint, opens a
    presentation, activates a window, or changes the user's selection.
    """
    if os.name != "nt":
        return False
    pythoncom = None
    initialized = False
    target = _normalized_path(path)
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
        except Exception:  # noqa: BLE001 no ROT entry is safe only if no process exists
            running = _powerpoint_process_running()
            return False if running is False else None
        try:
            presentations = app.Presentations
            for index in range(1, int(presentations.Count) + 1):
                pres = _com_item(presentations, index)
                if _normalized_path(getattr(pres, "FullName", "")) == target:
                    return True
            return False
        except Exception as exc:  # noqa: BLE001 missing proof must fail closed
            log.warning("cannot audit open PowerPoint presentations: %s", exc)
            return None
    except Exception as exc:  # noqa: BLE001 pywin32/COM unavailable
        running = _powerpoint_process_running()
        if running is not False:
            log.warning("cannot initialize PowerPoint open-file audit: %s", exc)
            return None
        return False
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


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
