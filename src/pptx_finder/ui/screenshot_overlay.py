"""Per-monitor frozen frame selection; capture stays in physical pixels."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget


def crop_physical(image: QImage, logical: QRect, bounds: QRect) -> QImage:
    area = logical.normalized().intersected(QRect(0, 0, bounds.width(), bounds.height()))
    if area.width() < 4 or area.height() < 4 or bounds.width() <= 0 or bounds.height() <= 0:
        return QImage()
    sx, sy = image.width() / bounds.width(), image.height() / bounds.height()
    left, top = round(area.x() * sx), round(area.y() * sy)
    right = round((area.x() + area.width()) * sx)
    bottom = round((area.y() + area.height()) * sy)
    result = image.copy(left, top, right - left, bottom - top)
    result.setDevicePixelRatio(1.0)
    return result


class ScreenshotOverlay(QWidget):
    selected = Signal(QImage)
    cancelled = Signal()

    def __init__(self, image: QImage, geometry: QRect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._image = image
        self._origin: QPoint | None = None
        self._selection = QRect()
        self.setGeometry(geometry)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(self.rect(), self._image)
        painter.fillRect(self.rect(), QColor(10, 18, 28, 95))
        if not self._selection.isEmpty():
            area = self._selection.normalized().intersected(self.rect())
            painter.setClipRect(area)
            painter.drawImage(self.rect(), self._image)
            painter.setClipping(False)
            painter.setPen(QPen(QColor('#4b8bf4'), 2))
            painter.drawRect(area)
        painter.fillRect(QRect(20, 20, 470, 42), QColor('#ffffff'))
        painter.setPen(QColor('#242b35'))
        painter.drawText(QRect(32, 20, 450, 42), Qt.AlignVCenter, '拖动框选 PPT 区域 · 松开完成 · Esc / 右键取消')

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.LeftButton:
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._selection = self._drag_rect(event.position().toPoint())
            self.update()

    def _drag_rect(self, endpoint: QPoint) -> QRect:
        # QRect.normalized() uses inclusive endpoints, which would trim two
        # pixels on a reverse drag. Represent a half-open pixel area explicitly.
        start = self._origin
        return QRect(min(start.x(), endpoint.x()), min(start.y(), endpoint.y()),
                     abs(endpoint.x() - start.x()), abs(endpoint.y() - start.y()))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._origin is not None:
            self._selection = self._drag_rect(event.position().toPoint())
            self._origin = None
            result = crop_physical(self._image, self._selection, self.rect())
            if not result.isNull():
                self.selected.emit(result)
            else:
                self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
