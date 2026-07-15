"""Non-COM thumbnails for result cards.

Left-side result thumbnails must not start or automate PowerPoint. The order is:
existing render cache -> embedded pptx thumbnail -> Windows Shell thumbnail cache.
"""
from __future__ import annotations

import ctypes
import logging
import os
import zipfile
from ctypes import wintypes
from pathlib import Path

from PySide6.QtGui import QImage

from .config import cache_dir
from . import renderer

log = logging.getLogger(__name__)

_THUMB_EXTS = (".jpeg", ".jpg", ".png", ".wmf", ".emf")
_SHELL_IID_ISHELLITEMIMAGEFACTORY = "{bcc18b79-ba16-442f-80c4-8a59c30c463b}"
_SIIGBF_RESIZETOFIT = 0x00000000
_SIIGBF_BIGGERSIZEOK = 0x00000001
_SIIGBF_THUMBNAILONLY = 0x00000008
_SIIGBF_INCACHEONLY = 0x00000010


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


def _guid(value: str) -> _GUID:
    import uuid

    u = uuid.UUID(value)
    data4 = (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:])
    return _GUID(u.time_low, u.time_mid, u.time_hi_version, data4)


def _thumb_cache_key(path: str) -> str | None:
    return renderer.default_cache_key(path)


def _valid_image(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0 and not QImage(str(path)).isNull()
    except Exception:  # noqa: BLE001
        return False


def embedded_thumbnail(path: str) -> Path | None:
    """Extract the thumbnail stored inside a pptx package, if present."""
    path = os.path.abspath(path)
    cache_key = _thumb_cache_key(path)
    if cache_key is None:
        return None
    for ext in (".png", ".jpg", ".jpeg"):
        cached = cache_dir() / f"{cache_key}_embedded_thumbnail{ext}"
        if _valid_image(cached):
            return cached

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            name = next(
                (
                    n for n in names
                    if n.lower().startswith("docprops/thumbnail")
                    and Path(n).suffix.lower() in _THUMB_EXTS
                ),
                "",
            )
            if not name:
                return None
            ext = Path(name).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                return None
            out = cache_dir() / f"{cache_key}_embedded_thumbnail{ext}"
            data = zf.read(name)
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, out)
    except Exception as exc:  # noqa: BLE001
        log.debug("embedded thumbnail failed path=%s: %s", path, exc)
        return None
    return out if _valid_image(out) else None


def shell_thumbnail(path: str, *, long_edge: int = 480, cache_only: bool = True) -> Path | None:
    """Ask Windows Shell for a thumbnail without using PowerPoint COM directly.

    By default this only uses the Shell cache. This avoids waking a registered
    thumbnail provider that could in turn start Office.
    """
    if os.name != "nt":
        return None
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None
    cache_key = _thumb_cache_key(path)
    if cache_key is None:
        return None
    long_edge = max(64, min(int(long_edge), 1024))
    out = cache_dir() / f"{cache_key}_shell_thumbnail_{long_edge}.png"
    if _valid_image(out):
        return out

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    gdi32 = ctypes.windll.gdi32
    iid = _guid(_SHELL_IID_ISHELLITEMIMAGEFACTORY)
    factory = ctypes.c_void_p()
    initialized = False
    hr = ole32.CoInitialize(None)
    if hr >= 0:
        initialized = True
    hbmp = wintypes.HBITMAP()
    try:
        hr = shell32.SHCreateItemFromParsingName(
            ctypes.c_wchar_p(path),
            None,
            ctypes.byref(iid),
            ctypes.byref(factory),
        )
        if hr < 0 or not factory.value:
            return None
        vtbl = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_image = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            _SIZE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HBITMAP),
        )(vtbl[3])
        flags = _SIIGBF_RESIZETOFIT | _SIIGBF_BIGGERSIZEOK | _SIIGBF_THUMBNAILONLY
        if cache_only:
            flags |= _SIIGBF_INCACHEONLY
        hr = get_image(factory, _SIZE(long_edge, long_edge), flags, ctypes.byref(hbmp))
        if hr < 0 or not hbmp.value:
            return None
        img = QImage.fromHBITMAP(int(hbmp.value))
        if img.isNull():
            return None
        tmp = out.with_suffix(out.suffix + ".tmp")
        if not img.save(str(tmp), "PNG"):
            return None
        os.replace(tmp, out)
        return out if _valid_image(out) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("shell thumbnail failed path=%s: %s", path, exc)
        return None
    finally:
        if hbmp.value:
            try:
                gdi32.DeleteObject(hbmp)
            except Exception:  # noqa: BLE001
                pass
        if factory.value:
            try:
                vtbl = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
                release(factory)
            except Exception:  # noqa: BLE001
                pass
        if initialized:
            try:
                ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


def find_non_com_thumbnail(path: str, page_no: int, *, long_edge: int = 480) -> Path | None:
    """Return a thumbnail path without starting or automating PowerPoint."""
    try:
        cached = renderer.find_cached_render(path, page_no, min_long_edge=long_edge)
        if cached is not None:
            return cached
        cached = renderer.find_cached_render(path, page_no, min_long_edge=1)
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001
        pass

    # 内置/Shell 缩略图只代表封面（第 1 页）。命中页 > 1 时拿它当该页缩略图会显示错的页，
    # 反而误导（结果卡还标着 “P{n}”）。仅当命中页本身就是封面时才回退到封面缩略图。
    if int(page_no) <= 1:
        embedded = embedded_thumbnail(path)
        if embedded is not None:
            return embedded
        return shell_thumbnail(path, long_edge=long_edge, cache_only=True)

    return None
