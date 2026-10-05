"""Publish bounded image FILES, preserving encoded bytes through file paste."""
from __future__ import annotations

import json
import os
import struct
import sys
import uuid
from pathlib import Path

from .encoding import ImagePart, MAX_BYTES


def copy_image(image, *, hwnd=0) -> None:
    """Ordinary capture: one original-resolution bitmap, no 50KB conversion."""
    from .encoding import encode, MAX_PIXELS
    if image.isNull() or image.width()*image.height() > MAX_PIXELS:
        raise ValueError('截图为空或过大，请缩小截图区域')
    if sys.platform == 'win32':
        import win32clipboard as cb
        import win32con
        from ..native_clipboard import opened_clipboard
        png = encode(image,'PNG')
        bitmap = encode(image,'BMP')
        with opened_clipboard(hwnd):
            cb.EmptyClipboard()
            cb.SetClipboardData(cb.RegisterClipboardFormat('PNG'),png)
            cb.SetClipboardData(win32con.CF_DIB,bitmap[14:])
    else:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setImage(image)


def store_parts(parts: list[ImagePart], root: Path) -> list[Path]:
    if not parts or any(p.size > MAX_BYTES or p.size == 0 for p in parts):
        raise ValueError("分图尚未满足每张 50 KB 的限制")
    batch = root / ('shot-' + uuid.uuid4().hex)
    batch.mkdir(parents=True, exist_ok=False)
    paths = []
    for index, part in enumerate(parts, 1):
        suffix = '.png' if part.format == 'PNG' else '.jpg'
        path = batch / f'PPT截图-{batch.name[5:13]}-{index:02d}{suffix}'
        with path.open('xb') as output:
            output.write(part.data)
        if path.stat().st_size != part.size:
            raise ValueError("落盘后的图片大小校验失败")
        paths.append(path)
    (batch / 'manifest.json').write_text(json.dumps({
        'schema': 'pptdoctor-screenshot-1',
        'files': [p.name for p in paths],
        'regions': [p.region for p in parts],
        'bytes': [p.size for p in parts],
    }, ensure_ascii=False), encoding='utf-8')
    return paths


def hdrop_payload(paths: list[Path]) -> bytes:
    names = [str(p.absolute()) for p in paths]
    if not names or any('\0' in n for n in names):
        raise ValueError("剪贴板文件列表无效")
    return struct.pack('<IiiII', 20, 0, 0, 0, 1) + ('\0'.join(names) + '\0\0').encode('utf-16-le')


def copy_files(paths: list[Path], *, hwnd=0) -> None:
    if not paths or any(not p.is_file() or not 0 < p.stat().st_size <= MAX_BYTES for p in paths):
        raise ValueError("图片文件丢失或超过 50 KB，请重新处理截图")
    if sys.platform == 'win32':
        import win32clipboard as cb
        import win32con
        from ..native_clipboard import opened_clipboard
        with opened_clipboard(hwnd):
            cb.EmptyClipboard()
            cb.SetClipboardData(win32con.CF_HDROP, hdrop_payload(paths))
            cb.SetClipboardData(cb.RegisterClipboardFormat('Preferred DropEffect'), struct.pack('<I', 1))
            readback = tuple(os.path.normcase(os.path.abspath(p)) for p in cb.GetClipboardData(win32con.CF_HDROP))
            expected = tuple(os.path.normcase(str(p.absolute())) for p in paths)
            if readback != expected:
                raise ValueError("剪贴板文件列表读回校验失败，请点击复制重试")
    else:
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QGuiApplication
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p.absolute())) for p in paths])
        QGuiApplication.clipboard().setMimeData(mime)


def export_files(paths: list[Path], directory: Path) -> list[Path]:
    """Create siblings using exclusive writes; existing user files are preserved."""
    output = []
    for source in paths:
        if not source.is_file():
            raise ValueError("缓存图片已丢失，请重新截图")
        destination = directory / source.name
        if destination.exists():
            if destination.read_bytes() == source.read_bytes():
                output.append(destination)
                continue
            raise ValueError(f"已有同名文件，未覆盖：{destination.name}")
        with destination.open('xb') as target:
            target.write(source.read_bytes())
        output.append(destination)
    return output
