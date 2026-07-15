"""Small, low-CPU progress indicator for full-disk indexing."""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QProgressBar


class IndexActivityBar(QProgressBar):
    """A calm progress bar with a custom indeterminate state.

    The native Windows busy progress animation is visually noisy and varies by
    OS theme. This widget paints a single soft sweep at 20 FPS, only while an
    actual scan is running and the widget is visible. Determinate indexing does
    not animate continuously. The public range/value API intentionally matches
    ``QProgressBar`` so existing callers and diagnostics remain compatible.
    """

    _TICK_MS = 50

    def __init__(self, parent=None, *, motion_allowed: bool | None = None) -> None:
        super().__init__(parent)
        if motion_allowed is None:
            from .spotlight import animations_enabled

            motion_allowed = animations_enabled()
        self._motion_allowed = bool(motion_allowed)
        self._phase = 0.0
        self._accent = QColor("#22D3EE")
        self._track = QColor(self._accent)
        self._track.setAlpha(34)
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.timeout.connect(self._advance)
        self.setTextVisible(False)
        self.setFixedHeight(8)
        self.setMinimumWidth(140)
        self.setMaximumWidth(168)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(156, 8)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(120, 8)

    def set_accent_color(self, color: str | QColor) -> None:
        accent = QColor(color)
        if not accent.isValid():
            return
        self._accent = accent
        self._track = QColor(accent)
        self._track.setAlpha(34)
        self.update()

    def is_indeterminate(self) -> bool:
        return self.minimum() == 0 and self.maximum() == 0

    def animation_active(self) -> bool:
        return self._timer.isActive()

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802 - Qt API
        super().setRange(minimum, maximum)
        if self.is_indeterminate():
            self._start_if_needed()
        else:
            self._timer.stop()
        self.update()

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt API
        super().setValue(value)
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._start_if_needed()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)

    def _start_if_needed(self) -> None:
        if (
            self._motion_allowed
            and self.isVisible()
            and self.is_indeterminate()
            and not self._timer.isActive()
        ):
            self._timer.start()

    def _advance(self) -> None:
        if not self.isVisible() or not self.is_indeterminate():
            self._timer.stop()
            return
        self._phase = (self._phase + self._TICK_MS / 1350.0) % 1.0
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        radius = bounds.height() / 2.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._track)
        painter.drawRoundedRect(bounds, radius, radius)

        clip = QPainterPath()
        clip.addRoundedRect(bounds, radius, radius)
        painter.setClipPath(clip)
        if self.is_indeterminate():
            segment_width = max(34.0, bounds.width() * 0.32)
            if self._motion_allowed:
                # Cosine easing keeps the sweep calm at the edges without a
                # second decorative element or a high-frequency timer.
                eased = 0.5 - 0.5 * math.cos(math.pi * self._phase)
                x = bounds.left() - segment_width + eased * (
                    bounds.width() + 2.0 * segment_width
                )
            else:
                x = bounds.left() + (bounds.width() - segment_width) / 2.0
            fill = QRectF(x, bounds.top(), segment_width, bounds.height())
            gradient = QLinearGradient(fill.left(), 0, fill.right(), 0)
            edge = QColor(self._accent)
            edge.setAlpha(0)
            center = QColor(self._accent)
            center.setAlpha(225)
            gradient.setColorAt(0.0, edge)
            gradient.setColorAt(0.5, center)
            gradient.setColorAt(1.0, edge)
            painter.setBrush(gradient)
            painter.drawRect(fill)
        else:
            span = self.maximum() - self.minimum()
            ratio = (
                (self.value() - self.minimum()) / span
                if span > 0 else 0.0
            )
            width = bounds.width() * max(0.0, min(1.0, ratio))
            if width > 0:
                painter.setBrush(self._accent)
                painter.drawRoundedRect(
                    QRectF(bounds.left(), bounds.top(), width, bounds.height()),
                    radius,
                    radius,
                )
