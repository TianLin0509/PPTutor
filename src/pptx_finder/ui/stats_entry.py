"""非侵入式入口注入：给主窗口挂「我的胶片报告」入口。

只通过 mw 的稳定 self 属性（theme_btn / status_label / _conn / _theme）附加入口，
不改 main_window 既有逻辑。main_window 仅需在 _build_ui 末尾调用 install_stats_entry(self)。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from .. import stats
from . import theme
from .report_overlay import ReportOverlay


def _open_report(mw) -> None:
    report = stats.build_report(mw._conn, year=None)
    ov = ReportOverlay(report, theme.tok(mw._theme), parent=mw)
    ov.setGeometry(mw.rect())
    ov.show()
    ov.raise_()
    mw._stats_overlay = ov  # 持引用，避免被 GC 回收


def install_stats_entry(mw) -> None:
    """给主窗口挂两个入口：顶栏 🎞️ 图标 + 状态栏数字可点击。"""
    # 入口 1：顶栏 🎞️ 图标（插到主题切换键旁，ghost 风格不抢眼）
    btn = QPushButton("🎞️")
    btn.setObjectName("ghost")
    btn.setMinimumHeight(42)
    btn.setToolTip("我的胶片报告")
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(lambda: _open_report(mw))
    try:
        bar = mw.theme_btn.parentWidget().layout().itemAt(0).layout()
        bar.addWidget(btn)
    except Exception:  # noqa: BLE001 顶栏结构变了就降级，不挂顶栏入口
        pass

    # 入口 2：状态栏数字可点击（彩蛋）
    try:
        mw.status_label.setCursor(Qt.PointingHandCursor)
        mw.status_label.setToolTip("点我看胶片报告")
        mw.status_label.mousePressEvent = lambda e: _open_report(mw)
    except Exception:  # noqa: BLE001
        pass
