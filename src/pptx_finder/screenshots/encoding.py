"""Fast native encoding first; resolution reduction is an explicit second action."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from threading import Event
import math

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QColor, QImage, QImageWriter, QPainter

from .splitting import split_region

MAX_BYTES = 50_000
MAX_PIXELS = 24_000_000
MAX_PARTS = 8
MIN_JPEG_QUALITY = 85


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    format: str
    region: tuple[int, int, int, int]
    quality: int | None

    @property
    def size(self) -> int:
        return len(self.data)


class CaptureCancelled(Exception):
    pass


def encode(image: QImage, fmt: str, quality: int = 90) -> bytes:
    buffer = QBuffer()
    if not buffer.open(QIODevice.WriteOnly):
        raise ValueError('无法创建图片编码缓冲区')
    writer = QImageWriter(buffer, fmt.encode('ascii'))
    writer.setOptimizedWrite(True)
    if fmt == 'PNG':
        writer.setCompression(100)
    else:
        writer.setQuality(quality)
    if not writer.write(image):
        raise ValueError(f'图片编码失败：{writer.errorString()}')
    return bytes(buffer.data())


def _opaque(image: QImage) -> QImage:
    if not image.hasAlphaChannel():
        return image.convertToFormat(QImage.Format_RGB888)
    canvas = QImage(image.size(), QImage.Format_RGB888)
    canvas.fill(QColor('white'))
    painter = QPainter(canvas)
    painter.drawImage(0, 0, image)
    painter.end()
    return canvas


def _check(cancelled):
    if cancelled.is_set():
        raise CaptureCancelled()


def _validate(image, max_bytes, max_parts=MAX_PARTS):
    if image.isNull() or image.width() * image.height() > MAX_PIXELS:
        raise ValueError('截图为空或超过 2400 万像素，请缩小截图区域')
    if not 2_000 <= max_bytes <= 1_000_000 or not 1 <= max_parts <= 16:
        raise ValueError('图片大小或分图上限无效')


def _encode_part(image, rect, limit, cancelled):
    _check(cancelled)
    tile = image.copy(rect)
    png = encode(tile, 'PNG')
    if len(png) <= limit:
        data, fmt, quality = png, 'PNG', None
    else:
        _check(cancelled)
        jpeg = encode(tile, 'JPEG', MIN_JPEG_QUALITY)
        data, fmt, quality = ((jpeg, 'JPEG', MIN_JPEG_QUALITY)
                              if len(jpeg) < len(png) else (png, 'PNG', None))
    _check(cancelled)
    return ImagePart(data, fmt, (rect.x(), rect.y(), rect.width(), rect.height()), quality)


def compress(image: QImage, *, max_bytes: int = MAX_BYTES, max_parts: int = MAX_PARTS,
             cancelled: Event | None = None, progress: Callable[[str], None] | None = None) -> list[ImagePart]:
    """Preserve resolution, prefer gutters, stop at the user's count even if oversized."""
    _validate(image, max_bytes, max_parts)
    cancelled = cancelled or Event()
    _check(cancelled)
    image = _opaque(image)
    image.setDevicePixelRatio(1.0)
    parts = [_encode_part(image, image.rect(), max_bytes, cancelled)]
    unsplittable = set()
    while len(parts) < max_parts:
        _check(cancelled)
        candidates = [(p.size, i) for i, p in enumerate(parts)
                      if p.size > max_bytes and p.region not in unsplittable]
        if not candidates:
            break
        _, index = max(candidates)
        part = parts[index]
        if progress:
            progress(f'正在压缩 · 最多 {max_parts} 张 · 目标 {max_bytes / 1000:g} KB / 张')
        try:
            regions = split_region(image, QRect(*part.region))
        except ValueError:
            unsplittable.add(part.region)
            continue
        parts[index:index + 1] = [_encode_part(image, rect, max_bytes, cancelled) for rect in regions]
    _check(cancelled)
    return parts


def force_limit(image: QImage, parts: list[ImagePart], *, max_bytes: int = MAX_BYTES,
                cancelled: Event | None = None, progress: Callable[[str], None] | None = None) -> list[ImagePart]:
    """Keep layout; re-encode original pixels before reducing resolution."""
    _validate(image, max_bytes)
    if not parts or len(parts) > 16:
        raise ValueError('请先生成小图，再强制压缩')
    cancelled = cancelled or Event()
    _check(cancelled)
    source = _opaque(image)
    source.setDevicePixelRatio(1.0)
    result = []
    for index, part in enumerate(parts, 1):
        _check(cancelled)
        rect = QRect(*part.region)
        if rect.isEmpty() or not source.rect().contains(rect):
            raise ValueError('分图区域超出原图')
        if part.size <= max_bytes:
            result.append(part)
            continue
        if progress:
            progress(f'强制压缩 · 第 {index} / {len(parts)} 张 · {max_bytes / 1000:g} KB / 张')
        tile = source.copy(rect)
        candidate = _encode_part(source, rect, max_bytes, cancelled)
        if candidate.size <= max_bytes:
            result.append(candidate)
            continue
        w, h = tile.width(), tile.height()
        data = candidate.data
        while len(data) > max_bytes:
            _check(cancelled)
            ratio = min(.9, math.sqrt(max_bytes / len(data)) * .92)
            new_w, new_h = max(1, int(w * ratio)), max(1, int(h * ratio))
            if (new_w, new_h) == (w, h):
                raise ValueError('无法满足大小上限，请提高每张大小限制')
            w, h = new_w, new_h
            resized = tile.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            data = encode(resized, 'JPEG', MIN_JPEG_QUALITY)
        _check(cancelled)
        decoded = QImage.fromData(data)
        if decoded.isNull() or (decoded.width(), decoded.height()) != (w, h):
            raise ValueError('压缩后的图片校验失败')
        result.append(ImagePart(data, 'JPEG', part.region, MIN_JPEG_QUALITY))
    _check(cancelled)
    return result
