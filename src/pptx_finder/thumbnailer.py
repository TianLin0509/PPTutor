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

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from .config import cache_dir
from . import renderer
from .parser import parse_pptx_page

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


def text_page_preview(path: str, page_no: int, *, long_edge: int = 800) -> Path | None:
    """Draw a clearly labelled text-first preview without starting Office.

    This is not presented as the original slide layout.  It is a fast safety
    net for the core use case: after a content search, the user can still read
    the matched page while a high-fidelity renderer is busy or unavailable.
    Only the requested slide is parsed, keeping CPU and latency bounded.
    """
    path = os.path.abspath(path)
    cache_key = _thumb_cache_key(path)
    if cache_key is None:
        return None
    page_no = int(page_no)
    edge = max(480, min(int(long_edge), 1280))
    out = cache_dir() / f"{cache_key}_safe_{page_no}_{edge}.png"
    if _valid_image(out):
        return out

    page = parse_pptx_page(path, page_no)
    if page is None:
        return None
    lines = [value.strip() for value in (page.body, page.smartart) if value and value.strip()]
    if page.notes and page.notes.strip():
        lines.append(f"备注\n{page.notes.strip()}")
    raw = "\n".join(lines).replace("\x00", "").strip()
    if not raw:
        raw = "这一页没有可提取的文字；高清原版可用时会自动替换。"
    raw_lines = [line.strip() for line in raw.splitlines() if line.strip()]
    headline = (raw_lines[0] if raw_lines else raw)[:120]
    body = "\n".join(raw_lines[1:]) if len(raw_lines) > 1 else ""
    if len(body) > 1800:
        body = body[:1799].rstrip() + "…"

    width = edge
    height = max(270, int(round(width * 9 / 16)))
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(QColor("#F5F7FA"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        margin = max(24, int(width * 0.045))
        accent = max(5, int(width * 0.007))
        painter.fillRect(QRect(0, 0, accent, height), QColor("#2F80ED"))

        meta_font = QFont()
        meta_font.setPixelSize(max(13, int(width * 0.018)))
        meta_font.setBold(True)
        painter.setFont(meta_font)
        painter.setPen(QColor("#2F80ED"))
        painter.drawText(
            QRect(margin, margin, width - margin * 2, int(height * 0.08)),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"P{page_no}  ·  文字速览",
        )

        title_top = margin + int(height * 0.09)
        title_font = QFont()
        title_font.setPixelSize(max(22, int(width * 0.034)))
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#172033"))
        title_rect = QRect(
            margin,
            title_top,
            width - margin * 2,
            int(height * 0.22),
        )
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            headline,
        )

        body_top = title_top + int(height * 0.23)
        body_font = QFont()
        body_font.setPixelSize(max(15, int(width * 0.022)))
        painter.setFont(body_font)
        painter.setPen(QColor("#3C465A"))
        painter.drawText(
            QRect(margin, body_top, width - margin * 2, height - body_top - margin * 2),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            body or "高清原版可用时会自动替换。",
        )

        hint_font = QFont()
        hint_font.setPixelSize(max(11, int(width * 0.015)))
        painter.setFont(hint_font)
        painter.setPen(QColor("#7B8495"))
        painter.drawText(
            QRect(margin, height - margin - 24, width - margin * 2, 24),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "本图用于快速确认命中内容，不代表原始排版",
        )
    finally:
        painter.end()

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        if not image.save(str(tmp), "PNG"):
            return None
        os.replace(tmp, out)
    except OSError as exc:
        log.debug("text page preview save failed path=%s page=%s: %s", path, page_no, exc)
        return None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return out if _valid_image(out) else None


def find_non_com_page_preview(
    path: str,
    page_no: int,
    *,
    long_edge: int = 800,
) -> Path | None:
    """Return the best immediate page preview without starting Office."""
    existing_or_cover = find_non_com_thumbnail(path, page_no, long_edge=long_edge)
    if existing_or_cover is not None:
        return existing_or_cover
    return text_page_preview(path, page_no, long_edge=long_edge)
