"""24×7 热力图 widget 单测：颜色映射纯逻辑 + 构造 smoke。"""
from __future__ import annotations

from pptx_finder.ui import heatmap as hm


def test_cell_alpha_zero_when_empty():
    assert hm.cell_alpha(0, 10) == 0.0


def test_cell_alpha_full_at_max():
    assert hm.cell_alpha(10, 10) == 1.0


def test_cell_alpha_midrange_between():
    assert 0.0 < hm.cell_alpha(5, 10) < 1.0


def test_cell_alpha_handles_zero_max():
    assert hm.cell_alpha(0, 0) == 0.0


def test_heatmap_widget_constructs(qtbot):
    matrix = [[0] * 24 for _ in range(7)]
    matrix[0][2] = 5
    w = hm.HeatmapWidget(matrix, accent=(10, 132, 255), empty="#EEEEEE", ink="#333333")
    qtbot.addWidget(w)
    assert w.matrix[0][2] == 5
    assert w.peak == 5  # 最热格子值
