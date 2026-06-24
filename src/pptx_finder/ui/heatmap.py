"""24×7 修改热力图 widget（火焰色阶 + 峰值高亮，🔥 肝度专用）。

颜色映射抽成纯函数（cell_alpha / fire_color）便于单测；widget 只负责绘制。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

_WEEK = ["一", "二", "三", "四", "五", "六", "日"]
_FIRE = ((255, 212, 121), (255, 140, 66), (232, 69, 60))  # 浅黄 → 橙 → 红


def cell_alpha(value: int, vmax: int) -> float:
    """格子热度 → 不透明度 [0,1]。空格或 vmax<=0 返回 0。"""
    if vmax <= 0 or value <= 0:
        return 0.0
    return min(1.0, value / vmax)


def fire_color(value: int, vmax: int) -> QColor:
    """热度 → 火焰色（浅黄→橙→红）。空格(value<=0)返回全透明，由调用方用 empty 兜底。"""
    a = cell_alpha(value, vmax)
    if a <= 0:
        return QColor(0, 0, 0, 0)
    x = a * 2.0
    i = 0 if x < 1.0 else 1
    f = x - i
    c0, c1 = _FIRE[i], _FIRE[i + 1]
    r, g, b = (round(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
    return QColor(r, g, b, int(170 + a * 85))  # 低热也够亮，压在深底上可读


def peak_label(wd: int, hour: int) -> str:
    """峰值格 → 「周三 23 点」式中文标签。"""
    if not (0 <= wd < 7):
        return ""
    return f"周{_WEEK[wd]} {hour} 点"


class HeatmapWidget(QWidget):
    """7 行(周一..周日) × 24 列(0..23 时) 的修改频次热力图（火焰色 + 峰值描边）。"""

    def __init__(self, matrix, *, accent, empty, ink, parent=None):
        super().__init__(parent)
        self.matrix = matrix
        self.peak = max((max(row) for row in matrix), default=0)
        self.peak_wd, self.peak_hour = self._find_peak(matrix)
        self._accent = accent          # 兼容旧签名；绘制改用 fire_color
        self._empty = QColor(empty)
        self._ink = QColor(ink)
        self.setMinimumHeight(7 * 16 + 28)

    @staticmethod
    def _find_peak(matrix) -> tuple[int, int]:
        best, pos = 0, (-1, -1)
        for wd, row in enumerate(matrix):
            for h, v in enumerate(row):
                if v > best:
                    best, pos = v, (wd, h)
        return pos

    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        left, top, gap = 26, 18, 2
        avail_w = self.width() - left - 6
        cell = max(8.0, (avail_w - 23 * gap) / 24)

        font = QFont(self.font())
        font.setPointSizeF(8.5)
        p.setFont(font)
        fm = p.fontMetrics()

        # 列标（每 6 小时一个刻度）
        p.setPen(self._ink)
        for h in (0, 6, 12, 18):
            x = left + h * (cell + gap)
            p.drawText(int(x), top - 5, f"{h}时")
        # 行标 + 格子（火焰色）
        for wd in range(7):
            y = top + wd * (cell + gap)
            ty = int(y + (cell + fm.ascent() - fm.descent()) / 2)
            p.setPen(self._ink)   # 格子循环会把 pen 设为 NoPen，画行标前必须复位
            p.drawText(2, ty, _WEEK[wd])
            for h in range(24):
                x = left + h * (cell + gap)
                col = fire_color(self.matrix[wd][h], self.peak)
                p.setPen(Qt.NoPen)
                p.setBrush(self._empty if col.alpha() == 0 else col)
                p.drawRoundedRect(int(x), int(y), int(cell), int(cell), 2, 2)
        # 峰值格描边高亮（魔鬼时段）
        if self.peak_wd >= 0 and self.peak > 0:
            x = left + self.peak_hour * (cell + gap)
            y = top + self.peak_wd * (cell + gap)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 220, 130), 1.6))
            p.drawRoundedRect(int(x) - 1, int(y) - 1, int(cell) + 2, int(cell) + 2, 3, 3)
        p.end()
