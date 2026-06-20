"""central 自绘极光背景。

主窗 central 用 AuroraCentral 替代裸 QWidget：paintEvent 读主窗当前主题 token
（`_tok["blobs"]` 极光光团 + `_tok["appbg"]` 底色），画径向渐变光团，半透明玻璃面板透出。
objectName 仍为 "central"（QSS `QWidget#central { background: transparent }` 让自绘可见）。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget


class AuroraCentral(QWidget):
    """主窗中央容器：自绘极光底（底色 + 多层径向光团），跟随主窗 `_tok` 变色。

    持有主窗弱引用以在每次 paint 时取当前主题 token；主题切换后主窗调 update() 重绘。
    """

    def __init__(self, win) -> None:
        super().__init__()
        self.setObjectName("central")
        self._win = win

    def _tok(self) -> dict:
        # 主窗 _tok 始终存在（__init__ 先于 _build_ui 赋值）；防御性兜底空 dict。
        return getattr(self._win, "_tok", {}) or {}

    def paintEvent(self, e):  # noqa: N802
        tok = self._tok()
        blobs = tok.get("blobs")
        appbg = tok.get("appbg")
        if not appbg:
            # 无极光数据（异常主题）→ 交给 QSS / 默认背景，不自绘
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.fillRect(r, QColor(appbg))
        for fx, fy, fr, col in (blobs or []):
            c = QPointF(r.width() * fx, r.height() * fy)
            g = QRadialGradient(c, r.width() * fr * 0.55)
            g.setColorAt(0, QColor(*col))
            end = QColor(*col)
            end.setAlpha(0)
            g.setColorAt(1, end)
            p.fillRect(r, g)
        p.end()
