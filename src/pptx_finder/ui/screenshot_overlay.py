"""Per-monitor frozen frame selection; capture stays in physical pixels."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget


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
    small_selected = Signal(QImage)
    scroll_selected = Signal(object, object)
    cancelled = Signal()

    def __init__(self, image: QImage, geometry: QRect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._image = image
        self._origin: QPoint | None = None
        self._selection = QRect()
        self._screen_geometry = QRect(geometry)
        self.setGeometry(geometry)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.toolbar = QWidget(self)
        self.toolbar.setObjectName('captureToolbar')
        self.toolbar.setStyleSheet('#captureToolbar { background: white; border: 1px solid #dfe5ed; border-radius: 6px; } QPushButton { background: white; color: #243249; border: 0; padding: 7px 10px; } QPushButton:hover { background: #eaf1fb; }')
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(4,4,4,4)
        bar.setSpacing(2)
        self.confirm_btn = QPushButton('✓ 完成')
        self.scroll_btn = QPushButton('滚动截图')
        self.small_btn = QPushButton('小图模式')
        self.cancel_btn = QPushButton('× 取消')
        for button in (self.confirm_btn,self.scroll_btn,self.small_btn,self.cancel_btn):
            bar.addWidget(button)
        self.confirm_btn.setToolTip('普通截图：直接复制原图')
        self.small_btn.setToolTip('AI 上传：压缩并按每张 50 KB 分图')
        self.scroll_btn.setToolTip('从当前位置向下滚动，拼成长图')
        self.confirm_btn.clicked.connect(lambda: self._confirm('normal'))
        self.small_btn.clicked.connect(lambda: self._confirm('small'))
        self.scroll_btn.clicked.connect(lambda: self._confirm('scroll'))
        self.cancel_btn.clicked.connect(self.cancelled)
        self.toolbar.hide()

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
        painter.drawText(QRect(32, 20, 450, 42), Qt.AlignVCenter, '框选任意区域 · ✓ 普通截图 · Esc / 右键取消')

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.LeftButton:
            self.toolbar.hide()
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
                self.toolbar.adjustSize()
                width,height = self.toolbar.width(),self.toolbar.height()
                x = max(0,min(self.width()-width,self._selection.right()-width+1))
                y = self._selection.bottom()+8
                if y+height > self.height():
                    y = max(0,self._selection.bottom()-height-8)
                self.toolbar.move(x,y)
                self.toolbar.show()
                self.update()
            else:
                self.update()

    def _confirm(self, mode):
        result = crop_physical(self._image,self._selection,self.rect())
        if result.isNull():
            return
        if mode == 'scroll':
            self.scroll_selected.emit(QRect(self._selection),QRect(self._screen_geometry))
        elif mode == 'small':
            self.small_selected.emit(result)
        else:
            self.selected.emit(result)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key_Return,Qt.Key_Enter):
            self._confirm('normal')
