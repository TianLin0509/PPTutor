"""central 自绘极光底。

主窗 central 用 AuroraCentral 替代裸 QWidget：paintEvent 读主窗当前主题 token
（`_tok["blobs"]` 极光光团 + `_tok["appbg"]` 底色），画径向渐变光团，半透明玻璃面板透出。
objectName 仍为 "central"（QSS `QWidget#central { background: transparent }` 让自绘可见）。

方案 D（2026-11-25）：晨雾 / 暗夜两套主题也走这里——只画静态光团，不做实时模糊、不加阴影效果。
画法抽成 paint_aurora()，欢迎层和主题缩略卡复用同一公式，所见即所得。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget


def paint_aurora(p: QPainter, r: QRectF, tok: dict) -> bool:
    """在 r 里铺底色并逐个画光团：圆心 (宽·fx, 高·fy)，半径 宽·fr·0.55，颜色 α → 0。

    没有底色（异常主题）时什么也不画并返回 False，交给 QSS / 默认背景。
    """
    appbg = tok.get("appbg")
    if not appbg:
        return False
    p.fillRect(r, QColor(appbg))
    for fx, fy, fr, col in (tok.get("blobs") or []):
        c = QPointF(r.left() + r.width() * fx, r.top() + r.height() * fy)
        g = QRadialGradient(c, r.width() * fr * 0.55)
        g.setColorAt(0, QColor(*col))
        end = QColor(*col)
        end.setAlpha(0)
        g.setColorAt(1, end)
        p.fillRect(r, g)
    return True


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
        if not tok.get("appbg"):
            # 无极光数据（异常主题）→ 交给 QSS / 默认背景，不自绘
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        paint_aurora(p, QRectF(self.rect()), tok)
        p.end()
