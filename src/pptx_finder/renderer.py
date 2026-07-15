"""预览渲染：PowerPoint COM 导出指定页为 PNG，带磁盘缓存。

隔离：打包版用 renderer 子进程；主动预览用临时快照，避免直接打开用户正在
     编辑的原文件。DispatchEx 不是强沙箱，因此后台 COM 渲染需经安全门。
线程：COM 为单线程套间，调用线程需 CoInitialize（本模块惰性处理）。
     UI 侧应在一个专用渲染线程里串行调用，避免并发与界面卡顿。
失败策略：任何异常都返回 None，由 UI 显示「无法预览，可直接打开」兜底。
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import xxhash

from .config import cache_dir

log = logging.getLogger(__name__)

_state = threading.local()
# 预览优先的 COM 单槽锁：预览(RenderWorker)与缩略图(ThumbWorker)共用一个 PowerPoint
# COM 渲染通道（不并发）。缩略图 FIFO 渲染一整屏（24 个）会把用户正盯着的预览挤到后面
# 排队（实测预览明显变慢的主因）。下面这把「优先锁」让预览插队：有预览在排队时，缩略图
# 一律先让路（且不预先堆到锁队列上），等预览渲完再继续。
_cv = threading.Condition()
_busy = False        # COM 通道当前是否被占用
_hi_waiting = 0      # 排队中的高优先(预览)请求数
_waiting_by_priority: dict[int, int] = {}
_FAILED_TTL_SEC = 90.0
_failed_until: dict[tuple[str, int, str, int], float] = {}
_FALSE = {"0", "false", "no", "off"}
_TRUE = {"1", "true", "yes", "on"}
_POWERPOINT_ACTIVE_TTL_SEC = 2.0
_powerpoint_active_cache_at = 0.0
_powerpoint_active_cache = False
_RENDER_CACHE_PATTERN = re.compile(
    r"^[0-9a-f]{16}_(?:\d+_\d+|safe_\d+_\d+)\.png$",
    re.IGNORECASE,
)
_RENDER_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
_RENDER_CACHE_MAX_FILES = 2000
_RENDER_CACHE_TRIM_RATIO = 0.8
_COMPAT_RENDER_TIMEOUT_SEC = 30.0


class PowerPointSessionBusy(RuntimeError):
    """A user/foreign PowerPoint COM server already exists; preview must stand down."""


class PowerPointHandoffBusy(RuntimeError):
    """A hidden preview PowerPoint process has not finished exiting yet."""


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def _powerpoint_active(*, force: bool = False) -> bool:
    """Best-effort check for an already running user PowerPoint session."""
    global _powerpoint_active_cache_at, _powerpoint_active_cache
    if os.name != "nt":
        return False
    now = time.monotonic()
    if not force and now - _powerpoint_active_cache_at < _POWERPOINT_ACTIVE_TTL_SEC:
        return _powerpoint_active_cache

    active = False
    pythoncom = None
    initialized = False
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
        # The mere existence of a registered server is enough.  An empty or
        # hidden instance can still be the user's start screen or an orphaned
        # automation server; DispatchEx is not a reliable isolation boundary.
        win32com.client.GetActiveObject("PowerPoint.Application")
        active = True
    except Exception:  # noqa: BLE001
        active = False
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
    _powerpoint_active_cache_at = now
    _powerpoint_active_cache = active
    return active


def _invalidate_powerpoint_active_cache() -> None:
    global _powerpoint_active_cache_at
    _powerpoint_active_cache_at = 0.0


def _powerpoint_process_ids() -> set[int] | None:
    """Return running POWERPNT PIDs, or None when ownership cannot be audited."""
    if os.name != "nt":
        return set()
    try:
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32process  # type: ignore

        pids = win32process.EnumProcesses()
    except Exception:  # noqa: BLE001
        return None
    found: set[int] = set()
    access = int(win32con.PROCESS_QUERY_INFORMATION) | int(win32con.PROCESS_VM_READ)
    for raw_pid in pids:
        pid = int(raw_pid or 0)
        if pid <= 0:
            continue
        handle = None
        try:
            handle = win32api.OpenProcess(access, False, pid)
            executable = str(win32process.GetModuleFileNameEx(handle, 0) or "")
            if os.path.basename(executable).lower() == "powerpnt.exe":
                found.add(pid)
        except Exception:  # noqa: BLE001 access-denied/system processes are irrelevant
            pass
        finally:
            if handle is not None:
                try:
                    handle.Close()
                except Exception:  # noqa: BLE001
                    pass
    return found


def _app_hwnd(app) -> int | None:
    """Return PowerPoint's HWND across early- and dynamic-dispatch wrappers."""
    try:
        value = getattr(app, "HWND")
        if callable(value):
            value = value()
        hwnd = int(value or 0)
        return hwnd if hwnd > 0 else None
    except Exception:  # noqa: BLE001 a missing HWND must never widen ownership
        return None


def _pid_for_app(app) -> int | None:
    if os.name != "nt":
        return None
    try:
        import win32process  # type: ignore

        hwnd = _app_hwnd(app)
        if hwnd is None:
            return None
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:  # noqa: BLE001
        return None


def _discover_owned_powerpoint_pid(
    app,
    *,
    existing_pids: set[int],
    timeout_sec: float = 1.0,
) -> int | None:
    """Prove ownership even when a hidden Application has no usable HWND yet."""
    direct = _pid_for_app(app)
    if direct is not None and direct not in existing_pids:
        return direct
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        observed = _powerpoint_process_ids()
        if observed is None:
            return None
        created = set(observed) - set(existing_pids)
        if len(created) == 1:
            return next(iter(created))
        if len(created) > 1 or time.monotonic() >= deadline:
            return None
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _open_owned_powerpoint_handle(pid: int):
    """Pin the exact preview process so PID reuse can never widen cleanup."""
    if os.name != "nt" or int(pid or 0) <= 0:
        return None
    try:
        import win32api  # type: ignore
        import win32con  # type: ignore

        access = int(win32con.PROCESS_TERMINATE) | int(win32con.SYNCHRONIZE)
        return win32api.OpenProcess(access, False, int(pid))
    except Exception:  # noqa: BLE001 no process handle means no hard-exit authority
        return None


def _close_process_handle(handle) -> None:
    if handle is None:
        return
    try:
        handle.Close()
    except Exception:  # noqa: BLE001
        pass


def _pid_has_visible_window(pid: int) -> bool:
    """Conservative visible-window check; errors mean 'visible' (never quit)."""
    if os.name != "nt":
        return True
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore

        visible = False

        def _visit(hwnd, _extra):
            nonlocal visible
            if visible or not win32gui.IsWindowVisible(hwnd):
                return
            try:
                window_pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
            except Exception:  # noqa: BLE001
                return
            if window_pid == int(pid):
                visible = True

        win32gui.EnumWindows(_visit, None)
        return visible
    except Exception:  # noqa: BLE001
        return True


def wait_for_external_open_ready(timeout_sec: float = 3.0) -> bool:
    """Wait until Windows cannot reuse a headless PowerPoint preview process.

    A visible PowerPoint session belongs to the user and is safe for shell-open.
    A headless process is not: Windows may reuse it and expose preview DPI/state.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        pids = _powerpoint_process_ids()
        if pids is not None and all(_pid_has_visible_window(pid) for pid in pids):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
def background_powerpoint_allowed() -> bool:
    """Return whether non-user-triggered PowerPoint rendering may run."""
    flag = _env_bool("PPTUTOR_BACKGROUND_POWERPOINT_RENDER")
    if flag is not None:
        return flag
    return not _powerpoint_active()


@contextlib.contextmanager
def _com_slot(hi_priority: bool, priority: int | None = None):
    """Acquire the single COM render slot.

    Lower numeric priority wins. ``hi_priority`` remains compatible with older
    callers and maps to priority 0.
    """
    global _busy, _hi_waiting
    if priority is None:
        priority = 0 if hi_priority else 100
    priority = int(priority)
    with _cv:
        if hi_priority:
            _hi_waiting += 1
        _waiting_by_priority[priority] = _waiting_by_priority.get(priority, 0) + 1
        while _busy or any(p < priority and n > 0 for p, n in _waiting_by_priority.items()):
            _cv.wait()
        n = _waiting_by_priority.get(priority, 0) - 1
        if n > 0:
            _waiting_by_priority[priority] = n
        else:
            _waiting_by_priority.pop(priority, None)
        if hi_priority:
            _hi_waiting -= 1
        _busy = True
    try:
        yield
    finally:
        with _cv:
            _busy = False
            _cv.notify_all()


def _get_app():
    app = getattr(_state, "app", None)
    if app is not None:
        return app
    existing_pids = _powerpoint_process_ids()
    if existing_pids is None:
        raise PowerPointSessionBusy(
            "cannot audit PowerPoint process ownership; preview renderer disabled"
        )
    if existing_pids or _powerpoint_active(force=True):
        raise PowerPointSessionBusy(
            "PowerPoint is already running; refusing to attach the preview renderer"
        )
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    _state.com_initialized_by_renderer = True
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
    except Exception:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass
        _state.com_initialized_by_renderer = False
        raise
    _state.app = app
    owned_pid = _discover_owned_powerpoint_pid(app, existing_pids=existing_pids)
    if owned_pid is None or owned_pid in existing_pids:
        _state.app_owned_pid = None
        _release_local_app_reference()
        raise PowerPointSessionBusy(
            "could not prove exclusive ownership of the preview PowerPoint process"
        )
    _state.app_owned_pid = owned_pid
    _state.app_owned_handle = _open_owned_powerpoint_handle(owned_pid)
    # Force every future first-session decision to observe current ROT state,
    # not the cached pre-creation "no PowerPoint" result.
    _invalidate_powerpoint_active_cache()
    return app


def _release_local_app_reference() -> None:
    """Release this COM apartment; quit only a proven-owned, empty, headless app."""
    app = getattr(_state, "app", None)
    owned_pid = getattr(_state, "app_owned_pid", None)
    owned_handle = getattr(_state, "app_owned_handle", None)
    # A transient RPC rejection can make Presentation.Close fail.  Dropping the
    # COM apartment here would orphan the hash-named snapshot in PowerPoint and
    # let Windows later reuse that hidden session for a user's real document.
    # Keep the proven-owned reference so a later close/shutdown can retry.
    if app is not None and list(getattr(_state, "owned_presentations", [])):
        return
    if app is not None and owned_pid is not None:
        try:
            # Headless PowerPoint commonly exposes no usable HWND.  The process
            # handle captured at creation is stronger proof than a late HWND
            # lookup and remains bound to the same process even after PID reuse.
            same_process = owned_handle is not None or _pid_for_app(app) == int(owned_pid)
            empty = int(app.Presentations.Count) == 0
            headless = not _pid_has_visible_window(int(owned_pid))
            if same_process and empty and headless:
                if not _request_owned_powerpoint_exit(
                    app,
                    int(owned_pid),
                    owned_handle=owned_handle,
                ):
                    log.warning(
                        "owned preview PowerPoint did not accept bounded exit: pid=%s",
                        owned_pid,
                    )
                    # Keep the exact process handle and COM reference for a later
                    # idle-cleanup retry.  Forgetting them here can leave a
                    # headless POWERPNT.EXE alive for Explorer to reuse, carrying
                    # the preview snapshot name and rendering state into the
                    # user's next normal presentation.
                    return
        except Exception as exc:  # noqa: BLE001 release below remains mandatory
            log.debug("owned preview PowerPoint did not quit cleanly: %s", exc)
    _close_process_handle(owned_handle)
    _state.app = None
    _state.app_owned_pid = None
    _state.app_owned_handle = None
    _invalidate_powerpoint_active_cache()
    if bool(getattr(_state, "com_initialized_by_renderer", False)):
        try:
            import pythoncom

            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass
    _state.com_initialized_by_renderer = False


def _request_owned_powerpoint_exit(
    app,
    owned_pid: int,
    *,
    owned_handle=None,
    graceful_wait_sec: float = 0.35,
) -> bool:
    """Bounded exit for this renderer's proven-empty, headless POWERPNT.EXE.

    ``PowerPoint.Application.Quit`` can block the COM apartment for roughly a
    minute even after the last presentation is closed.  That leaves every next
    page preview queued behind cleanup.  We instead post the normal window-close
    message, wait briefly, and only if the *same exact process* is still both
    empty and headless terminate that process handle.  Any visible window,
    foreign PID, user document, audit failure, or non-Windows platform makes the
    operation fail closed without touching PowerPoint.
    """
    if os.name != "nt":
        return False
    pid = int(owned_pid or 0)
    if pid <= 0:
        return False
    opened_here = False
    try:
        if owned_handle is None and _pid_for_app(app) != pid:
            return False
        if int(app.Presentations.Count) != 0:
            return False
        if _pid_has_visible_window(pid):
            return False

        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32event  # type: ignore
        import win32gui  # type: ignore

        handle = owned_handle
        if handle is None:
            access = int(win32con.PROCESS_TERMINATE) | int(win32con.SYNCHRONIZE)
            handle = win32api.OpenProcess(access, False, pid)
            opened_here = True
    except Exception:  # noqa: BLE001 ownership/handle uncertainty means no action
        return False

    try:
        status = win32event.WaitForSingleObject(handle, 0)
        if status == win32event.WAIT_OBJECT_0:
            return True
        if status != win32event.WAIT_TIMEOUT:
            return False
        try:
            hwnd = _app_hwnd(app)
            if hwnd is not None:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001 bounded hard exit remains available
            pass

        wait_ms = max(0, int(float(graceful_wait_sec) * 1000))
        if win32event.WaitForSingleObject(handle, wait_ms) == win32event.WAIT_OBJECT_0:
            return True

        # Re-check the only property available without another potentially
        # blocking COM round-trip.  A user-visible session is never terminated.
        if _pid_has_visible_window(pid):
            return False
        # Close the race where a user's document is routed into the preview
        # server between the first audit and the bounded exit request.
        if int(app.Presentations.Count) != 0:
            return False
        win32api.TerminateProcess(handle, 0)
        return (
            win32event.WaitForSingleObject(handle, 2000)
            == win32event.WAIT_OBJECT_0
        )
    except Exception:  # noqa: BLE001 never widen the kill boundary on failure
        return False
    finally:
        if opened_here:
            _close_process_handle(handle)


def _cleanup_snapshot() -> None:
    snapshot = getattr(_state, "snapshot_path", None)
    _state.snapshot_path = None
    _state.snapshot_src = None
    _state.snapshot_key = None
    if snapshot:
        try:
            os.remove(snapshot)
        except OSError:
            pass


def _cleanup_stale_snapshots(max_age_sec: float = 24 * 60 * 60) -> int:
    if bool(getattr(_state, "stale_snapshots_checked", False)):
        return 0
    _state.stale_snapshots_checked = True
    removed = 0
    cutoff = time.time() - max(0.0, float(max_age_sec))
    directory = cache_dir() / "render_snapshots"
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return 0
    current = os.path.normcase(str(getattr(_state, "snapshot_path", "") or ""))
    for candidate in candidates:
        try:
            if (
                not candidate.is_file()
                or os.path.normcase(str(candidate)) == current
                or candidate.stat().st_mtime >= cutoff
            ):
                continue
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _application_dir() -> Path:
    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _find_soffice() -> Path | None:
    """Locate an optional LibreOffice CLI used only when PowerPoint is busy."""
    configured = str(os.environ.get("PPTUTOR_SOFFICE_PATH", "") or "").strip()
    engine_dir = str(os.environ.get("PPTUTOR_PREVIEW_ENGINE_DIR", "") or "").strip()
    engine_roots = [
        Path(engine_dir).expanduser() if engine_dir else None,
        _application_dir() / "preview-engine",
    ]
    candidates = [configured]
    for root in engine_roots:
        if root is None:
            continue
        candidates.extend([
            str(root / "LibreOfficePortable/App/libreoffice/program/soffice.com"),
            str(root / "LibreOfficePortable/App/libreoffice/program/soffice.exe"),
            str(root / "LibreOffice/program/soffice.com"),
            str(root / "LibreOffice/program/soffice.exe"),
            str(root / "program/soffice.com"),
            str(root / "program/soffice.exe"),
            str(root / "soffice.com"),
            str(root / "soffice.exe"),
        ])
    candidates.extend([
        shutil.which("soffice.com") or "",
        shutil.which("soffice.exe") or "",
        shutil.which("soffice") or "",
    ])
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\LibreOffice\program\soffice.com",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
            str(Path.home() / "AppData/Local/Programs/LibreOffice/program/soffice.com"),
        ])
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def preview_engine_status() -> dict[str, object]:
    """Describe the independent image renderer without starting any process."""
    soffice = _find_soffice()
    if soffice is None:
        return {"available": False, "source": "missing", "path": "-"}
    source = "system"
    try:
        resolved = soffice.resolve()
        bundled_root = (_application_dir() / "preview-engine").resolve()
        if resolved.is_relative_to(bundled_root):
            source = "bundled"
        elif (
            str(os.environ.get("PPTUTOR_SOFFICE_PATH", "") or "").strip()
            or str(os.environ.get("PPTUTOR_PREVIEW_ENGINE_DIR", "") or "").strip()
        ):
            source = "configured"
    except OSError:
        resolved = soffice
    return {"available": True, "source": source, "path": str(resolved)}


def _terminate_compat_processes(profile_uri: str) -> int:
    """Terminate only LibreOffice processes carrying our unique profile URI."""
    if os.name != "nt" or not profile_uri:
        return 0
    pythoncom = None
    initialized = False
    terminated = 0
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
        service = win32com.client.GetObject(r"winmgmts:\\.\root\cimv2")
        needle = profile_uri.casefold()
        for process in service.ExecQuery(
            "SELECT Name, CommandLine, ProcessId FROM Win32_Process "
            "WHERE Name LIKE 'soffice%'"
        ):
            command_line = str(getattr(process, "CommandLine", "") or "")
            if needle not in command_line.casefold():
                continue
            try:
                process.Terminate()
                terminated += 1
            except Exception:  # noqa: BLE001 one child may already be exiting
                continue
    except Exception as exc:  # noqa: BLE001 timeout cleanup stays best-effort
        log.warning("compat timeout process cleanup failed: %s", exc)
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
    return terminated


def _render_pdf_page(pdf_path: Path, page_no: int, long_edge: int, out: Path) -> Path | None:
    """Render one local PDF page through QtPdf without another long-lived process."""
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    document = QPdfDocument()
    try:
        error = document.load(str(pdf_path))
        if error != QPdfDocument.Error.None_:
            return None
        page_index = int(page_no) - 1
        if page_index < 0 or page_index >= int(document.pageCount()):
            return None
        points = document.pagePointSize(page_index)
        page_width = max(1.0, float(points.width()))
        page_height = max(1.0, float(points.height()))
        edge = max(64, int(long_edge))
        if page_width >= page_height:
            width = edge
            height = max(1, int(round(edge * page_height / page_width)))
        else:
            height = edge
            width = max(1, int(round(edge * page_width / page_height)))
        image = document.render(page_index, QSize(width, height))
        if image.isNull():
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        if not image.save(str(tmp), "PNG"):
            return None
        os.replace(tmp, out)
        return out if out.exists() and out.stat().st_size > 0 else None
    finally:
        document.close()


def _render_page_compat(
    path: str,
    page_no: int,
    cache_key: str,
    long_edge: int,
    out: Path,
) -> Path | None:
    """Render without touching a running PowerPoint session.

    LibreOffice is an optional, isolated fallback.  The first clicked page
    converts a safe copy to a cached PDF; later page turns rasterize that PDF
    directly and do not keep LibreOffice, PowerPoint, or another worker alive.
    """
    soffice = _find_soffice()
    if soffice is None:
        log.info("compat preview unavailable: LibreOffice was not found")
        return None
    root = cache_dir()
    pdf_dir = root / "compat_pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{cache_key}.pdf"
    try:
        pdf_ready = pdf_path.exists() and pdf_path.stat().st_size > 0
    except OSError:
        pdf_ready = False
    if not pdf_ready:
        work_root = root / "compat_work"
        work_root.mkdir(parents=True, exist_ok=True)
        work: Path | None = None
        try:
            work = Path(tempfile.mkdtemp(
                prefix=f"{cache_key}-",
                dir=str(work_root),
            ))
            out_dir = work / "out"
            out_dir.mkdir()
            # Keep one isolated profile across decks.  A fresh LibreOffice
            # profile can spend 8-12 seconds on first-run initialization; a
            # stable private profile makes later files start in roughly 1-2s
            # while remaining completely separate from the user's LibreOffice.
            profile = root / "compat_profile"
            profile.mkdir(parents=True, exist_ok=True)
            snapshot = work / f"source{Path(path).suffix or '.pptx'}"
            shutil.copy2(path, snapshot)
            profile_uri = profile.resolve().as_uri()
            command = [
                str(soffice),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                "--norestore",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                "pdf:impress_pdf_Export",
                "--outdir",
                str(out_dir),
                str(snapshot),
            ]
            creationflags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                | int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
                if os.name == "nt"
                else 0
            )
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=_COMPAT_RENDER_TIMEOUT_SEC,
                    check=False,
                    creationflags=creationflags,
                )
            except subprocess.TimeoutExpired:
                killed = _terminate_compat_processes(profile_uri)
                log.warning(
                    "compat preview timed out path=%s terminated=%s",
                    path,
                    killed,
                )
                return None
            produced = out_dir / f"{snapshot.stem}.pdf"
            if (
                completed.returncode != 0
                or not produced.exists()
                or produced.stat().st_size <= 0
            ):
                log.warning(
                    "compat preview conversion failed rc=%s path=%s stderr=%s",
                    completed.returncode,
                    path,
                    str(completed.stderr or "")[-400:],
                )
                return None
            tmp_pdf = pdf_path.with_suffix(".pdf.tmp")
            shutil.copy2(produced, tmp_pdf)
            os.replace(tmp_pdf, pdf_path)
        except Exception as exc:  # noqa: BLE001 isolated fallback must fail quietly
            log.warning("compat preview failed path=%s: %s", path, exc)
            return None
        finally:
            if work is not None:
                try:
                    shutil.rmtree(work)
                except OSError as exc:
                    # soffice.com can return a fraction before soffice.bin has
                    # released its profile registry.  The cached PDF is already
                    # atomic and valid; a transient temp cleanup race must never
                    # erase the user's successful first preview.
                    log.info("compat workdir cleanup deferred path=%s: %s", work, exc)
    else:
        try:
            os.utime(pdf_path, None)  # LRU touch: a frequently flipped deck stays cached
        except OSError:
            pass
    try:
        return _render_pdf_page(pdf_path, page_no, long_edge, out)
    except Exception as exc:  # noqa: BLE001 malformed PDFs must not kill the worker
        log.warning("compat PDF render failed path=%s page=%s: %s", path, page_no, exc)
        return None


def maintain_render_cache(
    *,
    max_bytes: int = _RENDER_CACHE_MAX_BYTES,
    max_files: int = _RENDER_CACHE_MAX_FILES,
    compat_work_max_age_sec: float = 10 * 60,
) -> dict[str, int]:
    """Bound page previews/compat PDFs and remove stale isolated workdirs."""
    maximum_bytes = max(1, int(max_bytes))
    maximum_files = max(1, int(max_files))
    workdirs_deleted = 0
    work_root = cache_dir() / "compat_work"
    cutoff = time.time() - max(0.0, float(compat_work_max_age_sec))
    try:
        workdirs = list(work_root.iterdir())
    except OSError:
        workdirs = []
    for directory in workdirs:
        try:
            if not directory.is_dir() or directory.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(directory)
            workdirs_deleted += 1
        except OSError:
            continue

    candidates: list[tuple[float, int, Path, str]] = []
    try:
        entries = list(cache_dir().iterdir())
    except OSError:
        return {
            "files": 0,
            "bytes": 0,
            "deleted": 0,
            "compat_pdfs_deleted": 0,
            "workdirs_deleted": workdirs_deleted,
        }
    for candidate in entries:
        if not _RENDER_CACHE_PATTERN.fullmatch(candidate.name):
            continue
        try:
            stat = candidate.stat()
            if not candidate.is_file():
                continue
        except OSError:
            continue
        candidates.append((float(stat.st_mtime), int(stat.st_size), candidate, "png"))
    pdf_dir = cache_dir() / "compat_pdf"
    try:
        pdf_entries = list(pdf_dir.glob("*.pdf"))
    except OSError:
        pdf_entries = []
    for candidate in pdf_entries:
        try:
            stat = candidate.stat()
            if not candidate.is_file() or stat.st_size <= 0:
                continue
        except OSError:
            continue
        candidates.append((float(stat.st_mtime), int(stat.st_size), candidate, "compat_pdf"))
    total_bytes = sum(size for _mtime, size, _path, _kind in candidates)
    if len(candidates) <= maximum_files and total_bytes <= maximum_bytes:
        return {
            "files": len(candidates),
            "bytes": total_bytes,
            "deleted": 0,
            "compat_pdfs_deleted": 0,
            "workdirs_deleted": workdirs_deleted,
        }

    target_bytes = max(1, int(maximum_bytes * _RENDER_CACHE_TRIM_RATIO))
    target_files = max(1, int(maximum_files * _RENDER_CACHE_TRIM_RATIO))
    deleted = 0
    compat_pdfs_deleted = 0
    remaining = len(candidates)
    for _mtime, size, candidate, kind in sorted(candidates, key=lambda item: item[0]):
        if remaining <= target_files and total_bytes <= target_bytes:
            break
        try:
            candidate.unlink()
        except OSError:
            continue
        deleted += 1
        if kind == "compat_pdf":
            compat_pdfs_deleted += 1
        remaining -= 1
        total_bytes = max(0, total_bytes - size)
    return {
        "files": remaining,
        "bytes": total_bytes,
        "deleted": deleted,
        "compat_pdfs_deleted": compat_pdfs_deleted,
        "workdirs_deleted": workdirs_deleted,
    }


def _snapshot_for_render(path: str, cache_key: str) -> str | None:
    """Copy the source file once per render key so COM never opens the live file."""
    snapshot = getattr(_state, "snapshot_path", None)
    if (
        snapshot
        and getattr(_state, "snapshot_src", None) == path
        and getattr(_state, "snapshot_key", None) == cache_key
        and os.path.exists(snapshot)
    ):
        return snapshot

    _cleanup_snapshot()
    _cleanup_stale_snapshots()
    try:
        snap_dir = cache_dir() / "render_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(path).suffix or ".pptx"
        out = snap_dir / f"{cache_key}{suffix}"
        tmp = out.with_suffix(out.suffix + ".tmp")
        shutil.copy2(path, tmp)
        os.replace(tmp, out)
        _state.snapshot_path = str(out)
        _state.snapshot_src = path
        _state.snapshot_key = cache_key
        return str(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot copy failed path=%s: %s", path, exc)
        _cleanup_snapshot()
        return None


def prewarm() -> bool:
    """Warm PowerPoint only when background COM work is safe."""
    if not background_powerpoint_allowed():
        return False
    if _ipc_enabled():
        try:
            from . import render_client

            render_client.prewarm()
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        _get_app()
        return True
    except Exception:  # noqa: BLE001
        return False


def _open_pres(app, path: str, cache_key: str):
    """复用上次打开的 Presentation：同文件同内容直接返回，免重复 Open（翻页 / 多次预览
    同一稿，最耗时的就是 Open，大稿尤甚）；换文件或文件已变（cache_key 含 mtime+size）
    则关旧开新。ReadOnly 打开不锁文件写入（实测），不影响恢复/导出覆盖该文件。"""
    if (getattr(_state, "pres", None) is not None
            and getattr(_state, "pres_path", None) == path
            and getattr(_state, "pres_key", None) == cache_key):
        return _state.pres
    if not _close_pres(clean_snapshot=False):
        raise RuntimeError("previous preview presentation is still closing")
    pres = app.Presentations.Open(path, ReadOnly=1, WithWindow=0)
    _state.pres = pres
    owned = list(getattr(_state, "owned_presentations", []))
    if not any(item is pres for item in owned):
        owned.append(pres)
    _state.owned_presentations = owned
    _state.pres_path = path
    _state.pres_key = cache_key
    try:
        sw = float(pres.PageSetup.SlideWidth)
        sh = float(pres.PageSetup.SlideHeight)
        _state.pres_ratio = sh / sw if sw else 9 / 16
    except Exception:  # noqa: BLE001
        _state.pres_ratio = 9 / 16
    return pres


def _close_pres(*, clean_snapshot: bool = True) -> bool:
    pres = getattr(_state, "pres", None)
    owned = list(getattr(_state, "owned_presentations", []))
    if pres is not None and not any(item is pres for item in owned):
        owned.append(pres)
    # Close only presentations explicitly opened by this renderer.  Keeping all
    # references (not just the latest) lets shutdown recover from a prior Close
    # failure instead of leaking hash-named snapshot decks into the taskbar.
    failed = []
    for item in owned:
        closed = False
        for attempt in range(3):
            try:
                item.Close()
                closed = True
                break
            except Exception:  # noqa: BLE001 PowerPoint may temporarily reject RPC calls
                if attempt < 2:
                    time.sleep(0.05)
        if not closed:
            failed.append(item)
    _state.owned_presentations = failed
    _state.pres = None
    _state.pres_path = None
    _state.pres_key = None
    if clean_snapshot and not failed:
        _cleanup_snapshot()
    return not failed


def cache_key_for_metadata(path: str, mtime: float, size: int) -> str:
    """Pure cache-key calculation safe for the GUI thread."""
    raw = f"{os.path.abspath(path)}|{float(mtime)}|{int(size)}"
    return xxhash.xxh64(raw.encode("utf-8")).hexdigest()


def default_cache_key(path: str) -> str | None:
    """以 路径+mtime+size 派生缓存键；文件变了就换新键、自动失效旧图。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return cache_key_for_metadata(path, st.st_mtime, st.st_size)


def _ipc_enabled() -> bool:
    try:
        from .render_client import should_use_ipc

        return should_use_ipc()
    except Exception:  # noqa: BLE001
        return False


def close_current_presentation() -> None:
    """Release the presentation currently held by this renderer without quitting PowerPoint."""
    if _ipc_enabled():
        try:
            from . import render_client

            render_client.close_current_presentation()
            return
        except Exception:  # noqa: BLE001
            pass
    _close_pres()


def find_cached_render(
    path: str,
    page_no: int,
    cache_key: str | None = None,
    min_long_edge: int = 1,
) -> Path | None:
    """Return an existing cached render that is sharp enough for this request."""
    path = os.path.abspath(path)
    if cache_key is None:
        cache_key = default_cache_key(path)
        if cache_key is None:
            return None
    min_long_edge = int(min_long_edge)
    prefix = f"{cache_key}_{page_no}_"
    best: tuple[int, Path] | None = None
    for candidate in cache_dir().glob(f"{prefix}*.png"):
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size <= 0:
                continue
        except OSError:
            continue
        stem = candidate.stem
        if not stem.startswith(prefix):
            continue
        try:
            edge = int(stem[len(prefix):])
        except ValueError:
            continue
        if edge < min_long_edge:
            continue
        if best is None or edge > best[0]:
            best = (edge, candidate)
    return best[1] if best is not None else None


def _render_page_direct(
    path: str,
    page_no: int,
    cache_key: str | None = None,
    long_edge: int = 2560,
    hi_priority: bool = False,
    priority: int | None = None,
    use_snapshot: bool = False,
    existing_session_only: bool = False,
) -> Path | None:
    """导出 path 第 page_no 页（1-based）为高清 PNG，返回缓存路径；失败返回 None。

    long_edge 为长边像素，高度按 slide 实际宽高比自适应（兼容 16:9 / 4:3）。
    缓存文件名含 long_edge，提分辨率后旧低清缓存自动失效。
    hi_priority=True（预览）抢占共享 COM 锁，缩略图等低优先渲染让路（见 _priority）。
    """
    path = os.path.abspath(path)
    if cache_key is None:
        cache_key = default_cache_key(path)
        if cache_key is None:
            return None
    out = cache_dir() / f"{cache_key}_{page_no}_{long_edge}.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    cached = find_cached_render(path, page_no, cache_key=cache_key, min_long_edge=long_edge)
    if cached is not None:
        return cached
    if not os.path.exists(path):
        return None

    fail_key = (path, int(page_no), cache_key, int(long_edge))
    if time.monotonic() < _failed_until.get(fail_key, 0.0):
        return None

    with _com_slot(hi_priority, priority):  # COM 串行单槽（预览优先抢占缩略图）
        if (
            not existing_session_only
            and getattr(_state, "app", None) is None
            and _powerpoint_active(force=True)
        ):
            if hi_priority and use_snapshot:
                # Never open even an owned snapshot inside the user's visible
                # PowerPoint process.  If COM rejects Close, that snapshot can
                # remain in their taskbar and recreate the original pollution.
                rendered = _render_page_compat(
                    path,
                    int(page_no),
                    cache_key,
                    int(long_edge),
                    out,
                )
                if rendered is not None:
                    _failed_until.pop(fail_key, None)
                    return rendered
            log.info("PowerPoint is active and isolated compatibility preview was unavailable")
            return None
        try:
            if existing_session_only:
                # Low-CPU prefetch may reuse the exact safe-snapshot session opened
                # by a preceding user preview, but must never start/attach to
                # PowerPoint or open another presentation in the background.
                open_path = getattr(_state, "snapshot_path", None)
                pres = getattr(_state, "pres", None)
                if not (
                    use_snapshot
                    and pres is not None
                    and open_path
                    and os.path.exists(open_path)
                    and getattr(_state, "snapshot_src", None) == path
                    and getattr(_state, "snapshot_key", None) == cache_key
                    and getattr(_state, "pres_path", None) == open_path
                    and getattr(_state, "pres_key", None) == cache_key
                ):
                    return None
            else:
                app = _get_app()
                open_path = path
                if use_snapshot:
                    open_path = _snapshot_for_render(path, cache_key)
                    if open_path is None:
                        return None
                pres = _open_pres(app, open_path, cache_key)  # 复用已打开的同文件，免重复 Open
            if page_no < 1 or page_no > int(pres.Slides.Count):
                return None
            # 按 slide 实际宽高比算输出像素（宽高比随 pres 缓存，避免每页重取）
            width = long_edge
            height = max(1, int(round(width * getattr(_state, "pres_ratio", 9 / 16))))
            pres.Slides(page_no).Export(str(out), "PNG", width, height)
            if out.exists() and out.stat().st_size > 0:
                _failed_until.pop(fail_key, None)
                return out
            return None
        except PowerPointSessionBusy as e:
            # This is an expected safety decision, not a broken-file failure;
            # do not poison the page's 90-second retry cache.
            if hi_priority and use_snapshot:
                rendered = _render_page_compat(
                    path,
                    int(page_no),
                    cache_key,
                    int(long_edge),
                    out,
                )
                if rendered is not None:
                    _failed_until.pop(fail_key, None)
                    return rendered
            log.info("preview skipped after isolated fallback: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("render_page failed path=%s page=%s: %s", path, page_no, e)
            _failed_until[fail_key] = time.monotonic() + _FAILED_TTL_SEC
            _close_pres()       # 关掉可能损坏的 pres
            _release_local_app_reference()  # 丢弃损坏的 COM apartment，下次重建
            return None
        # 不再每次 Close——保持打开供同文件翻页复用（shutdown 统一关）


def render_page(
    path: str,
    page_no: int,
    cache_key: str | None = None,
    long_edge: int = 2560,
    hi_priority: bool = False,
    priority: int | None = None,
    use_snapshot: bool = False,
    existing_session_only: bool = False,
    one_shot: bool = False,
) -> Path | None:
    """Render a page, using a child process in packaged GUI builds."""
    if not _ipc_enabled():
        direct_kwargs = {
            "cache_key": cache_key,
            "long_edge": long_edge,
            "hi_priority": hi_priority,
            "priority": priority,
            "use_snapshot": use_snapshot,
        }
        if existing_session_only:
            direct_kwargs["existing_session_only"] = True
        if one_shot:
            try:
                return _render_page_direct(path, page_no, **direct_kwargs)
            finally:
                shutdown()
        return _render_page_direct(path, page_no, **direct_kwargs)

    path = os.path.abspath(path)
    if cache_key is None:
        cache_key = default_cache_key(path)
        if cache_key is None:
            return None
    out = cache_dir() / f"{cache_key}_{page_no}_{long_edge}.png"
    try:
        if out.exists() and out.stat().st_size > 0:
            return out
    except OSError:
        pass
    cached = find_cached_render(path, page_no, cache_key=cache_key, min_long_edge=long_edge)
    if cached is not None:
        return cached
    if not os.path.exists(path):
        return None
    try:
        from . import render_client

        client_kwargs = {
            "cache_key": cache_key,
            "long_edge": long_edge,
            "hi_priority": hi_priority,
            "priority": priority,
            "use_snapshot": use_snapshot,
        }
        if existing_session_only:
            client_kwargs["existing_session_only"] = True
        if one_shot:
            return render_client.render_page_once(path, page_no, **client_kwargs)
        return render_client.render_page(path, page_no, **client_kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("renderer ipc failed path=%s page=%s: %s", path, page_no, e)
        return None


def render_page_once(
    path: str,
    page_no: int,
    cache_key: str | None = None,
    long_edge: int = 2560,
    hi_priority: bool = False,
    priority: int | None = None,
    use_snapshot: bool = False,
) -> Path | None:
    """Render one historical preview and close that presentation atomically."""
    return render_page(
        path,
        page_no,
        cache_key=cache_key,
        long_edge=long_edge,
        hi_priority=hi_priority,
        priority=priority,
        use_snapshot=use_snapshot,
        one_shot=True,
    )


def abort_inflight() -> bool:
    """Abort only the isolated packaged renderer child; never user PowerPoint."""
    if not _ipc_enabled():
        return False
    try:
        from . import render_client

        return bool(render_client.abort_inflight())
    except Exception:  # noqa: BLE001 emergency cleanup must be best-effort
        return False


def shutdown() -> None:
    if _ipc_enabled():
        try:
            from . import render_client

            render_client.shutdown()
        except Exception:  # noqa: BLE001
            pass
    """退出 PowerPoint 实例并释放 COM。应用退出 / 渲染线程结束时调用。"""
    _close_pres()  # 关掉保持打开的 Presentation
    app = getattr(_state, "app", None)
    if app is None:
        _close_process_handle(getattr(_state, "app_owned_handle", None))
        _state.app_owned_pid = None
        _state.app_owned_handle = None
        _invalidate_powerpoint_active_cache()
        return
    # DispatchEx may still reuse a single-instance server, so Application.Quit is
    # delegated to the strict PID + empty + headless proof in
    # _release_local_app_reference.  Any user-visible or document-bearing session is
    # reference-released only and is never closed.
    _release_local_app_reference()


def diagnostic_lines() -> list[str]:
    engine = preview_engine_status()
    engine_line = (
        "preview_engine: "
        f"available={engine['available']} "
        f"source={engine['source']} "
        f"path={engine['path']}"
    )
    if not _ipc_enabled():
        return [
            f"renderer_ipc: enabled=False frozen={bool(getattr(sys, 'frozen', False))}",
            engine_line,
        ]
    try:
        from . import render_client

        return [*render_client.diagnostic_lines(), engine_line]
    except Exception as e:  # noqa: BLE001
        return [
            f"renderer_ipc: enabled=True unavailable ({type(e).__name__}: {e})",
            engine_line,
        ]
