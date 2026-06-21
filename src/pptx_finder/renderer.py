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
import threading
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


@contextlib.contextmanager
def _com_slot(hi_priority: bool):
    """获取 COM 渲染单槽。预览(hi_priority=True)优先：低优先(缩略图)在有预览排队时
    一律让路，且不抢先堆到锁队列上——预览随时能插到下一个执行。"""
    global _busy, _hi_waiting
    with _cv:
        if hi_priority:
            _hi_waiting += 1
        # 等到通道空闲；低优先还需等到没有预览在排队（让预览插队）
        while _busy or (not hi_priority and _hi_waiting > 0):
            _cv.wait()
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


def default_cache_key(path: str) -> str | None:
    """以 路径+mtime+size 派生缓存键；文件变了就换新键、自动失效旧图。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = f"{os.path.abspath(path)}|{st.st_mtime}|{st.st_size}"
    return xxhash.xxh64(raw.encode("utf-8")).hexdigest()


def render_page(
    path: str,
    page_no: int,
    cache_key: str | None = None,
    long_edge: int = 2560,
    hi_priority: bool = False,
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
    if not os.path.exists(path):
        return None

    with _com_slot(hi_priority):  # COM 串行单槽（预览优先抢占缩略图）
        pres = None
        try:
            app = _get_app()
            pres = app.Presentations.Open(path, ReadOnly=1, WithWindow=0)
            if page_no < 1 or page_no > int(pres.Slides.Count):
                return None
            # 按 slide 实际宽高比算输出像素，避免非 16:9 被拉伸
            try:
                sw = float(pres.PageSetup.SlideWidth)
                sh = float(pres.PageSetup.SlideHeight)
                ratio = sh / sw if sw else 9 / 16
            except Exception:  # noqa: BLE001
                ratio = 9 / 16
            width = long_edge
            height = max(1, int(round(width * ratio)))
            pres.Slides(page_no).Export(str(out), "PNG", width, height)
            return out if (out.exists() and out.stat().st_size > 0) else None
        except Exception as e:  # noqa: BLE001
            log.warning("render_page failed path=%s page=%s: %s", path, page_no, e)
            _state.app = None  # 丢弃可能已损坏的 COM 实例，下次重建干净实例
            return None
        finally:
            if pres is not None:
                try:
                    pres.Close()
                except Exception as e:  # noqa: BLE001
                    log.debug("pres.Close failed: %s", e)


def shutdown() -> None:
    """退出 PowerPoint 实例并释放 COM。应用退出 / 渲染线程结束时调用。"""
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
