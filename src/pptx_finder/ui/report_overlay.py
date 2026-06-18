"""报告浮层 ReportOverlay：盖在主界面上的「我的胶片报告」卡片流。

- 文案格式化纯函数（human_bytes / redmansion_equiv / hour_label）便于单测。
- 跟随主题（tok 传入），用内联样式而非全局 QSS（不依赖 theme 模块，互不冲突）。
- 给定 conn 时 header 提供「全部 / 本年」年度切换；导出 PNG；Esc / 点遮罩关闭。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import stats
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


def _this_year() -> int:
    return datetime.now().year


# ---------- 浮层 ----------

class ReportOverlay(QWidget):
    """半透明遮罩 + 居中卡片。给定 conn 时支持年度切换（重算重建内容）。"""

    def __init__(self, report, tok, parent=None, *, conn=None):
        super().__init__(parent)
        self._tok = tok
        self._conn = conn
        self.current_report = report
        self.current_year = report.scope_year
        self.setObjectName("reportOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("#reportOverlay{background:rgba(0,0,0,0.42);}")

        self._card = QFrame()
        self._card.setObjectName("repCard")
        self._card.setFixedWidth(480)
        self._card.setStyleSheet(
            f"#repCard{{background:{tok['win']};border:1px solid {tok['bd']};border-radius:16px;}}")
        self._card_lay = QVBoxLayout(self._card)
        self._card_lay.setContentsMargins(24, 20, 24, 24)
        self._card_lay.setSpacing(13)

        self._build_header()
        self._content = QWidget()
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setContentsMargins(0, 0, 0, 0)
        self._content_lay.setSpacing(13)
        self._card_lay.addWidget(self._content)
        self._fill_content()

        scroll = QScrollArea()
        scroll.setWidget(self._card)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(self._card.width() + 18)
        scroll.setMaximumHeight(700)
        scroll.setStyleSheet("background:transparent;")

        outer = QVBoxLayout(self)
        outer.addWidget(scroll, alignment=Qt.AlignCenter)
        if parent is not None:
            self.setGeometry(parent.rect())

    # ---- 构建 ----
    def _build_header(self) -> None:
        tok = self._tok
        head = QHBoxLayout()
        self._title = QLabel("🎞️ 我的胶片报告")
        self._title.setStyleSheet(
            f"font-size:17px;font-weight:700;color:{tok['ink1']};background:transparent;border:none;")
        head.addWidget(self._title, 1)

        # 年度切换（仅当给定 conn 可重算时）
        if self._conn is not None:
            self._all_btn = QPushButton("全部")
            self._year_btn = QPushButton("本年")
            chip_css = (
                f"QPushButton{{background:{tok['field']};color:{tok['ink3']};border:1px solid {tok['bd']};"
                f"border-radius:980px;padding:3px 11px;font-size:12px;}}"
                f"QPushButton:checked{{background:rgba({tok['hl_r']},{tok['hl_g']},{tok['hl_b']},0.16);"
                f"color:{tok['acc']};border-color:{tok['acc']};}}")
            for b, yr in ((self._all_btn, None), (self._year_btn, _this_year())):
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet(chip_css)
                b.clicked.connect(lambda _=False, y=yr: self.switch_year(y))
                head.addWidget(b)

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
        self._card_lay.addLayout(head)

    def switch_year(self, year: int | None) -> None:
        self.current_year = year
        if self._conn is not None:
            self.current_report = stats.build_report(self._conn, year=year)
        self._fill_content()

    def _fill_content(self) -> None:
        while self._content_lay.count():
            it = self._content_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        report = self.current_report
        tok = self._tok

        scope = f"{self.current_year} 年" if self.current_year else "全部历史"
        if self._conn is not None:
            self._all_btn.setChecked(self.current_year is None)
            self._year_btn.setChecked(self.current_year is not None)

        sc = report.scale
        overview = QLabel(
            f"{scope}　·　{report.deck_count} 份胶片　·　{sc.total_chars:,} 字　·　{human_bytes(sc.total_bytes)}")
        overview.setStyleSheet(f"color:{tok['ink3']};background:transparent;border:none;font-size:12.5px;")
        self._content_lay.addWidget(overview)

        # 🔥 肝度（含热力图）
        n = report.night
        nlines = [
            f"深夜胶片 <b>{n.night_count}</b> 份（{n.night_ratio:.0%}）　"
            f"周末打工 <b>{n.weekend_count}</b> 次"
        ]
        if n.latest_name:
            nlines.append(f"最晚一次 <b>{hour_label(n.latest_hour)}</b> 改了《{n.latest_name}》")
        liver = self._section("🔥 肝度", nlines)
        liver.layout().addWidget(HeatmapWidget(
            report.heatmap,
            accent=(int(tok["hl_r"]), int(tok["hl_g"]), int(tok["hl_b"])),
            empty=tok["field"], ink=tok["ink3"]))
        self._content_lay.addWidget(liver)

        # 😅 改版名场面
        d = report.drama
        dlines = []
        if d.top_group_name:
            dlines.append(f"最能改奖：《{d.top_group_name}》存了 <b>{d.top_group_versions}</b> 版")
        dlines.append(
            f"终版诅咒：<b>{d.final_curse_count}</b> 份名带「最终/final/vN」（{d.final_curse_ratio:.0%}）")
        if d.zombie_name:
            dlines.append(f"僵尸胶片：《{d.zombie_name}》吃灰最久")
        self._content_lay.addWidget(self._section("😅 改版名场面", dlines))

        # 📊 规模仓鼠
        slines = []
        if sc.longest_name:
            slines.append(f"最长：《{sc.longest_name}》<b>{sc.longest_pages}</b> 页")
        slines.append(f"累计码字 {sc.total_chars:,} ≈ <b>{redmansion_equiv(sc.total_chars)}</b>")
        slines.append(f"磁盘占用 <b>{human_bytes(sc.total_bytes)}</b>")
        self._content_lay.addWidget(self._section("📊 规模仓鼠", slines))

        # 🏅 人格称号
        p = report.persona
        badge = "　·　".join(p.badges) if p.badges else "—"
        plines = [
            f"<span style='font-size:18px;font-weight:700;color:{tok['acc']};'>{p.title}</span>",
            f"副标签：{badge}",
        ]
        self._content_lay.addWidget(self._section("🏅 你的称号", plines))

        self._card.adjustSize()

    def _section(self, title: str, html_lines: list[str]) -> QFrame:
        tok = self._tok
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
        if self.childAt(e.position().toPoint()) is None:
            self.close()
        super().mousePressEvent(e)
