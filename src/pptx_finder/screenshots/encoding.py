"""Bound each encoded image without downscaling the captured text."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from threading import Event

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QColor, QImage, QImageWriter, QPainter

MAX_BYTES = 50_000  # Decimal KB: stricter than a 50 KiB upload limit.
MAX_PIXELS = 24_000_000
MAX_PARTS = 16
MIN_JPEG_QUALITY = 75


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


def _split(image: QImage, rect: QRect) -> tuple[QRect, QRect]:
    # Prefer two horizontal reading bands on a normal PPT. Subsequent cuts
    # alternate when a band would become excessively wide. No pixels are lost.
    horizontal = rect.width() <= rect.height() * 2.4
    length = rect.height() if horizontal else rect.width()
    if length < 32:
        horizontal = not horizontal
        length = rect.height() if horizontal else rect.width()
    if length < 32:
        raise ValueError("截图细节太多，无法在保留原始分辨率的同时继续分图；请缩小截图区域")
    # Find a low-edge cut close to the middle to favour gaps between text rows.
    sample = image.copy(rect).scaled(96, 96, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    scores = []
    for position in range(40, 57):
        score = 0
        for j in range(1, 96):
            c = sample.pixelColor(j, position) if horizontal else sample.pixelColor(position, j)
            prev = sample.pixelColor(j - 1, position) if horizontal else sample.pixelColor(position, j - 1)
            score += abs(c.lightness() - prev.lightness())
        scores.append((score, abs(position - 48), position))
    cut = max(16, min(length - 16, round(min(scores)[2] * length / 96)))
    overlap = min(8, cut // 4, (length - cut) // 4)
    if horizontal:
        return (
            QRect(rect.x(), rect.y(), rect.width(), cut + overlap),
            QRect(rect.x(), rect.y() + cut - overlap, rect.width(), length - cut + overlap),
        )
    return (
        QRect(rect.x(), rect.y(), cut + overlap, rect.height()),
        QRect(rect.x() + cut - overlap, rect.y(), length - cut + overlap, rect.height()),
    )


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
        data = encode(tile, 'PNG')
        fmt, quality = 'PNG', None
        if len(data) > max_bytes:
            fmt = 'JPEG'
            for quality in (95, 90, 85, 80, MIN_JPEG_QUALITY):
                if cancelled.is_set():
                    raise CaptureCancelled()
                data = encode(tile, fmt, quality)
                if len(data) <= max_bytes:
                    break
        if len(data) <= max_bytes:
            # A decoded, full-resolution image and an actual byte count are
            # required before calling a part upload-ready.
            check = QImage.fromData(data)
            if check.isNull() or check.size() != tile.size():
                raise ValueError("编码后图片校验失败")
            parts.append(ImagePart(data, fmt, (rect.x(), rect.y(), rect.width(), rect.height()), quality))
        else:
            if len(parts) + len(pending) + 2 > max_parts:
                raise ValueError(f"需要超过 {max_parts} 张才能保留清晰度，请缩小截图区域后重试")
            pending[0:0] = list(_split(image, rect))
    if cancelled.is_set():
        raise CaptureCancelled()
    return sorted(parts, key=lambda p: (p.region[1], p.region[0]))
