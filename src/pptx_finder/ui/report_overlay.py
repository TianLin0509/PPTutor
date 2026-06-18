"""报告浮层 ReportOverlay：盖在主界面上的「我的胶片报告」卡片流。

- 文案格式化纯函数（human_bytes / redmansion_equiv / hour_label）便于单测。
- 跟随主题（tok 传入），用内联样式而非全局 QSS（不依赖 theme 模块，互不冲突）。
- 导出 PNG：grab 完整卡片面板存图，方便发同事群。
- Esc / 点遮罩关闭。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .heatmap import HeatmapWidget

_RED_MANSION_CHARS = 730_000  # 《红楼梦》约 73 万字


# ---------- 文案格式化（纯函数，可测） ----------

def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def redmansion_equiv(chars: int) -> str:
    return f"{chars / _RED_MANSION_CHARS:.1f} 本《红楼梦》"


def hour_label(h: int) -> str:
    if h < 6:
        return f"凌晨{h}点"
    if h < 12:
        return f"上午{h}点"
    if h < 18:
        return f"下午{h}点"
    if h < 23:
        return f"晚上{h}点"
    return f"深夜{h}点"


# ---------- 浮层 ----------

class ReportOverlay(QWidget):
    """半透明遮罩 + 居中卡片。card 自然高度（导出完整），显示时套滚动区限高。"""

    def __init__(self, report, tok, parent=None):
        super().__init__(parent)
        self._tok = tok
        self.setObjectName("reportOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("#reportOverlay{background:rgba(0,0,0,0.42);}")

        self._card = self._build_card(report, tok)

        scroll = QScrollArea()
        scroll.setWidget(self._card)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(self._card.width() + 18)
        scroll.setMaximumHeight(700)
        scroll.setStyleSheet("background:transparent;")
        self._scroll = scroll

        outer = QVBoxLayout(self)
        outer.addWidget(scroll, alignment=Qt.AlignCenter)

        if parent is not None:
            self.setGeometry(parent.rect())

    # ---- 构建 ----
    def _build_card(self, report, tok) -> QFrame:
        card = QFrame()
        card.setObjectName("repCard")
        card.setFixedWidth(480)
        card.setStyleSheet(
            f"#repCard{{background:{tok['win']};border:1px solid {tok['bd']};border-radius:16px;}}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 24)
        lay.setSpacing(13)

        # 标题行 + 导出 / 关闭
        head = QHBoxLayout()
        scope = report.scope_year if report.scope_year else "全部历史"
        title = QLabel(f"🎞️ 我的胶片报告 · {scope}")
        title.setStyleSheet(
            f"font-size:17px;font-weight:700;color:{tok['ink1']};background:transparent;border:none;")
        head.addWidget(title, 1)
        self.export_btn = QPushButton("导出图片")
        self.export_btn.setStyleSheet(
            f"QPushButton{{background:{tok['acc']};color:{tok['acctext']};border:none;"
            f"border-radius:7px;padding:5px 12px;font-weight:600;}}")
        head.addWidget(self.export_btn)
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedWidth(30)
        self.close_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{tok['ink3']};border:none;font-size:15px;}}")
        self.close_btn.clicked.connect(self.close)
        head.addWidget(self.close_btn)
        lay.addLayout(head)

        # 总览
        sc = report.scale
        overview = QLabel(
            f"{report.deck_count} 份胶片　·　{sc.total_chars:,} 字　·　{human_bytes(sc.total_bytes)}")
        overview.setStyleSheet(f"color:{tok['ink3']};background:transparent;border:none;font-size:12.5px;")
        lay.addWidget(overview)

        # 🔥 肝度（含热力图）
        n = report.night
        nlines = [
            f"深夜胶片 <b>{n.night_count}</b> 份（{n.night_ratio:.0%}）　"
            f"周末打工 <b>{n.weekend_count}</b> 次"
        ]
        if n.latest_name:
            nlines.append(f"最晚一次 <b>{hour_label(n.latest_hour)}</b> 改了《{n.latest_name}》")
        liver = self._section("🔥 肝度", nlines, tok)
        liver.layout().addWidget(HeatmapWidget(
            report.heatmap,
            accent=(int(tok["hl_r"]), int(tok["hl_g"]), int(tok["hl_b"])),
            empty=tok["field"], ink=tok["ink3"]))
        lay.addWidget(liver)

        # 😅 改版名场面
        d = report.drama
        dlines = []
        if d.top_group_name:
            dlines.append(f"最能改奖：《{d.top_group_name}》存了 <b>{d.top_group_versions}</b> 版")
        dlines.append(
            f"终版诅咒：<b>{d.final_curse_count}</b> 份名带「最终/final/vN」（{d.final_curse_ratio:.0%}）")
        if d.zombie_name:
            dlines.append(f"僵尸胶片：《{d.zombie_name}》吃灰最久")
        lay.addWidget(self._section("😅 改版名场面", dlines, tok))

        # 📊 规模仓鼠
        slines = []
        if sc.longest_name:
            slines.append(f"最长：《{sc.longest_name}》<b>{sc.longest_pages}</b> 页")
        slines.append(f"累计码字 {sc.total_chars:,} ≈ <b>{redmansion_equiv(sc.total_chars)}</b>")
        slines.append(f"磁盘占用 <b>{human_bytes(sc.total_bytes)}</b>")
        lay.addWidget(self._section("📊 规模仓鼠", slines, tok))

        # 🏅 人格称号
        p = report.persona
        badge = "　·　".join(p.badges) if p.badges else "—"
        plines = [
            f"<span style='font-size:18px;font-weight:700;color:{tok['acc']};'>{p.title}</span>",
            f"副标签：{badge}",
        ]
        lay.addWidget(self._section("🏅 你的称号", plines, tok))

        card.adjustSize()
        return card

    def _section(self, title: str, html_lines: list[str], tok) -> QFrame:
        f = QFrame()
        f.setStyleSheet(
            f"background:{tok['canvas']};border:1px solid {tok['bd']};border-radius:12px;")
        v = QVBoxLayout(f)
        v.setContentsMargins(16, 13, 16, 14)
        v.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet(
            f"font-size:14px;font-weight:700;color:{tok['ink1']};background:transparent;border:none;")
        v.addWidget(t)
        body = QLabel("<br>".join(html_lines))
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setStyleSheet(
            f"font-size:12.5px;color:{tok['ink2']};background:transparent;border:none;")
        v.addWidget(body)
        return f

    # ---- 行为 ----
    def export_png(self, path: str) -> bool:
        self._card.adjustSize()
        return bool(self._card.grab().save(path))

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):  # noqa: N802
        # 点击遮罩空白（不在卡片/滚动区内）即关闭
        if self.childAt(e.position().toPoint()) is None:
            self.close()
        super().mousePressEvent(e)
