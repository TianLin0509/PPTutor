"""预览渲染：PowerPoint COM 导出指定页为 PNG，带磁盘缓存。

隔离：用 DispatchEx 启动独立 PowerPoint 实例，不干扰用户已打开的 PowerPoint。
线程：COM 为单线程套间，调用线程需 CoInitialize（本模块惰性处理）。
     UI 侧应在一个专用渲染线程里串行调用，避免并发与界面卡顿。
失败策略：任何异常都返回 None，由 UI 显示「无法预览，可直接打开」兜底。
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
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
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("PowerPoint.Application")
    _state.app = app
    return app


def _open_pres(app, path: str, cache_key: str):
    """复用上次打开的 Presentation：同文件同内容直接返回，免重复 Open（翻页 / 多次预览
    同一稿，最耗时的就是 Open，大稿尤甚）；换文件或文件已变（cache_key 含 mtime+size）
    则关旧开新。ReadOnly 打开不锁文件写入（实测），不影响恢复/导出覆盖该文件。"""
    if (getattr(_state, "pres", None) is not None
            and getattr(_state, "pres_path", None) == path
            and getattr(_state, "pres_key", None) == cache_key):
        return _state.pres
    _close_pres()
    pres = app.Presentations.Open(path, ReadOnly=1, WithWindow=0)
    _state.pres = pres
    _state.pres_path = path
    _state.pres_key = cache_key
    try:
        sw = float(pres.PageSetup.SlideWidth)
        sh = float(pres.PageSetup.SlideHeight)
        _state.pres_ratio = sh / sw if sw else 9 / 16
    except Exception:  # noqa: BLE001
        _state.pres_ratio = 9 / 16
    return pres


def _close_pres():
    pres = getattr(_state, "pres", None)
    if pres is not None:
        try:
            pres.Close()
        except Exception:  # noqa: BLE001
            pass
    _state.pres = None
    _state.pres_path = None
    _state.pres_key = None


def default_cache_key(path: str) -> str | None:
    """以 路径+mtime+size 派生缓存键；文件变了就换新键、自动失效旧图。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = f"{os.path.abspath(path)}|{st.st_mtime}|{st.st_size}"
    return xxhash.xxh64(raw.encode("utf-8")).hexdigest()


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
        try:
            app = _get_app()
            pres = _open_pres(app, path, cache_key)  # 复用已打开的同文件，免重复 Open
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
        except Exception as e:  # noqa: BLE001
            log.warning("render_page failed path=%s page=%s: %s", path, page_no, e)
            _failed_until[fail_key] = time.monotonic() + _FAILED_TTL_SEC
            _close_pres()       # 关掉可能损坏的 pres
            _state.app = None   # 丢弃可能已损坏的 COM 实例，下次重建干净实例
            return None
        # 不再每次 Close——保持打开供同文件翻页复用（shutdown 统一关）


def render_page(
    path: str,
    page_no: int,
    cache_key: str | None = None,
    long_edge: int = 2560,
    hi_priority: bool = False,
    priority: int | None = None,
) -> Path | None:
    """Render a page, using a child process in packaged GUI builds."""
    if not _ipc_enabled():
        return _render_page_direct(
            path,
            page_no,
            cache_key=cache_key,
            long_edge=long_edge,
            hi_priority=hi_priority,
            priority=priority,
        )

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

        return render_client.render_page(
            path,
            page_no,
            cache_key=cache_key,
            long_edge=long_edge,
            hi_priority=hi_priority,
            priority=priority,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("renderer ipc failed path=%s page=%s: %s", path, page_no, e)
        return None


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
        return
    try:
        app.Quit()
    except Exception as e:  # noqa: BLE001
        log.debug("app.Quit failed: %s", e)
    _state.app = None
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def diagnostic_lines() -> list[str]:
    if not _ipc_enabled():
        return [f"renderer_ipc: enabled=False frozen={bool(getattr(sys, 'frozen', False))}"]
    try:
        from . import render_client

        return render_client.diagnostic_lines()
    except Exception as e:  # noqa: BLE001
        return [f"renderer_ipc: enabled=True unavailable ({type(e).__name__}: {e})"]
