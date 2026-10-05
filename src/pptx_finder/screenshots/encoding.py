"""Bound each encoded image without downscaling the captured text."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from threading import Event

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage, QImageWriter, QPainter

from .fidelity import visually_close
from .splitting import split_region

MAX_BYTES = 50_000  # Decimal KB: stricter than a 50 KiB upload limit.
MAX_PIXELS = 24_000_000
MAX_PARTS = 16
MIN_JPEG_QUALITY = 80


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
        raise ValueError("无法创建图片编码缓冲区")
    writer = QImageWriter(buffer, fmt.encode('ascii'))
    writer.setOptimizedWrite(True)
    if fmt == 'PNG':
        writer.setCompression(100)
    else:
        writer.setQuality(quality)
    if not writer.write(image):
        raise ValueError(f"图片编码失败：{writer.errorString()}")
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


def _smallest_safe(tile: QImage, limit: int, cancelled: Event):
    best = None
    # Try JPEG first, beginning with stronger compression. Accept a smaller
    # result only after native-pixel appearance/edge checks; never resize text.
    for quality in (MIN_JPEG_QUALITY, 85, 90, 95):
        if cancelled.is_set():
            raise CaptureCancelled()
        data = encode(tile, 'JPEG', quality)
        if len(data) <= limit and (best is None or len(data) < len(best[0])):
            if visually_close(tile, QImage.fromData(data)):
                best = data, 'JPEG', quality
    if cancelled.is_set():
        raise CaptureCancelled()
    lossless = encode(tile, 'PNG')
    if len(lossless) <= limit and (best is None or len(lossless) < len(best[0])):
        best = lossless, 'PNG', None
    return best


def compress(
    image: QImage, *, max_bytes: int = MAX_BYTES, max_parts: int = MAX_PARTS,
    cancelled: Event | None = None, progress: Callable[[str], None] | None = None,
) -> list[ImagePart]:
    if image.isNull() or image.width() * image.height() > MAX_PIXELS:
        raise ValueError("截图为空或超过 2400 万像素，请缩小截图区域")
    if not 2_000 <= max_bytes <= 1_000_000 or not 1 <= max_parts <= 16:
        raise ValueError("图片大小或分图上限无效")
    cancelled = cancelled or Event()
    image = _opaque(image)
    image.setDevicePixelRatio(1.0)
    pending = [image.rect()]
    parts: list[ImagePart] = []
    while pending:
        if cancelled.is_set():
            raise CaptureCancelled()
        rect = pending.pop(0)
        tile = image.copy(rect)
        if progress:
            progress(f"正在压缩 · 已完成 {len(parts)} 张，待处理 {len(pending) + 1} 块")
        result = _smallest_safe(tile, max_bytes, cancelled)
        if result is not None:
            data, fmt, quality = result
            # A decoded, full-resolution image and an actual byte count are
            # required before calling a part upload-ready.
            check = QImage.fromData(data)
            if check.isNull() or check.size() != tile.size():
                raise ValueError("编码后图片校验失败")
            parts.append(ImagePart(data, fmt, (rect.x(), rect.y(), rect.width(), rect.height()), quality))
        else:
            if len(parts) + len(pending) + 2 > max_parts:
                raise ValueError(f"需要超过 {max_parts} 张才能保留清晰度，请缩小截图区域后重试")
            pending[0:0] = list(split_region(image, rect))
    if cancelled.is_set():
        raise CaptureCancelled()
    return parts
