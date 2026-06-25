"""首次启动欢迎引导覆盖层：玩法教学 + 索引进度 + 风格选择。

覆盖在主窗口 central 之上（随父 resize）；点「开始使用」或索引完成后由主窗关闭。
风格选择实时换肤（覆盖层自身也跟着换），降低「选了不知道长啥样」的顾虑。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme


def _welcome_logo(size: int = 60) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#E3B572"), 2.6))
    p.drawRoundedRect(6, 10, 48, 40, 11, 11)
    p.setPen(QPen(QColor("#5D9BFF"), 2.6))
    p.drawEllipse(20, 22, 18, 18)
    p.drawLine(34, 36, 46, 48)
    p.end()
    return pm


_FEATURES = [
    ("🔍", "搜内容，直接定位到页", "记得幻灯片里写过的字，搜出来是哪个文件第几页，点开直接跳过去"),
    ("🎬", "版本归组 · PPT 版 git", "同一份 PPT 的多个版本自动归到一起，改崩了能找回任意历史版本"),
    ("📊", "趣味统计", "看看你的「PPT 肝度」「年度报告」「规模仓鼠」，打工人专属"),
    ("⌨️", "全局热键随叫随到", "任何时候按 Ctrl+Alt+P 唤起，关闭即收进托盘常驻"),
]


class WelcomeOverlay(QWidget):
    def __init__(self, parent=None, *, on_start, on_pick_theme, current_theme="cloud"):
        super().__init__(parent)
        self._on_start_cb = on_start
        self._on_pick_cb = on_pick_theme
        self._theme = current_theme
        self.setObjectName("welcomeOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignCenter)
        root.setContentsMargins(40, 28, 40, 28)

        card = QWidget()
        card.setObjectName("welcomeCard")
        card.setMaximumWidth(720)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(46, 38, 46, 32)
        cl.setSpacing(0)
        cl.setAlignment(Qt.AlignHCenter)

        logo = QLabel()
        logo.setPixmap(_welcome_logo())
        logo.setAlignment(Qt.AlignCenter)
        cl.addWidget(logo, 0, Qt.AlignHCenter)

        title = QLabel('欢迎使用 <span style="color:#E3B572">PPT Doctor</span>')
        title.setObjectName("wTitle")
        title.setTextFormat(Qt.RichText)
        title.setAlignment(Qt.AlignCenter)
        cl.addSpacing(14)
        cl.addWidget(title)

        sub = QLabel("记得写过什么，就能找到它在哪个 PPT、第几页")
        sub.setObjectName("wSub")
        sub.setAlignment(Qt.AlignCenter)
        cl.addSpacing(7)
        cl.addWidget(sub)

        grid = QGridLayout()
        grid.setSpacing(11)
        for i, (ic, ft, fd) in enumerate(_FEATURES):
            grid.addWidget(self._feat_card(ic, ft, fd), i // 2, i % 2)
        cl.addSpacing(26)
        cl.addLayout(grid)

        self._progress_label = QLabel("正在首次扫描你的磁盘… 可边扫边搜")
        self._progress_label.setObjectName("wProgress")
        self._progress_label.setAlignment(Qt.AlignCenter)
        cl.addSpacing(22)
        cl.addWidget(self._progress_label)

        st = QLabel("顺手挑个风格（之后随时能换）")
        st.setObjectName("wStyleHint")
        st.setAlignment(Qt.AlignCenter)
        cl.addSpacing(20)
        cl.addWidget(st)
        # 10 主题分两行网格（5×2）：单行 HBox 在窄窗口会把 chip 挤到文字重叠
        sw = QGridLayout()
        sw.setSpacing(8)
        sw.setContentsMargins(0, 0, 0, 0)
        per_row = 5
        for c in range(per_row):
            sw.setColumnStretch(c, 1)  # 等宽列，chip 均匀铺满不挤
        self._theme_btns: dict[str, QPushButton] = {}
        for i, (name, label) in enumerate(theme.THEMES):
            b = QPushButton(label)
            b.setObjectName("wSwatch")
            b.setCheckable(True)
            b.setChecked(name == self._theme)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, n=name: self._pick(n))
            self._theme_btns[name] = b
            sw.addWidget(b, i // per_row, i % per_row)
        cl.addSpacing(10)
        cl.addLayout(sw)

        self._start_btn = QPushButton("开始使用 →")
        self._start_btn.setObjectName("wStart")
        self._start_btn.clicked.connect(lambda: self._on_start_cb())
        cl.addSpacing(26)
        cl.addWidget(self._start_btn, 0, Qt.AlignHCenter)

        root.addWidget(card)
        self._apply_qss()

    def _feat_card(self, ic: str, ft: str, fd: str) -> QWidget:
        w = QWidget()
        w.setObjectName("wFeat")
        h = QHBoxLayout(w)
        h.setContentsMargins(16, 13, 16, 13)
        h.setSpacing(13)
        icon = QLabel(ic)
        icon.setObjectName("wFeatIc")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(38, 38)
        h.addWidget(icon, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        t = QLabel(ft)
        t.setObjectName("wFeatT")
        d = QLabel(fd)
        d.setObjectName("wFeatD")
        d.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(d)
        h.addLayout(col, 1)
        return w

    def _pick(self, name: str) -> None:
        self._theme = name
        for n, b in self._theme_btns.items():
            b.setChecked(n == name)
        self._apply_qss()
        self._on_pick_cb(name)

    def update_progress(self, found: int) -> None:
        self._progress_label.setText(
            f"正在首次扫描你的磁盘… 已找到 {found} 个 PPTX · 可边扫边搜")

    def resizeEvent(self, e):  # noqa: N802
        if self.parent() is not None:
            self.resize(self.parent().size())
        super().resizeEvent(e)

    def _apply_qss(self) -> None:
        t = theme.tok(self._theme)
        self.setStyleSheet(f"""
        #welcomeOverlay {{ background: {t['appbg']}; }}
        #welcomeCard {{ background: transparent; }}
        #wTitle {{ font-size: 26px; font-weight: 800; color: {t['ink1']}; }}
        #wSub {{ font-size: 14px; color: {t['ink3']}; }}
        #wFeat {{ background: {t['panel2']}; border: 1px solid {t['bd']}; border-radius: 12px; }}
        #wFeatIc {{ background: rgba({t['hl_r']},{t['hl_g']},{t['hl_b']},0.12);
                    border: 1px solid {t['bd2']}; border-radius: 10px; font-size: 18px; }}
        #wFeatT {{ font-size: 14px; font-weight: 700; color: {t['ink1']}; background: transparent; }}
        #wFeatD {{ font-size: 12px; color: {t['ink3']}; background: transparent; }}
        #wProgress {{ font-size: 13px; color: {t['ink2']}; }}
        #wStyleHint {{ font-size: 11px; color: {t['ink4']}; }}
        #wSwatch {{ background: {t['field']}; border: 1px solid {t['bd']}; border-radius: 8px;
                    padding: 6px 10px; color: {t['ink3']}; font-size: 12px; min-width: 44px; }}
        #wSwatch:checked {{ border-color: {t['acc']}; color: {t['acc']};
                            background: rgba({t['hl_r']},{t['hl_g']},{t['hl_b']},0.14); }}
        #wStart {{ background: {t['acc']}; border: 1px solid {t['acc']}; border-radius: 10px;
                   padding: 11px 30px; color: {t['acctext']}; font-size: 14px; font-weight: 600; }}
        #wStart:hover {{ background: {t['accd']}; }}
        """)
