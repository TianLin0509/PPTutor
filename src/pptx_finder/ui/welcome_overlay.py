"""首次启动欢迎引导覆盖层：玩法教学 + 索引进度 + 风格选择。

覆盖在主窗口 central 之上（随父 resize）；完成后显示结果，点「开始使用」由主窗关闭。
风格选择实时换肤（覆盖层自身也跟着换），降低「选了不知道长啥样」的顾虑。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from . import icons as _icons
from . import theme
from .aurora_bg import paint_aurora


def _welcome_logo(size: int = 60) -> QPixmap:
    """欢迎页用程序自己的吉祥物 logo（与任务栏图标一致），按 3 倍分辨率缩放，高分屏不糊。"""
    from ..config import resource_path
    logo = QPixmap(str(resource_path("assets", "logo.png")))
    if not logo.isNull():
        ratio = 3.0
        logo = logo.scaled(round(size * ratio), round(size * ratio), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        logo.setDevicePixelRatio(ratio)
        return logo
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#2563EB"), 2.6))
    p.drawEllipse(20, 22, 18, 18)
    p.drawLine(34, 36, 46, 48)
    p.end()
    return pm


# 方案 D：线性图标代替彩色 emoji（图标名见 icons.PATHS）
_FEATURES = [
    ("search", "搜内容，直接定位到页", "记得幻灯片里写过的字，搜出来是哪个文件第几页，点开直接跳过去"),
    ("copy", "复制本页 / 插入到 PPT", "找到需要的那页后，可以复制整页或插入到已打开的 PPT；请先打开目标文稿"),
    ("materials", "图片转文字 · 素材库", "把截图转成文字可编辑的 PPT；常用箭头、框图也能收藏复用"),
    ("version", "版本留存 · 智能归组", "版本留存可找回历史版本，智能归组把相似 PPT 归在一起；两项默认关闭，可在设置里开启。按 Alt+F 随时唤起搜索"),
]


def _is_disk_full(error: object) -> bool:
    detail = str(error).lower()
    return any(word in detail for word in ("errno 28", "winerror 112", "no space", "disk full", "磁盘满", "磁盘空间不足"))


#: 欢迎层的主题缩略卡：晨雾玻璃（默认）/ 暗夜玻璃 / 静白
WELCOME_THEMES = ("mist_glass", "night_glass", "atelier")


def _index_problem_message(error: object) -> str:
    """首次引导只给可操作的原因，不透出异常类、系统路径和堆栈。"""
    detail = str(error).lower()
    if any(word in detail for word in ("errno 28", "winerror 112", "no space", "disk full", "磁盘满", "磁盘空间不足")):
        reason = "磁盘空间不足，请清理一些空间"
    elif any(word in detail for word in ("permission", "access is denied", "winerror 5]", "errno 13", "权限", "拒绝访问")):
        reason = "没有访问权限，请确认相关文件夹允许读取"
    elif any(word in detail for word in ("not found", "no such file", "winerror 3]", "找不到", "不存在")):
        reason = "文件或磁盘暂时找不到，请确认磁盘已连接、文件未被移动"
    else:
        reason = "部分文件读取失败，请确认磁盘可访问"
    return reason + "；点击「开始使用」后，可在「设置 → 健康诊断」点「重新扫描索引」重试"


class WelcomeOverlay(QWidget):
    def __init__(self, parent=None, *, on_start, on_pick_theme, current_theme="cloud", on_settings=None):
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

        title = QLabel('欢迎使用 <span style="color:#2563EB">PPT Doctor</span>')
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
        self._settings_btn = QPushButton("去设置「功能」页开启 →")
        self._settings_btn.setVisible(on_settings is not None)
        if on_settings is not None:
            self._settings_btn.clicked.connect(on_settings)
        cl.addWidget(self._settings_btn, 0, Qt.AlignHCenter)

        self._progress_label = QLabel("正在查找文件… 不影响你同时搜索")
        self._progress_label.setObjectName("wProgress")
        self._progress_label.setTextFormat(Qt.PlainText)
        self._progress_label.setWordWrap(True)
        self._progress_label.setAlignment(Qt.AlignCenter)
        cl.addSpacing(22)
        cl.addWidget(self._progress_label)

        # 磁盘不足：橙色提示条 + 就地按钮（不让用户自己去找设置）
        self._disk_bar = QWidget()
        self._disk_bar.setObjectName("wDiskBar")
        self._disk_bar.setAttribute(Qt.WA_StyledBackground, True)
        db_lay = QHBoxLayout(self._disk_bar)
        db_lay.setContentsMargins(14, 8, 8, 8)
        db_lay.setSpacing(10)
        self._disk_text = QLabel("磁盘空间不足，索引没能写完。清理一些空间后点右边按钮继续。")
        self._disk_text.setObjectName("wDiskText")
        self._disk_text.setWordWrap(True)
        db_lay.addWidget(self._disk_text, 1)
        self._disk_btn = QPushButton("清理后重新扫描")
        self._disk_btn.setObjectName("wDiskBtn")
        self._disk_btn.setCursor(Qt.PointingHandCursor)
        if on_settings is not None:
            self._disk_btn.clicked.connect(on_settings)
        db_lay.addWidget(self._disk_btn)
        self._disk_bar.hide()
        cl.addSpacing(10)
        cl.addWidget(self._disk_bar)

        st = QLabel("顺手挑个外观（随时可在左下角切深浅，或到「设置 → 外观」换别的）")
        st.setObjectName("wStyleHint")
        st.setAlignment(Qt.AlignCenter)
        cl.addSpacing(20)
        cl.addWidget(st)
        # 首次使用主推三张缩略卡：晨雾玻璃 / 暗夜玻璃 / 静白；完整主题列表在设置「外观」页。
        sw = QGridLayout()
        sw.setSpacing(10)
        sw.setContentsMargins(0, 0, 0, 0)
        per_row = len(WELCOME_THEMES)
        for c in range(per_row):
            sw.setColumnStretch(c, 1)
        self._theme_btns: dict[str, QToolButton] = {}
        labels = dict(theme.THEMES)
        for i, name in enumerate(WELCOME_THEMES):
            b = QToolButton()
            b.setObjectName("wSwatch")
            b.setText(labels.get(name, name))
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIcon(QIcon(theme.preview_pixmap(name, 132, 74)))
            b.setIconSize(QSize(132, 74))
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
        icon = QLabel()
        icon.setObjectName("wFeatIc")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(38, 38)
        icon.setProperty("iconName", ic)
        self._feat_icons = getattr(self, "_feat_icons", []) + [icon]
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
        self.update()
        self._on_pick_cb(name)

    def update_progress(self, found: int, total: int | None = None) -> None:
        count = f"已发现 {found} 个文件" if total is None else f"已处理 {found} / {total} 个文件"
        phase = "正在查找文件" if total is None else "正在建立索引"
        self._progress_label.setText(f"{phase}… {count} · 不影响你同时搜索")

    def finish_index(self, summary: dict) -> None:
        self._disk_bar.setVisible(bool(summary.get("error")) and _is_disk_full(summary.get("error")))
        if summary.get("error"):
            text = "首次建立索引遇到问题，已完成的结果仍可搜索。" + _index_problem_message(summary["error"])
        elif summary.get("cancelled"):
            text = "首次建立索引已暂停，已完成的结果可搜；点击「开始使用」后，可在「设置 → 健康诊断」点「重新扫描索引」继续"
        else:
            text = "首次建立索引完成，可以开始搜索了"
            if summary.get("name_index_error"):
                text = "PPT 内容已可搜索，部分文件名暂时搜不到。" + _index_problem_message(summary["name_index_error"])
        self._progress_label.setText(text)

    def resizeEvent(self, e):  # noqa: N802
        if self.parent() is not None:
            self.resize(self.parent().size())
        super().resizeEvent(e)

    def paintEvent(self, e):  # noqa: N802
        """欢迎层的底与主窗同一画法：底色 + 静态光团（不模糊、不加阴影效果）。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        paint_aurora(p, QRectF(self.rect()), theme.tok(self._theme))
        p.end()

    def _apply_qss(self) -> None:
        t = theme.tok(self._theme)
        for icon in getattr(self, "_feat_icons", ()):
            icon.setPixmap(_icons.icon(icon.property("iconName"), t["acc"], 20).pixmap(20, 20))
        card_bg = t["panel"] if theme.float_glass(self._theme) else "transparent"
        self.setStyleSheet(f"""
        #welcomeOverlay {{ background: transparent; }}
        #welcomeCard {{ background: {card_bg}; border: 1px solid {t['edge']}; border-radius: 24px; }}
        #wDiskBar {{ background: rgba(232,120,12,0.12); border: 1px solid rgba(232,120,12,0.35); border-radius: 12px; }}
        #wDiskText {{ font-size: 12.5px; color: {t['ink1']}; background: transparent; }}
        #wDiskBtn {{ background: {t['panel2']}; border: 1px solid {t['bd']}; border-radius: 14px; padding: 6px 14px;
                     color: {t['ink1']}; font-size: 12.5px; }}
        #wTitle {{ font-size: 26px; font-weight: 800; color: {t['ink1']}; }}
        #wSub {{ font-size: 14px; color: {t['ink3']}; }}
        #wFeat {{ background: {t['panel2']}; border: 1px solid {t['bd']}; border-radius: 12px; }}
        #wFeatIc {{ background: rgba({t['hl_r']},{t['hl_g']},{t['hl_b']},0.12);
                    border: 1px solid {t['bd2']}; border-radius: 10px; font-size: 18px; }}
        #wFeatT {{ font-size: 14px; font-weight: 700; color: {t['ink1']}; background: transparent; }}
        #wFeatD {{ font-size: 12px; color: {t['ink3']}; background: transparent; }}
        #wProgress {{ font-size: 13px; color: {t['ink2']}; }}
        #wStyleHint {{ font-size: 11px; color: {t['ink4']}; }}
        #wSwatch {{ background: {t['panel2']}; border: 1px solid {t['bd']}; border-radius: 14px;
                    padding: 8px 8px 6px 8px; color: {t['ink2']}; font-size: 12.5px; }}
        #wSwatch:checked {{ border: 2px solid {t['acc']}; color: {t['acc']}; font-weight: 600; }}
        #wStart {{ background: {t['accfill']}; border: 1px solid {t['accfill']}; border-radius: 18px;
                   padding: 11px 30px; color: {t['acctext']}; font-size: 14px; font-weight: 600; }}
        #wStart:hover {{ background: {t['accfillh']}; }}
        """)
