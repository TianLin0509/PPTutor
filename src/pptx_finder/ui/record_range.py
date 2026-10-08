"""Live, click-through recording outline; its mask leaves captured pixels empty."""
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRegion
from PySide6.QtWidgets import QWidget


class RecordRange(QWidget):
    def __init__(self, owner=None):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle('录制范围 · PPT Doctor')
        self._inner = QRect()
        self._colour = QColor('#e5484d')
        self._state = '准备录制'
        self._inset = False
        self.capture_warning = ''

    def display(self, target, pixel_size=None):
        # Only the exterior ring and the exterior badge belong to this window.
        # No need to hide it for a frame, even without capture-exclusion support.
        self.setGeometry(target.adjusted(-6, -30, 6, 6))
        self._inner = QRect(6, 30, target.width(), target.height())
        self._pixels = pixel_size or (target.width(), target.height())
        self._inset = False
        self.capture_warning = ''
        ring = QRegion(self._inner.adjusted(-4, -4, 4, 4)).subtracted(QRegion(self._inner))
        badge_width = min(172, self.width())
        self._badge = QRect(0, 0, badge_width, 23)
        self.setMask(ring.united(QRegion(self._badge)))
        self.update_state('countdown')
        self.show()

    def ensure_on_screen(self, bounds, *, capture_excluded):
        visible = self.mask().translated(self.pos()).intersected(QRegion(bounds))
        if visible.isEmpty():
            # A full-monitor selection has no exterior pixels on that monitor.
            # Without affinity a thin interior ring is still visible, with an
            # explicit warning in the controls/result; never shrink the capture.
            self._inset = True
            thickness = 4 if capture_excluded else 2
            ring = QRegion(self._inner).subtracted(QRegion(self._inner.adjusted(thickness,thickness,-thickness,-thickness)))
            if capture_excluded:
                self._badge.moveTopLeft(self._inner.topLeft() + QPointF(8,8).toPoint())
                ring = ring.united(QRegion(self._badge))
            else:
                self.capture_warning = '系统不支持隐藏全屏边框，GIF 将保留细边框'
            self.setMask(ring)
            self.update()

    def update_state(self, state):
        self._state, colour = {
            'countdown': ('准备录制', '#2563eb'),
            'recording': ('正在录制', '#e5484d'),
            'paused': ('已暂停', '#b7791f'),
            'saving': ('正在保存', '#2563eb'),
        }.get(state, ('录制范围', '#e5484d'))
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, event):
        if self._inner.isEmpty():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipRegion(self.mask())
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self._colour, 1.5))
        inset = 1.0 if self.capture_warning else 1.5
        rect = (QRectF(self._inner).adjusted(inset,inset,-inset,-inset) if self._inset
                else QRectF(self._inner).adjusted(-1.5,-1.5,1.5,1.5))
        p.drawRect(rect)
        p.setPen(QPen(self._colour, 1.5 if self.capture_warning else 3, Qt.SolidLine, Qt.RoundCap))
        length = min(18, self._inner.width()/3, self._inner.height()/3)
        for x, y, dx, dy in ((rect.left(), rect.top(), 1, 1),
                             (rect.right(), rect.top(), -1, 1),
                             (rect.left(), rect.bottom(), 1, -1),
                             (rect.right(), rect.bottom(), -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx*length, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy*length))
        if self.capture_warning:
            return
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#ffffff'))
        p.drawRoundedRect(self._badge, 7, 7)
        p.setBrush(self._colour)
        p.drawEllipse(8, 9, 5, 5)
        p.setPen(QColor('#344054'))
        font = QFont('Microsoft YaHei'); font.setPixelSize(11)
        p.setFont(font)
        p.drawText(self._badge.adjusted(20, 0, -5, 0), Qt.AlignVCenter,
                   self._state + f' · {self._pixels[0]} × {self._pixels[1]}')
