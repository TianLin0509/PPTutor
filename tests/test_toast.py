"""方向 05：浮层 Toast —— 操作反馈走中下方浮层，不再污染状态栏。"""
from __future__ import annotations

from test_ui import StubRender, _index

import pptx_finder.ui.main_window as main_window_mod
from pptx_finder.ui.main_window import MainWindow


def test_toast_uses_floating_label_not_statusbar(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    status_before = win.status_label.text()

    win._toast("已复制完整路径")

    # 浮层承载提示文本
    assert win._toast_label.text() == "已复制完整路径"
    # 状态栏不被一次性操作反馈污染（仍是索引状态）
    assert win.status_label.text() == status_before


def test_toast_schedules_autohide(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win._toast("测试")
    # 启动了自动隐藏定时器（一段时间后浮层自行消失）
    assert win._toast_timer.isActive()


def test_stale_toast_hide_does_not_hide_newer_toast(qtbot, tmp_path, monkeypatch):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    scheduled = []

    monkeypatch.setattr(
        main_window_mod.QTimer,
        "singleShot",
        staticmethod(lambda delay_ms, callback: scheduled.append((delay_ms, callback))),
    )

    win._toast("第一条")
    win._hide_toast()
    assert scheduled and scheduled[-1][0] == 200
    stale_hide = scheduled[-1][1]

    win._toast("第二条")
    stale_hide()

    assert win._toast_label.isHidden() is False
    assert win._toast_label.text() == "第二条"
