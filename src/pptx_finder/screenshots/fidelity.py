"""Compare JPEG detail at native pixels; this is a visual guard, not OCR."""
from __future__ import annotations

from PySide6.QtGui import QImage


def visually_close(source: QImage, decoded: QImage) -> bool:
    if decoded.isNull() or source.size() != decoded.size():
        return False
    source = source.convertToFormat(QImage.Format_RGB888)
    decoded = decoded.convertToFormat(QImage.Format_RGB888)
    original, restored = source.constBits(), decoded.constBits()
    sw, dw = source.bytesPerLine(), decoded.bytesPerLine()
    width, height = source.width(), source.height()
    # Full-resolution stratified pixels, including neighbouring pixels so thin
    # text edges are checked without resizing the image or needing NumPy.
    stride = max(1, int((width * height / 12000) ** .5))
    total = count = edge_total = edge_count = weakened = 0
    for y in range(0, height, stride):
        for x in range(0, width, stride):
            si, di = y * sw + x * 3, y * dw + x * 3
            error = sum(abs(original[si+c] - restored[di+c]) for c in range(3)) / 3
            total += error
            count += 1
            if x+1 < width:
                contrast = max(abs(original[si+c] - original[si+3+c]) for c in range(3))
                if contrast >= 35:
                    edge_total += error
                    edge_count += 1
                    recovered = max(abs(restored[di+c] - restored[di+3+c]) for c in range(3))
                    weakened += recovered < contrast * .65
            if y+1 < height:
                contrast = max(abs(original[si+c] - original[si+sw+c]) for c in range(3))
                if contrast >= 35:
                    edge_total += error
                    edge_count += 1
                    recovered = max(abs(restored[di+c] - restored[di+dw+c]) for c in range(3))
                    weakened += recovered < contrast * .65
    return (total / max(count, 1) <= 3.0
            and edge_total / max(edge_count, 1) <= 6.0
            and weakened / max(edge_count, 1) <= .015)
