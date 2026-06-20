"""引导基建：呼吸高亮 attention_pulse + 聚光灯遮罩 SpotlightOverlay。"""
from __future__ import annotations

from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from pptx_finder.ui.spotlight import (
    SpotlightOverlay, animations_enabled, attention_pulse,
)


def test_animations_enabled_returns_bool():
    assert isinstance(animations_enabled(), bool)


def test_attention_pulse_sets_effect_no_crash(qtbot):
    w = QLineEdit()
    qtbot.addWidget(w)
    attention_pulse(w, color="#0A84FF", cycles=2)
    assert w.graphicsEffect() is not None       # 呼吸光晕已挂
    assert hasattr(w, "_pulse_anim")            # 防 GC 标记存在


def test_attention_pulse_replaces_previous(qtbot):
    w = QLineEdit()
    qtbot.addWidget(w)
    attention_pulse(w, cycles=3)
    attention_pulse(w, cycles=3)                # 重复调用应替换上一个、不崩
    assert w.graphicsEffect() is not None


def _host(qtbot):
    host = QWidget()
    host.resize(600, 400)
    lay = QVBoxLayout(host)
    target = QLineEdit()
    target.setFixedSize(320, 42)
    lay.addWidget(target)
    qtbot.addWidget(host)
    host.show()
    qtbot.wait(40)                               # 等布局完成（mapTo 才有效）
    return host, target


def test_spotlight_hole_covers_target(qtbot):
    host, target = _host(qtbot)
    ov = SpotlightOverlay(host, target, "在这里搜", tok={"acc": "#0A84FF"})
    hole = ov._hole()
    assert hole.isValid() and not hole.isEmpty()
    assert hole.width() >= target.width()        # 含 pad，应不小于目标
    assert hole.height() >= target.height()


def test_spotlight_paint_no_crash(qtbot):
    host, target = _host(qtbot)
    ov = SpotlightOverlay(
        host, target, "在这里输入你 PPT 里写过的字，跨所有文件按页搜索",
        tok={"acc": "#0A84FF", "win": "#FFFFFF", "ink1": "#1D1D1F"})
    ov.grab()                                    # 触发 paintEvent（挖洞+气泡+箭头）不崩即过


def test_spotlight_dismiss_fires_callback(qtbot):
    host, target = _host(qtbot)
    fired = []
    ov = SpotlightOverlay(host, target, "x", on_dismiss=lambda: fired.append(1))
    ov._close()
    assert fired == [1]


def test_spotlight_no_target_fills_full(qtbot):
    """target 不可见时退化为全屏遮罩，不崩。"""
    host, target = _host(qtbot)
    target.hide()
    ov = SpotlightOverlay(host, target, "x")
    assert ov._hole().isEmpty()
    ov.grab()
