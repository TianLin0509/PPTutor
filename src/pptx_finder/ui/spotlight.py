"""引导交互基建：聚光灯遮罩 + 呼吸高亮 + reduced-motion 适配。

全局复用的「运行中引导」零件，供首次 coachmark / 版本存在感 / 入口发现等场景调用。
纪律（业界规范）：呼吸 ≤3 周期自停、同屏只动一处、动效 200–300ms、
尊重系统「减弱动态效果」（关则降级为静态高亮，绝不无限循环）。
"""
from __future__ import annotations

import sys

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, Qt, QTimer,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QWidget,
)


def animations_enabled() -> bool:
    """系统是否允许动画（尊重 Windows「显示动画效果」开关 / reduced-motion）。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        val = ctypes.c_int(1)
        spi_get_client_area_animation = 0x1042
        ctypes.windll.user32.SystemParametersInfoW(
            spi_get_client_area_animation, 0, ctypes.byref(val), 0)
        return bool(val.value)
    except Exception:  # noqa: BLE001
        return True


def attention_pulse(widget, *, color: str = "#0A84FF", duration_ms: int = 1200,
                    cycles: int = 3, min_blur: int = 4, max_blur: int = 26):
    """让 widget 呼吸高亮 cycles 个周期后自动停。

    reduced-motion 开启时降级为静态高亮 0.8s。同一 widget 重复调用会先停上一个。
    返回 QPropertyAnimation（动画态）或 None（静态/降级态）。
    """
    old = getattr(widget, "_pulse_anim", None)
    if old is not None:
        try:
            old.stop()
        except Exception:  # noqa: BLE001
            pass

    anim_on = animations_enabled()
    eff = QGraphicsDropShadowEffect(widget)
    eff.setColor(QColor(color))
    eff.setOffset(0, 0)  # 0 偏移 = 四周均匀光晕
    eff.setBlurRadius(min_blur if anim_on else max_blur)
    widget.setGraphicsEffect(eff)

    def _cleanup():
        try:
            if widget.graphicsEffect() is eff:  # 仅卸载自己，别误删后续设置的
                widget.setGraphicsEffect(None)
            widget._pulse_anim = None
        except RuntimeError:
            pass  # widget 已被销毁（C++ 对象先于回调释放）——呼吸期间关窗等

    if not anim_on:
        QTimer.singleShot(800, _cleanup)
        widget._pulse_anim = None
        return None

    anim = QPropertyAnimation(eff, b"blurRadius", widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(min_blur)
    anim.setKeyValueAt(0.5, max_blur)  # 中点最亮
    anim.setEndValue(min_blur)
    anim.setEasingCurve(QEasingCurve.InOutSine)  # 正弦 = 最自然的呼吸
    anim.setLoopCount(cycles)
    anim.finished.connect(_cleanup)
    anim.start()
    widget._pulse_anim = anim  # 防 GC
    return anim


class SpotlightOverlay(QWidget):
    """全屏半透明遮罩：在 target 控件处挖洞高亮 + 旁边带箭头气泡引导。

    覆盖在父窗（通常 centralWidget）之上、随父 resize。点任意处关闭（高亮区点击
    会放行真实控件——遮罩关掉后下次点击即落到控件）。仅用于「一次性」引导，不常驻。
    """

    def __init__(self, parent, target, text: str, *, tok: dict | None = None,
                 on_dismiss=None, pad: int = 8, radius: int = 10):
        super().__init__(parent)
        self._target = target
        self._text = text
        self._tok = tok or {}
        self._on_dismiss = on_dismiss
        self._pad = pad
        self._radius = radius
        self.setObjectName("spotlightOverlay")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        if parent is not None:
            self.setGeometry(parent.rect())

        self._op = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._op)
        self.show()
        self.raise_()
        if animations_enabled():
            self._op.setOpacity(0.0)
            fade = QPropertyAnimation(self._op, b"opacity", self)
            fade.setDuration(250)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.OutCubic)
            fade.start()
            self._fade = fade
        else:
            self._op.setOpacity(1.0)

    def _hole(self) -> QRect:
        t = self._target
        if t is None or not t.isVisible() or self.parentWidget() is None:
            return QRect()
        tl = t.mapTo(self.parentWidget(), QPoint(0, 0))
        r = QRect(tl, t.size())
        return r.adjusted(-self._pad, -self._pad, self._pad, self._pad)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        full = QPainterPath()
        full.addRect(QRectF(self.rect()))
        hole_r = self._hole()
        acc = self._tok.get("acc", "#0A84FF")
        if hole_r.isValid() and not hole_r.isEmpty():
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(hole_r), self._radius, self._radius)
            p.fillPath(full.subtracted(hole), QColor(0, 0, 0, 150))
            p.setPen(QPen(QColor(acc), 2))
            p.drawRoundedRect(hole_r, self._radius, self._radius)
            self._draw_bubble(p, hole_r, acc)
        else:
            p.fillPath(full, QColor(0, 0, 0, 150))

    def _draw_bubble(self, p: QPainter, hole: QRect, acc: str) -> None:
        bw, gap, arrow = 290, 13, 9
        fm = p.fontMetrics()
        tr = fm.boundingRect(QRect(0, 0, bw - 32, 600), Qt.TextWordWrap, self._text)
        bh = tr.height() + 30
        below = hole.bottom() + gap + arrow
        if below + bh > self.height() - 12:  # 下方放不下 → 放上方
            by = hole.top() - gap - arrow - bh
            arrow_up = False
        else:
            by = below
            arrow_up = True
        bx = max(14, min(hole.center().x() - bw // 2, self.width() - bw - 14))
        bubble = QRect(bx, by, bw, bh)
        win = self._tok.get("win", "#FFFFFF")
        ink = self._tok.get("ink1", "#1D1D1F")

        path = QPainterPath()
        path.addRoundedRect(QRectF(bubble), 12, 12)
        p.fillPath(path, QColor(win))
        p.setPen(QPen(QColor(acc), 1.4))
        p.drawRoundedRect(bubble, 12, 12)

        cx = max(bx + 18, min(hole.center().x(), bx + bw - 18))
        p.setBrush(QColor(win))
        p.setPen(Qt.NoPen)
        if arrow_up:
            tri = QPolygonF([QPointF(cx - 8, by), QPointF(cx + 8, by),
                             QPointF(cx, hole.bottom() + gap)])
        else:
            tri = QPolygonF([QPointF(cx - 8, by + bh), QPointF(cx + 8, by + bh),
                             QPointF(cx, hole.top() - gap)])
        p.drawPolygon(tri)

        p.setPen(QColor(ink))
        p.drawText(bubble.adjusted(16, 15, -16, -15),
                   int(Qt.TextWordWrap | Qt.AlignTop), self._text)

    def mousePressEvent(self, _e):  # noqa: N802
        self._close()

    def _close(self) -> None:
        if self._on_dismiss:
            try:
                self._on_dismiss()
            except Exception:  # noqa: BLE001
                pass
        self.hide()
        self.deleteLater()

    def resizeEvent(self, _e):  # noqa: N802
        if self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())
