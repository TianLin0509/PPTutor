"""24×7 修改热力图 widget（GitHub 贡献墙风格）。

颜色映射抽成纯函数 cell_alpha 便于单测；widget 只负责绘制。
颜色由调用方从主题 tok 提取后传入，本模块不依赖 theme（解耦，互不冲突）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

_WEEK = ["一", "二", "三", "四", "五", "六", "日"]


def cell_alpha(value: int, vmax: int) -> float:
    """格子热度 → 不透明度 [0,1]。空格或 vmax<=0 返回 0。"""
    if vmax <= 0 or value <= 0:
        return 0.0
    return min(1.0, value / vmax)


class HeatmapWidget(QWidget):
    """7 行(周一..周日) × 24 列(0..23 时) 的修改频次热力图。"""

    def __init__(self, matrix, *, accent, empty, ink, parent=None):
        super().__init__(parent)
        self.matrix = matrix
        self.peak = max((max(row) for row in matrix), default=0)
        self._accent = accent          # (r, g, b)
        self._empty = QColor(empty)
        self._ink = QColor(ink)
        self.setMinimumHeight(7 * 16 + 28)

    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        left, top, gap = 26, 18, 2
        avail_w = self.width() - left - 6
        cell = max(8.0, (avail_w - 23 * gap) / 24)
        r, g, b = self._accent

        font = QFont(self.font())
        font.setPointSizeF(8.5)
        p.setFont(font)
        fm = p.fontMetrics()

        # 列标（每 6 小时一个刻度）
        p.setPen(self._ink)
        for h in (0, 6, 12, 18):
            x = left + h * (cell + gap)
            p.drawText(int(x), top - 5, f"{h}时")
        # 行标 + 格子
        for wd in range(7):
            y = top + wd * (cell + gap)
            ty = int(y + (cell + fm.ascent() - fm.descent()) / 2)
            p.setPen(self._ink)   # 关键：格子循环会把 pen 设为 NoPen，画行标前必须复位
            p.drawText(2, ty, _WEEK[wd])
            for h in range(24):
                x = left + h * (cell + gap)
                a = cell_alpha(self.matrix[wd][h], self.peak)
                p.setPen(Qt.NoPen)
                p.setBrush(self._empty if a <= 0 else QColor(r, g, b, int(40 + a * 215)))
                p.drawRoundedRect(int(x), int(y), int(cell), int(cell), 2, 2)
        p.end()
