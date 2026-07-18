"""报告浮层 ReportOverlay：盖在主界面上的「我的胶片报告」主题 Tab。

- 文案格式化纯函数（human_bytes / redmansion_equiv / hour_label）便于单测。
- 跟随主题（tok 传入），用内联样式而非全局 QSS（不依赖 theme 模块，互不冲突）。
- 给定 conn 时 header 提供「全部 / 本年 / 本月 / 本周」切换；导出当前 Tab；Esc 关闭。
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta

from PySide6.QtCore import QPoint, QRect, QEasingCurve, Qt, QVariantAnimation
from PySide6.QtGui import QPainter, QPixmap, QRegion
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QTabBar, QVBoxLayout, QWidget,
)
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import actions, db, stats
from .bg_task import BackgroundTask
from .heatmap import HeatmapWidget

_RED_MANSION_CHARS = 730_000  # 《红楼梦》约 73 万字
_CARD_TARGET_WIDTH = 1140
_CARD_MIN_WIDTH = 420
_OVERLAY_MARGIN = 96
_SCROLL_CHROME_WIDTH = 18
_SCROLL_MAX_HEIGHT = 1230
_CARD_HEIGHT_RATIO = 0.80
_CARD_MIN_HEIGHT = 420

_SCOPE_ALL = "all"
_SCOPE_YEAR = "year"
_SCOPE_MONTH = "month"
_SCOPE_WEEK = "week"
_SCOPE_LABELS = {
    _SCOPE_ALL: "全部历史",
    _SCOPE_YEAR: "本年",
    _SCOPE_MONTH: "本月",
    _SCOPE_WEEK: "本周",
}
_REPORT_TABS = (
    ("overview", "总览"),
    ("hall", "名人堂"),
    ("rhythm", "创作节奏"),
    ("versions", "版本时光机"),
    ("content", "内容人格"),
    ("library", "片库版图"),
)


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


def _month_bounds(now: datetime | None = None) -> tuple[float, float]:
    now = now or datetime.now()
    start = datetime(now.year, now.month, 1)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1)
    else:
        end = datetime(now.year, now.month + 1, 1)
    return start.timestamp(), end.timestamp()


def _week_bounds(now: datetime | None = None) -> tuple[float, float]:
    now = now or datetime.now()
    start_day = now.date() - timedelta(days=now.weekday())
    start = datetime(start_day.year, start_day.month, start_day.day)
    end = start + timedelta(days=7)
    return start.timestamp(), end.timestamp()


def _scope_query(scope: str, year: int | None = None) -> tuple[int | None, float | None, float | None]:
    if scope == _SCOPE_YEAR:
        return year if year is not None else _this_year(), None, None
    if scope == _SCOPE_MONTH:
        since, until = _month_bounds()
        return None, since, until
    if scope == _SCOPE_WEEK:
        since, until = _week_bounds()
        return None, since, until
    return None, None, None


def _conn_path(conn) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None
        return row["file"] if hasattr(row, "keys") else row[2]
    except Exception:  # noqa: BLE001
        return None


def _build_report_off_ui(
    conn,
    *,
    year=None,
    since_ts=None,
    until_ts=None,
    version_db_path=None,
):
    kwargs = {"year": year}
    if since_ts is not None:
        kwargs["since_ts"] = since_ts
    if until_ts is not None:
        kwargs["until_ts"] = until_ts
    if version_db_path is not None:
        kwargs["version_db_path"] = version_db_path
    path = _conn_path(conn)
    if path:
        own = db.connect(path)
        try:
            return stats.build_report(own, **kwargs)
        finally:
            own.close()
    return stats.build_report(conn, **kwargs)


# ---------- 胶片配色回退源（浮层跟随 app 主题；仅当传入 token 缺键时用这里兜底） ----------
FILM = {
    "win": "#16131f", "card0": "#1d1a2a", "card1": "#15121e",
    "bd": "rgba(255,255,255,0.09)",
    "ink1": "#f4f1ea", "ink2": "#d7d2e0", "ink3": "#9a94a8", "ink4": "#7a7488",
    "field": "#241f33", "canvas": "rgba(255,255,255,0.035)",
    "acc": "#ff8c42", "accd": "#ff6b6b", "acctext": "#1a1320",
    "hl_r": "255", "hl_g": "140", "hl_b": "66",
    "roast": "#ff9f6b",
}


def _remap_tok(tok: dict | None) -> dict:
    """主题 token → 报告浮层键集：同名键直接采用，缺键回退 FILM。

    theme.tok() 没有 card0/card1/roast，按 panel/panel2/acc 派生；显式传入优先。
    """
    merged = dict(FILM)
    if isinstance(tok, dict):
        merged.update({k: v for k, v in tok.items() if v is not None})
        merged["card0"] = tok.get("card0") or tok.get("panel") or FILM["card0"]
        merged["card1"] = tok.get("card1") or tok.get("panel2") or FILM["card1"]
        merged["roast"] = tok.get("roast") or tok.get("acc") or FILM["roast"]
    return merged


# ---------- 嘴替吐槽文案（纯函数，按阈值切档，可测） ----------

def roast_night(ratio: float) -> str:
    if ratio >= 0.45:
        return "这哪是上班，这是修仙 —— 也给自己存一版「睡觉.pptx」吧"
    if ratio >= 0.30:
        return "深夜的灵感是真的，黑眼圈也是真的"
    if ratio >= 0.10:
        return "偶尔修仙，作息基本健康"
    return "作息良好得有点可疑 —— 是不是偷偷换了小号肝？"


def roast_curse(ratio: float) -> str:
    if ratio >= 0.30:
        return "「最终版」是你说过最大的谎 —— 后面还有 final、final2、真的final"
    if ratio >= 0.10:
        return "命名诚信度告急，建议成立「终版打假办公室」"
    return "命名克制，难得的清流"


def roast_weekend(count: int) -> str:
    if count >= 500:
        return "周末？那是用来加班的第六、第七个工作日"
    if count >= 100:
        return "周末偶尔也放不下鼠标"
    return "周末基本能放过自己，给你点个赞"


def roast_zombie(days: int) -> str:
    if days >= 1000:
        return f"吃灰 {days} 天，建议入土为安"
    if days >= 365:
        return f"沉睡 {days} 天，再不打开它要长蘑菇了"
    return ""


class RollNumber(QLabel):
    """大数字滚动累加揭晓（0 → 终值，easeOutCubic）。fmt: 'int' / 'comma' / 'gb'。"""

    def __init__(self, value: float, *, fmt: str = "comma", suffix: str = "",
                 dur: int = 1100, parent=None):
        super().__init__(parent)
        self._value = float(value)
        self._fmt = fmt
        self._suffix = suffix
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(self._value)
        self._anim.setDuration(dur)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(lambda v: self._render(float(v)))
        self._render(0.0)

    def _text(self, v: float) -> str:
        if self._fmt == "gb":
            s = f"{v:.1f}"
        elif self._fmt == "wan":
            s = f"{v / 10000:.0f}万" if v >= 10000 else f"{int(round(v)):,}"
        elif self._fmt == "comma":
            s = f"{int(round(v)):,}"
        else:
            s = f"{int(round(v))}"
        return s + self._suffix

    def _render(self, v: float) -> None:
        self.setText(self._text(v))

    def start(self) -> None:
        from .spotlight import animations_enabled
        if not animations_enabled():
            self.finish()
            return
        self._anim.start()

    def finish(self) -> None:
        """停动画、直接显示终值（导出前调用，避免抓到滚动中途的数字）。"""
        try:
            self._anim.stop()
        except Exception:  # noqa: BLE001
            pass
        self._render(self._value)


# ---------- 浮层 ----------

class ReportOverlay(QWidget):
    """半透明遮罩 + 居中卡片。给定 conn 时支持年度切换（重算重建内容）。"""

    def __init__(self, report, tok, parent=None, *, conn=None, version_db_path=None):
        super().__init__(parent)
        self._tok = _remap_tok(tok)  # 跟随打开时的 app 主题（导出图=所见即所得），缺键回退 FILM
        tok = self._tok
        self._rolls: list[RollNumber] = []
        self._conn = conn
        self._version_db_path = version_db_path
        self._closing_owner = parent
        self.current_report = report
        self.current_year = report.scope_year
        self.current_scope = _SCOPE_YEAR if report.scope_year is not None else _SCOPE_ALL
        initial_key = (self.current_scope, self.current_year, None, None)
        self._report_cache: dict[tuple[str, int | None, float | None, float | None], object] = {
            initial_key: report,
        }
        self._switch_cache_key: tuple[str, int | None, float | None, float | None] | None = None
        self._switch_token = 0
        self._switch_inflight: tuple[int, str, int | None] | None = None
        self._report_ready_for_export = True
        self._export_inflight = False
        self._report_tasks: list[BackgroundTask] = []
        self._scope_buttons: dict[str, QPushButton] = {}
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self._closing = False
        self.setObjectName("reportOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setStyleSheet("#reportOverlay{background:rgba(0,0,0,0.42);}")

        self._card = QFrame()
        self._card.setObjectName("repCard")
        self._card.setStyleSheet(
            "#repCard{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {tok['card0']},stop:1 {tok['card1']});"
            f"border:1px solid {tok['bd']};border-radius:18px;}}")
        self._card_lay = QVBoxLayout(self._card)
        self._card_lay.setContentsMargins(24, 20, 24, 24)
        self._card_lay.setSpacing(13)

        self._build_header()
        self._build_tabs()
        self._content = QWidget()
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setContentsMargins(0, 0, 0, 0)
        self._content_lay.setSpacing(13)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._content)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background:transparent;")
        self._card_lay.addWidget(self._scroll, 1)
        self._fill_content()
        self._resize_report_card()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card, alignment=Qt.AlignCenter)
        if parent is not None:
            self.setGeometry(parent.rect())
            self._resize_report_card()

    def _resize_report_card(self) -> None:
        parent = self.parentWidget()
        w = self.width() or (parent.width() if parent is not None else 980)
        h = self.height() or (parent.height() if parent is not None else 860)
        card_w = min(_CARD_TARGET_WIDTH, max(_CARD_MIN_WIDTH, w - _OVERLAY_MARGIN))
        card_h = min(_SCROLL_MAX_HEIGHT, max(_CARD_MIN_HEIGHT, int(h * _CARD_HEIGHT_RATIO)))
        self._card.setFixedWidth(card_w)
        self._card.setFixedHeight(card_h)
        self._scroll.setMinimumHeight(260)

    def _ui_alive(self) -> bool:
        if self._closing or not _qt_is_valid(self):
            return False
        owner = getattr(self, "_closing_owner", None)
        try:
            return owner is None or not getattr(owner, "_closing", False)
        except RuntimeError:
            return False

    def closeEvent(self, event):  # noqa: N802
        self._closing = True
        self._switch_token += 1
        self._switch_inflight = None
        try:
            parent = self.parent()
            if parent is not None and getattr(parent, "_stats_overlay", None) is self:
                parent._stats_overlay = None
        except RuntimeError:
            pass
        super().closeEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._resize_report_card()

    # ---- 构建 ----
    def _build_header(self) -> None:
        tok = self._tok
        head = QHBoxLayout()
        self._title = QLabel("🎞️ 我的胶片报告")
        self._title.setStyleSheet(
            f"font-size:17px;font-weight:700;color:{tok['ink1']};background:transparent;border:none;")
        head.addWidget(self._title, 1)

        # 范围切换（仅当给定 conn 可重算时）
        if self._conn is not None:
            self._all_btn = QPushButton("全部")
            self._year_btn = QPushButton("本年")
            self._month_btn = QPushButton("本月")
            self._week_btn = QPushButton("本周")
            chip_css = (
                f"QPushButton{{background:{tok['field']};color:{tok['ink3']};border:1px solid {tok['bd']};"
                f"border-radius:9px;padding:3px 11px;font-size:12px;}}"
                f"QPushButton:checked{{background:{tok['sel']};"
                f"color:{tok['acc']};border-color:{tok['acc']};}}")
            for b, scope in (
                (self._all_btn, _SCOPE_ALL),
                (self._year_btn, _SCOPE_YEAR),
                (self._month_btn, _SCOPE_MONTH),
                (self._week_btn, _SCOPE_WEEK),
            ):
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet(chip_css)
                b.clicked.connect(lambda _=False, s=scope: self.switch_scope(s))
                self._scope_buttons[scope] = b
                head.addWidget(b)

        self.copy_btn = QPushButton("复制")
        self.copy_btn.setToolTip("复制当前 Tab 的完整报告图片，可直接粘贴到微信 / 钉钉")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setStyleSheet(
            f"QPushButton{{background:{tok['field']};color:{tok['ink2']};border:1px solid {tok['bd']};"
            f"border-radius:7px;padding:5px 11px;font-weight:600;}}"
            f"QPushButton:hover{{border-color:{tok['acc']};color:{tok['acc']};}}")
        self.copy_btn.clicked.connect(self._copy_clicked)
        head.addWidget(self.copy_btn)
        self.export_btn = QPushButton("导出图片")
        self.export_btn.setStyleSheet(
            f"QPushButton{{background:{tok['acc']};color:{tok['acctext']};border:none;"
            f"border-radius:7px;padding:5px 12px;font-weight:600;}}")
        self.export_btn.clicked.connect(self._export_clicked)
        head.addWidget(self.export_btn)
        self.close_btn = QPushButton("✕")
        self.close_btn.setText("×")
        self.close_btn.setFixedSize(34, 34)
        self.close_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{tok['ink3']};border:none;"
            "font-size:22px;font-weight:600;border-radius:8px;padding:0;}"
            "QPushButton:hover{background:#ff453a;color:#ffffff;}")
        self.close_btn.clicked.connect(self.close)
        head.addWidget(self.close_btn)
        self._card_lay.addLayout(head)

    def _build_tabs(self) -> None:
        """真正的 QTabBar：支持键盘左右键、焦点可见和窄窗口滚动。"""
        tok = self._tok
        self._tab_bar = QTabBar()
        self._tab_bar.setObjectName("reportTabs")
        self._tab_bar.setAccessibleName("胶片报告统计分组")
        self._tab_bar.setExpanding(False)
        self._tab_bar.setUsesScrollButtons(True)
        self._tab_bar.setElideMode(Qt.ElideRight)
        self._tab_bar.setDrawBase(False)
        for key, label in _REPORT_TABS:
            index = self._tab_bar.addTab(label)
            self._tab_bar.setTabData(index, key)
            self._tab_bar.setTabToolTip(index, f"查看{label}统计")
        self._tab_bar.setStyleSheet(
            "QTabBar{background:transparent;border:none;}"
            f"QTabBar::tab{{background:{tok['field']};color:{tok['ink3']};"
            f"border:1px solid {tok['bd']};border-radius:9px;padding:7px 14px;margin-right:6px;"
            "font-size:12px;font-weight:600;min-width:64px;}"
            f"QTabBar::tab:selected{{background:{tok['sel']};color:{tok['acc']};"
            f"border-color:{tok['acc']};}}"
            f"QTabBar::tab:hover{{color:{tok['ink1']};border-color:{tok['ink4']};}}"
            f"QTabBar::tab:focus{{border:2px solid {tok['acc']};padding:6px 13px;}}"
        )
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        self._card_lay.addWidget(self._tab_bar)

    def _on_tab_changed(self, _index: int) -> None:
        if hasattr(self, "_content") and self._ui_alive():
            self._fill_content()

    def _current_tab_key(self) -> str:
        if not hasattr(self, "_tab_bar"):
            return _REPORT_TABS[0][0]
        return str(self._tab_bar.tabData(self._tab_bar.currentIndex()) or _REPORT_TABS[0][0])

    def _current_tab_label(self) -> str:
        if not hasattr(self, "_tab_bar"):
            return _REPORT_TABS[0][1]
        return self._tab_bar.tabText(self._tab_bar.currentIndex())

    def switch_year(self, year: int | None) -> None:
        self.switch_scope(_SCOPE_ALL if year is None else _SCOPE_YEAR, year=year)

    def switch_scope(self, scope: str, *, year: int | None = None) -> None:
        if not self._ui_alive():
            return
        if scope not in _SCOPE_LABELS:
            scope = _SCOPE_ALL
        query_year, since_ts, until_ts = _scope_query(scope, year)
        if scope == self.current_scope and query_year == self.current_year:
            return
        if self._switch_inflight is not None:
            return
        cache_key = (scope, query_year, since_ts, until_ts)
        self.current_scope = scope
        self.current_year = query_year
        cached = self._report_cache.get(cache_key)
        if cached is not None:
            self.current_report = cached
            self._report_ready_for_export = True
            self._set_scope_buttons_enabled(True)
            self.copy_btn.setEnabled(True)
            self.export_btn.setEnabled(not self._export_inflight)
            self._fill_content()
            return
        if self._conn is not None:
            self._switch_token += 1
            token = self._switch_token
            self._switch_inflight = (token, scope, query_year)
            self._switch_cache_key = cache_key
            self._report_ready_for_export = False
            self._set_scope_buttons_enabled(False)
            self.export_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            self._show_loading(scope, query_year)
            task = BackgroundTask(
                lambda year=query_year, since_ts=since_ts, until_ts=until_ts:
                    _build_report_off_ui(
                        self._conn,
                        year=year,
                        since_ts=since_ts,
                        until_ts=until_ts,
                        version_db_path=self._version_db_path,
                    ),
                "stats-report-switch",
                None,
            )
            self._track_report_task(task)
            task.done.connect(
                lambda report, token=token, scope=scope, year=query_year:
                    self._on_report_switched(token, scope, year, report))
            task.finished.connect(
                lambda task=task: self._forget_report_task(task))
            task.start()
        else:
            self._fill_content()

    def _set_scope_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._scope_buttons.values():
            btn.setEnabled(enabled)
        if hasattr(self, "_tab_bar"):
            self._tab_bar.setEnabled(enabled)

    def _set_scope_buttons_checked(self) -> None:
        for scope, btn in self._scope_buttons.items():
            btn.setChecked(scope == self.current_scope)

    def _scope_text(self, scope: str | None = None, year: int | None = None) -> str:
        scope = scope or self.current_scope
        year = self.current_year if year is None else year
        if scope == _SCOPE_YEAR and year is not None:
            return f"{year} 年"
        return _SCOPE_LABELS.get(scope, _SCOPE_LABELS[_SCOPE_ALL])

    def _show_loading(self, scope: str, year: int | None) -> None:
        self._clear_content()
        if self._conn is not None:
            self._set_scope_buttons_checked()
        scope_text = self._scope_text(scope, year)
        lab = QLabel(f"正在生成 {scope_text} 胶片报告…")
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{self._tok['ink3']};background:transparent;border:none;font-size:12.5px;")
        self._content_lay.addWidget(lab)

    def _show_switch_error(self, scope: str, year: int | None) -> None:
        self._clear_content()
        if self._conn is not None:
            self._set_scope_buttons_checked()
        scope_text = self._scope_text(scope, year)
        lab = QLabel(f"{scope_text}胶片报告生成失败，请稍后重试。")
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{self._tok['ink3']};background:transparent;border:none;font-size:12.5px;")
        self._content_lay.addWidget(lab)

    def _on_report_switched(self, token: int, scope: str, year: int | None, report: object) -> None:
        if not self._ui_alive() or token != self._switch_token:
            return
        if self._switch_inflight == (token, scope, year):
            self._switch_inflight = None
        self._set_scope_buttons_enabled(True)
        self.copy_btn.setEnabled(True)
        if report is None:
            self._switch_cache_key = None
            self._show_switch_error(scope, year)
            return
        self.current_scope = scope
        self.current_year = year
        self.current_report = report
        if self._switch_cache_key is not None:
            self._report_cache[self._switch_cache_key] = report
        self._switch_cache_key = None
        self._fill_content()
        self._report_ready_for_export = True
        self.export_btn.setEnabled(not self._export_inflight)

    def _track_report_task(self, task) -> None:
        self._report_tasks.append(task)
        if self._parent_bg_tasks is not None and task not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(task)

    def _forget_report_task(self, task) -> None:
        parent_tasks = self._parent_bg_tasks
        try:
            if _qt_is_valid(self) and task in self._report_tasks:
                self._report_tasks.remove(task)
        except RuntimeError:
            pass
        if parent_tasks is not None and task in parent_tasks:
            parent_tasks.remove(task)


    def _clear_content(self) -> None:
        """立即脱离旧 Tab 组件，避免 deleteLater 前的一帧内容叠影。"""
        while self._content_lay.count():
            it = self._content_lay.takeAt(0)
            widget = it.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _fill_content(self) -> None:
        self._clear_content()
        self._rolls = []
        report = self.current_report
        if self._conn is not None:
            self._set_scope_buttons_checked()

        builders = {
            "overview": self._fill_overview_tab,
            "hall": self._fill_hall_tab,
            "rhythm": self._fill_rhythm_tab,
            "versions": self._fill_versions_tab,
            "content": self._fill_content_tab,
            "library": self._fill_library_tab,
        }
        builders.get(self._current_tab_key(), self._fill_overview_tab)(report)
        self._content_lay.addStretch(1)

        self._content.adjustSize()
        for r in self._rolls:
            r.start()

    def _add_cards(self, *cards: QFrame) -> None:
        for card in cards:
            self._content_lay.addWidget(card)

    def _fill_overview_tab(self, report) -> None:
        self._add_cards(
            self._hero(report),
            self._persona_hero(report.persona),
            self._achievement_card(report),
            self._overview_version_card(report.versions),
            self._activity_card(report.activity),
            self._library_dna_card(report.library_dna, report.deck_count),
        )

    def _fill_hall_tab(self, report) -> None:
        self._add_cards(
            self._hall_of_fame_card(report),
            self._fun_records_card(report),
            self._anniversary_card(report.hall),
        )

    def _fill_rhythm_tab(self, report) -> None:
        self._add_cards(
            self._liver_card(report),
            self._real_save_clock_card(report.versions),
            self._creation_seasons_card(report),
            self._revision_night_card(report.versions),
        )

    def _fill_versions_tab(self, report) -> None:
        self._add_cards(
            self._real_most_edited_card(report.versions),
            self._growth_story_card(report.versions),
            self._version_safety_card(report.versions),
            self._version_fun_card(report.versions),
        )

    def _fill_content_tab(self, report) -> None:
        self._add_cards(
            self._catchphrase_card(report.content),
            self._topic_constellation_card(report.content),
            self._opening_ending_card(report.content),
            self._language_persona_card(report.content),
            self._keyword_trend_card(report.content),
        )

    def _fill_library_tab(self, report) -> None:
        self._add_cards(
            self._library_map_card(report),
            self._shape_distribution_card(report),
            self._filename_dna_card(report.library),
            self._library_fun_card(report),
            self._library_dna_card(report.library_dna, report.deck_count),
            self._scale_card(report.scale),
        )

    # ---- 卡片构建（配色经 self._tok 跟随主题） ----
    def _stat_box(self, value_widget, label: str) -> QFrame:
        tok = self._tok
        box = QFrame()
        box.setStyleSheet(f"background:{tok['canvas']};border:1px solid {tok['bd']};border-radius:13px;")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(13, 11, 13, 11)
        bl.setSpacing(2)
        value_widget.setStyleSheet(
            f"color:{tok['ink1']};background:transparent;border:none;"
            "font-size:22px;font-weight:800;font-family:'Consolas','SF Mono';")
        bl.addWidget(value_widget)
        cap = QLabel(label)
        cap.setStyleSheet(f"color:{tok['ink3']};background:transparent;border:none;font-size:11px;")
        bl.addWidget(cap)
        return box

    def _hero(self, report) -> QFrame:
        tok = self._tok
        sc = report.scale
        scope = self._scope_text()
        f = QFrame()
        f.setStyleSheet("background:transparent;border:none;")
        v = QVBoxLayout(f)
        v.setContentsMargins(2, 2, 2, 0)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        n_decks = RollNumber(report.deck_count, fmt="comma")
        n_chars = RollNumber(sc.total_chars, fmt="wan")
        self._rolls.extend([n_decks, n_chars])
        disk = QLabel(human_bytes(sc.total_bytes))   # 单位多变，用 human_bytes 静态显示
        row.addWidget(self._stat_box(n_decks, "份胶片"))
        row.addWidget(self._stat_box(n_chars, "累计码字"))
        row.addWidget(self._stat_box(disk, "磁盘占用"))
        v.addLayout(row)
        head = QLabel(f"🎬 {scope} · 你和 {report.deck_count} 份胶片，一起肝了这一程")
        head.setWordWrap(True)
        head.setStyleSheet(f"color:{tok['ink3']};background:transparent;border:none;font-size:12px;")
        v.addWidget(head)
        return f

    def _persona_hero(self, p) -> QFrame:
        tok = self._tok
        f = QFrame()
        f.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "stop:0 rgba(255,140,66,0.16),stop:1 rgba(255,107,107,0.07));"
            "border:1px solid rgba(255,140,66,0.32);border-radius:16px;")
        v = QVBoxLayout(f)
        v.setContentsMargins(17, 14, 17, 15)
        v.setSpacing(3)
        cap = QLabel("🏅 你的称号")
        cap.setStyleSheet(
            f"color:{tok['ink3']};background:transparent;border:none;font-size:11.5px;font-weight:700;")
        v.addWidget(cap)
        big = QLabel(p.title)
        big.setStyleSheet(
            f"color:{tok['acc']};background:transparent;border:none;font-size:26px;font-weight:800;")
        v.addWidget(big)
        if p.role:
            role = QLabel(f"· {p.role} ·")
            role.setStyleSheet(f"color:{tok['ink2']};background:transparent;border:none;font-size:13px;")
            v.addWidget(role)
        if p.rhythm and p.output:
            mat = QLabel(f"作息 × 产出定位：<b style='color:{tok['ink2']}'>{p.rhythm} × {p.output}</b>")
            mat.setTextFormat(Qt.RichText)
            mat.setStyleSheet(f"color:{tok['ink3']};background:transparent;border:none;font-size:11.5px;")
            v.addWidget(mat)
        if p.badges:
            chips = "　".join(f"#{b}" for b in p.badges)
            badges = QLabel(chips)
            badges.setStyleSheet(
                f"color:{tok['acc']};background:transparent;border:none;font-size:11.5px;font-weight:700;")
            v.addWidget(badges)
        return f

    def _roast_label(self, text: str) -> QLabel:
        tok = self._tok
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet(
            f"color:{tok['roast']};background:rgba(255,140,66,0.08);border:none;"
            f"border-left:2px solid {tok['acc']};border-top-right-radius:7px;border-bottom-right-radius:7px;"
            "padding:5px 10px;font-size:12px;")
        return lab

    def _liver_card(self, report) -> QFrame:
        from .heatmap import peak_label
        tok = self._tok
        n = report.night
        nlines = [
            f"深夜胶片 <b>{n.night_count}</b> 份（{n.night_ratio:.0%}）　周末打工 <b>{n.weekend_count}</b> 次"
        ]
        if n.latest_name:
            nlines.append(f"最晚一次 <b>{hour_label(n.latest_hour)}</b> 改了《{n.latest_name}》")
        card = self._section("🔥 肝度", nlines)
        lay = card.layout()
        lay.addWidget(self._roast_label(
            roast_night(n.night_ratio) + "　" + roast_weekend(n.weekend_count)))
        hmw = HeatmapWidget(
            report.heatmap,
            accent=(int(tok["hl_r"]), int(tok["hl_g"]), int(tok["hl_b"])),
            empty=tok["field"], ink=tok["ink3"])
        lay.addWidget(hmw)
        foot = QHBoxLayout()
        if hmw.peak > 0 and hmw.peak_wd >= 0:
            call = QLabel(
                f"🕚 <b style='color:{tok['accd']}'>{peak_label(hmw.peak_wd, hmw.peak_hour)}</b> 是你的魔鬼时段")
            call.setTextFormat(Qt.RichText)
            call.setStyleSheet(f"color:{tok['roast']};background:transparent;border:none;font-size:11px;")
            foot.addWidget(call, 1)
        legend = QLabel(
            "少 <span style='color:#ffd479'>■</span>"
            "<span style='color:#ff8c42'>■</span><span style='color:#e8453c'>■</span> 多")
        legend.setTextFormat(Qt.RichText)
        legend.setStyleSheet(f"color:{tok['ink4']};background:transparent;border:none;font-size:10px;")
        foot.addWidget(legend, 0)
        lay.addLayout(foot)
        return card

    def _drama_card(self, d) -> QFrame:
        dlines = []
        if d.top_group_name:
            dlines.append(f"最能改奖：《{d.top_group_name}》存了 <b>{d.top_group_versions}</b> 版")
        dlines.append(
            f"终版诅咒：<b>{d.final_curse_count}</b> 份名带「最终/final/vN」（{d.final_curse_ratio:.0%}）")
        if d.zombie_name:
            days = int((datetime.now().timestamp() - d.zombie_mtime) / 86400) if d.zombie_mtime else 0
            dlines.append(f"僵尸胶片：《{d.zombie_name}》吃灰最久")
        else:
            days = 0
        card = self._section("😅 改版名场面", dlines)
        lay = card.layout()
        lay.addWidget(self._roast_label(roast_curse(d.final_curse_ratio)))
        zr = roast_zombie(days)
        if zr:
            lay.addWidget(self._roast_label(zr))
        return card

    def _activity_card(self, a) -> QFrame:
        if a.peak_month:
            year, month = a.peak_month.split("-", 1)
            peak = f"{year} 年 {int(month)} 月"
        else:
            peak = "暂无"
        lines = [
            f"留下修改足迹 <b>{a.active_days}</b> 天　最长连续开工 <b>{a.longest_streak_days}</b> 天",
            f"最忙月份：<b>{peak}</b>，有 {a.peak_month_count} 份胶片在那个月更新",
        ]
        if a.first_mtime and a.latest_mtime:
            first = datetime.fromtimestamp(a.first_mtime).strftime("%Y-%m-%d")
            latest = datetime.fromtimestamp(a.latest_mtime).strftime("%Y-%m-%d")
            lines.append(f"当前库的修改时间跨度：<b>{first}</b> → <b>{latest}</b>")
        card = self._section("📅 创作足迹", lines)
        card.setObjectName("activityCard")
        card.setToolTip("按当前文件的最后修改时间统计；不是逐次保存日志")
        return card

    def _library_dna_card(self, dna, deck_count: int) -> QFrame:
        lines = [
            f"平均 <b>{dna.avg_pages:.1f}</b> 页/份　每页平均 <b>{dna.avg_chars_per_page:.0f}</b> 字",
            f"短平快（≤5 页）<b>{dna.brief_count}</b> 份　长篇巨制（≥50 页）<b>{dna.epic_count}</b> 份",
            f"复用家族 <b>{dna.family_count}</b> 个，覆盖 <b>{dna.family_deck_count}</b> 份胶片（{dna.family_ratio:.0%}）",
            f"正文索引就绪 <b>{dna.content_ready_count}/{deck_count}</b>（{dna.content_ready_ratio:.0%}）",
        ]
        card = self._section("🧬 胶片 DNA", lines)
        card.setObjectName("libraryDnaCard")
        card.setToolTip("复用家族来自已有近似内容分组；统计复用现有索引，不会重新打开 PPT")
        return card

    def _scale_card(self, sc) -> QFrame:
        slines = []
        if sc.longest_name:
            slines.append(f"最长：《{sc.longest_name}》<b>{sc.longest_pages}</b> 页")
        slines.append(f"累计码字 {sc.total_chars:,} ≈ <b>{redmansion_equiv(sc.total_chars)}</b>")
        slines.append(f"磁盘占用 <b>{human_bytes(sc.total_bytes)}</b>")
        return self._section("📊 规模仓鼠", slines)

    @staticmethod
    def _h(value: object) -> str:
        return html.escape(str(value or ""), quote=True)

    @staticmethod
    def _date(ts: float) -> str:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d") if ts else "暂无"

    @staticmethod
    def _count_items(items, *, limit: int = 8) -> str:
        picked = list(items or ())[:limit]
        if not picked:
            return "暂无足够数据"
        return "　".join(f"<b>{html.escape(str(item.label))}</b> × {item.count}" for item in picked)

    def _achievement_card(self, report) -> QFrame:
        badges = "　".join(f"🏅 {self._h(item)}" for item in report.achievements)
        lines = [self._h(report.one_liner), badges]
        memory = report.hall.today_memory
        if memory.name:
            lines.append(
                f"📽️ 今日胶片回忆：《{self._h(memory.name)}》在 {memory.value} 年前的今天留下修改足迹"
            )
        return self._section("🏆 成就徽章与片库一句话", lines)

    def _overview_version_card(self, versions) -> QFrame:
        if not versions.available:
            return self._section(
                "🛡️ 时光机概览",
                ["当前未连接版本元数据库；文件统计仍可正常使用。"],
            )
        return self._section(
            "🛡️ 时光机概览",
            [
                f"真实保存点 <b>{versions.version_count:,}</b> 个，覆盖 <b>{versions.protected_docs}</b> 份 PPT",
                f"可回退（至少 2 个健康版本）<b>{versions.rollback_docs}</b> 份",
                f"已删除但仍可恢复 <b>{versions.recoverable_deleted_docs}</b> 份",
            ],
        )

    def _award_section(self, title: str, rows: list[tuple[str, object, str]]) -> QFrame:
        """名人堂行可一键在资源管理器定位；没有路径的记录保持纯展示。"""
        tok = self._tok
        card = QFrame()
        card.setStyleSheet(f"background:{tok['canvas']};border:1px solid {tok['bd']};border-radius:12px;")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 13, 16, 14)
        lay.setSpacing(7)
        head = QLabel(title)
        head.setStyleSheet(
            f"font-size:14px;font-weight:700;color:{tok['ink1']};background:transparent;border:none;"
        )
        lay.addWidget(head)
        for label, metric, value_text in rows:
            row = QHBoxLayout()
            row.setSpacing(8)
            name = getattr(metric, "name", None)
            text = f"<b>{self._h(label)}</b>　"
            text += f"《{self._h(name)}》　{value_text}" if name else "暂无"
            value = QLabel(text)
            value.setTextFormat(Qt.RichText)
            value.setWordWrap(True)
            value.setStyleSheet(
                f"font-size:12.5px;color:{tok['ink2']};background:transparent;border:none;"
            )
            row.addWidget(value, 1)
            path = getattr(metric, "path", None)
            if path:
                locate = QPushButton("定位")
                locate.setAccessibleName(f"定位{label}{name}")
                locate.setToolTip(str(path))
                locate.setCursor(Qt.PointingHandCursor)
                locate.setStyleSheet(
                    f"QPushButton{{background:{tok['field']};color:{tok['ink3']};"
                    f"border:1px solid {tok['bd']};border-radius:7px;padding:3px 9px;}}"
                    f"QPushButton:hover{{color:{tok['acc']};border-color:{tok['acc']};}}"
                )
                locate.clicked.connect(lambda _=False, p=str(path): actions.open_folder(p))
                row.addWidget(locate)
            lay.addLayout(row)
        return card

    def _hall_of_fame_card(self, report) -> QFrame:
        h = report.hall
        return self._award_section(
            "🏆 我的 PPT 之最",
            [
                ("页数之最", h.most_pages, f"<b>{int(h.most_pages.value or 0)}</b> 页"),
                ("体积之最", h.largest, f"<b>{human_bytes(float(h.largest.value or 0))}</b>"),
                ("名字最长", h.longest_filename, f"<b>{int(h.longest_filename.value or 0)}</b> 字"),
                ("名字最短", h.shortest_filename, f"<b>{int(h.shortest_filename.value or 0)}</b> 字"),
                ("最老胶片", h.oldest, f"修改于 <b>{self._date(float(h.oldest.value or 0))}</b>"),
                ("最新胶片", h.newest, f"修改于 <b>{self._date(float(h.newest.value or 0))}</b>"),
                ("目录最深", h.deepest_path, f"路径深度 <b>{int(h.deepest_path.value or 0)}</b> 层"),
            ],
        )

    def _fun_records_card(self, report) -> QFrame:
        h = report.hall
        lines = [
            f"一日爆肝纪录：<b>{self._h(h.busiest_day.name or '暂无')}</b> 一天更新 {int(h.busiest_day.value or 0)} 份",
            f"最常见页数：<b>{h.common_page_count}</b> 页，共 {h.common_page_count_decks} 份",
            f"终版诅咒：{report.drama.final_curse_count} 份名字含 最终/final/vN",
        ]
        return self._section("🎯 趣味纪录", lines)

    def _anniversary_card(self, hall) -> QFrame:
        lines = []
        if hall.today_memory.name:
            lines.append(
                f"今日胶片回忆：《{self._h(hall.today_memory.name)}》—— {int(hall.today_memory.value)} 年前的今天"
            )
        for item in hall.anniversaries:
            lines.append(f"周年纪念：《{self._h(item.name)}》 · {int(item.value)} 周年 · {self._h(item.detail)}")
        if not lines:
            lines.append("今天暂时没有同日往年的胶片回忆，明天再来会换一批彩蛋。")
        return self._section("🎂 今日回忆与周年纪念", lines)

    def _real_save_clock_card(self, versions) -> QFrame:
        lines = []
        if versions.available:
            lines.append(f"按真实自动快照统计，共 <b>{versions.version_count}</b> 次保存留版；不是拿文件修改时间代替。")
        else:
            lines.append("版本元数据库暂不可用，因此不会拿文件修改时间冒充真实保存记录。")
        card = self._section("⏱️ 真实保存时钟", lines)
        if versions.available:
            hmw = HeatmapWidget(
                versions.save_heatmap,
                accent=(int(self._tok["hl_r"]), int(self._tok["hl_g"]), int(self._tok["hl_b"])),
                empty=self._tok["field"],
                ink=self._tok["ink3"],
            )
            card.layout().addWidget(hmw)
        return card

    def _creation_seasons_card(self, report) -> QFrame:
        creation = report.creation
        seasons = "　".join(f"<b>{i.label}</b> {i.count}" for i in creation.season_counts)
        years = self._count_items(creation.yearly_counts[-8:], limit=8)
        months = self._count_items(creation.monthly_counts[-12:], limit=12)
        lines = [
            f"创作年轮：{years}",
            f"四季分布：{seasons or '暂无'}",
            f"最近月份：{months}",
            f"活跃 <b>{report.activity.active_days}</b> 天，最长连续开工 <b>{report.activity.longest_streak_days}</b> 天",
        ]
        return self._section("🌳 创作年轮与旺季", lines)

    def _revision_night_card(self, versions) -> QFrame:
        if not versions.available:
            return self._section("🌙 年度最大改稿夜与冲刺榜", ["需要版本快照后才能统计真实改稿夜。"])
        lines = [
            f"年度最大改稿夜：<b>{self._h(versions.peak_revision_night or '暂无')}</b>，留版 {versions.peak_revision_night_count} 次"
        ]
        for i, sprint in enumerate(versions.revision_sprints[:5], 1):
            lines.append(
                f"改稿冲刺榜 #{i}：《{self._h(sprint.name)}》72 小时内留版 <b>{sprint.count}</b> 次"
            )
        if len(lines) == 1:
            lines.append("还没有形成两次以上的连续改稿冲刺。")
        return self._section("🌙 年度最大改稿夜与冲刺榜", lines)

    def _real_most_edited_card(self, versions) -> QFrame:
        if not versions.available:
            return self._section(
                "🕰️ 真正的「最能改奖」",
                ["版本库未连接；这里不再用相似文件分组冒充版本次数。"],
            )
        if not versions.most_edited_name:
            return self._section("🕰️ 真正的「最能改奖」", ["暂无真实版本快照。"])
        return self._section(
            "🕰️ 真正的「最能改奖」",
            [
                f"《{self._h(versions.most_edited_name)}》留下 <b>{versions.most_edited_versions}</b> 个真实快照",
                f"当前范围共有 <b>{versions.version_count}</b> 个健康版本点，覆盖 {versions.protected_docs} 份 PPT",
            ],
        )

    def _growth_story_card(self, versions) -> QFrame:
        if not versions.growth_points:
            return self._section("📈 一份 PPT 的成长史", ["至少留下一个真实版本后，这里会出现页数与体积的成长轨迹。"])
        first, last = versions.growth_points[0], versions.growth_points[-1]
        lines = [
            f"主角：《{self._h(versions.most_edited_name)}》 · 展示最近 {len(versions.growth_points)} 个点",
            f"页数：<b>{first.page_count}</b> → <b>{last.page_count}</b>　体积：{human_bytes(first.size)} → {human_bytes(last.size)}",
        ]
        recent = versions.growth_points[-8:]
        lines.append("最近轨迹：" + "　".join(
            f"{datetime.fromtimestamp(p.ts).strftime('%m-%d')}·{p.page_count}页" for p in recent
        ))
        return self._section("📈 一份 PPT 的成长史", lines)

    def _version_safety_card(self, versions) -> QFrame:
        if not versions.available:
            return self._section("🛟 被时光机救下的胶片", ["版本库未连接。"])
        return self._section(
            "🛟 被时光机救下的胶片",
            [
                f"已删除但仍有健康恢复点：<b>{versions.recoverable_deleted_docs}</b> 份",
                f"真正可回退（≥2 版）：<b>{versions.rollback_docs}</b> 份",
                f"越改越长 <b>{versions.growing_docs}</b> 份　越改越瘦 <b>{versions.slimming_docs}</b> 份",
            ],
        )

    def _version_fun_card(self, versions) -> QFrame:
        lines = []
        if versions.biggest_revision_name:
            lines.append(
                f"最大单次改稿：《{self._h(versions.biggest_revision_name)}》 · "
                f"{self._date(versions.biggest_revision_ts)} · 强度 {versions.biggest_revision_score}"
                + (f" · {self._h(versions.biggest_revision_summary)}" if versions.biggest_revision_summary else "")
            )
        if versions.sleeping_revival_name:
            lines.append(
                f"沉睡后复活：《{self._h(versions.sleeping_revival_name)}》隔了 <b>{versions.sleeping_revival_days}</b> 天再改"
            )
        lines.extend([
            f"最能改名：《{self._h(versions.most_renamed_name or '暂无')}》出现 {versions.most_renamed_count} 个名字",
            f"迁徙最远：《{self._h(versions.most_migrated_name or '暂无')}》待过 {versions.most_migrated_count} 个目录",
            f"页数反复横跳：《{self._h(versions.page_flip_flop_name or '暂无')}》方向切换 {versions.page_flip_flops} 次",
        ])
        return self._section("🎢 改稿奇闻", lines)

    def _sample_note(self, content) -> str:
        mode = "有界抽样" if content.sample_truncated else "全量索引"
        return (
            f"{mode}：{content.sampled_decks} 份 / {content.sampled_pages} 页 / "
            f"{content.sampled_chars:,} 字；全程本地，不打开 PPT。"
        )

    def _catchphrase_card(self, content) -> QFrame:
        return self._section(
            "💬 我的 PPT 口头禅",
            [self._count_items(content.catchphrases, limit=10), self._sample_note(content)],
        )

    def _topic_constellation_card(self, content) -> QFrame:
        return self._section(
            "✨ 我的主题星座",
            [
                self._count_items(content.topics, limit=10),
                "星座按“覆盖了多少份 PPT”排序，避免某一份超长文档霸榜。",
            ],
        )

    def _opening_ending_card(self, content) -> QFrame:
        lines = [
            f"开场仪式：<b>{self._h(content.opening_phrase or '暂无')}</b> · {content.opening_count} 次",
            f"收尾仪式：<b>{self._h(content.ending_phrase or '暂无')}</b> · {content.ending_count} 次",
            f"最常重复的一句话：<b>{self._h(content.repeated_sentence or '暂无')}</b> · {content.repeated_sentence_count} 次",
        ]
        return self._section("🎬 开场、收尾与复读机", lines)

    def _language_persona_card(self, content) -> QFrame:
        return self._section(
            "🗣️ 内容语言人格",
            [
                f"你的内容人格：<b>{self._h(content.language_persona)}</b>",
                f"英文字母占比 {content.english_ratio:.1%}　数字占比 {content.digit_ratio:.1%}　问号 {content.question_marks} 个",
                f"低文字收尾 <b>{content.low_text_ending_count}</b> 份（≤30 字，完整的一页讲完统计见片库版图）",
            ],
        )

    def _keyword_trend_card(self, content) -> QFrame:
        lines = [
            f"<b>{self._h(item.period)}</b>　" + " / ".join(self._h(term) for term in item.terms)
            for item in content.keyword_trends
        ]
        return self._section("🌊 关键词趋势河流", lines or ["暂无跨月份内容趋势。"])

    def _library_map_card(self, report) -> QFrame:
        folders = report.library.top_folders
        compact = []
        for item in folders[:8]:
            parts = [p for p in str(item.label).replace("/", "\\").split("\\") if p]
            label = "\\".join(parts[-2:]) if parts else str(item.label)
            if len(parts) > 2:
                label = "…\\" + label
            compact.append(f"<b>{self._h(label)}</b> × {item.count}")
        lines = ["　".join(compact) or "暂无目录数据"]
        if report.hall.deepest_path.name:
            lines.append(
                f"目录最深：《{self._h(report.hall.deepest_path.name)}》 · {int(report.hall.deepest_path.value)} 层"
            )
        card = self._section("🗺️ 我的胶片版图", lines)
        if folders:
            card.setToolTip("\n".join(f"{item.label} × {item.count}" for item in folders[:8]))
        return card

    def _shape_distribution_card(self, report) -> QFrame:
        bins = report.library.shape_bins
        distribution = "　".join(f"<b>{self._h(label)}</b> {count}" for label, count in bins.items())
        return self._section(
            "📐 胶片身材分布",
            [
                distribution or "暂无",
                f"最常见页数：<b>{report.hall.common_page_count}</b> 页 · {report.hall.common_page_count_decks} 份",
            ],
        )

    def _filename_dna_card(self, library) -> QFrame:
        examples = "、".join(self._h(name) for name in library.same_name_examples) or "暂无"
        generic = "、".join(self._h(name) for name in library.generic_name_examples) or "暂无"
        return self._section(
            "🧬 文件名 DNA",
            [
                "高频词：" + self._count_items(library.filename_terms, limit=10),
                f"同名双胞胎：{library.same_name_twin_groups} 组 / {library.same_name_twin_files} 份 · {examples}",
                f"默认名俱乐部：{library.generic_name_count} 份 · {generic}",
                f"标点人格：<b>{self._h(library.punctuation_label)}</b>",
            ],
        )

    def _library_fun_card(self, report) -> QFrame:
        minutes = report.library.meeting_minutes
        hours, remain = divmod(minutes, 60)
        return self._section(
            "🎞️ 片库脑洞换算",
            [
                f"全部讲完预计 <b>{hours} 小时 {remain} 分</b>（按每页 2 分钟估算）",
                f"全部打印约摞成 <b>{report.library.paper_height_mm:.1f} mm</b> 高（按每张 0.1 mm）",
                f"一页讲完 <b>{report.library.one_page_count}</b> 份　低文字收尾 <b>{report.content.low_text_ending_count}</b> 份",
            ],
        )

    def _finish_rolls(self) -> None:
        for r in getattr(self, "_rolls", []):
            try:
                r.finish()
            except Exception:  # noqa: BLE001
                pass

    def _grab_full_report_pixmap(self):
        """Grab the fixed-height card as a full report image, including scrolled content."""
        self._finish_rolls()
        self._content_lay.activate()
        self._content.adjustSize()
        self._card_lay.activate()

        app = QApplication.instance()
        if app is not None:
            app.processEvents()

        content_min = self._content.minimumSize()
        content_max = self._content.maximumSize()
        content_size = self._content.size()
        scroll_value = self._scroll.verticalScrollBar().value()

        try:
            margins = self._card_lay.contentsMargins()
            top_to_scroll = self._scroll.geometry().top()
            if top_to_scroll <= 0:
                header_item = self._card_lay.itemAt(0)
                header_h = header_item.sizeHint().height() if header_item is not None else 0
                top_to_scroll = margins.top() + header_h + self._card_lay.spacing()

            content_hint = self._content_lay.sizeHint()
            content_w = max(self._scroll.viewport().width(), self._content.width(), content_hint.width())
            content_h = max(content_hint.height(), self._content.sizeHint().height(), self._content.height())
            scroll_x = self._scroll.geometry().left()
            full_w = max(self._card.width(), scroll_x + content_w + margins.right())
            full_h = max(top_to_scroll + content_h + margins.bottom(), self._card.height())

            self._scroll.verticalScrollBar().setValue(0)
            self._content.setMinimumSize(content_w, content_h)
            self._content.resize(content_w, content_h)
            self._content_lay.activate()
            if app is not None:
                app.processEvents()

            dpr = max(1.0, float(self._card.devicePixelRatioF()))
            pixmap = QPixmap(int(full_w * dpr), int(full_h * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            try:
                bg = QFrame()
                bg.setObjectName("repCard")
                bg.setAttribute(Qt.WA_StyledBackground, True)
                bg.setStyleSheet(self._card.styleSheet())
                bg.resize(full_w, full_h)
                bg.ensurePolished()
                bg.render(painter, QPoint(0, 0))

                header_h = min(top_to_scroll, self._card.height())
                self._card.render(
                    painter,
                    QPoint(0, 0),
                    QRegion(QRect(0, 0, self._card.width(), header_h)),
                )
                self._content.render(painter, QPoint(scroll_x, top_to_scroll))
            finally:
                painter.end()
            return pixmap
        finally:
            self._content.setMinimumSize(content_min)
            self._content.setMaximumSize(content_max)
            self._content.resize(content_size)
            self._scroll.verticalScrollBar().setValue(scroll_value)
            self._card_lay.activate()

    def _copy_clicked(self) -> None:
        if not self._ui_alive() or self._switch_inflight is not None:
            return   # 年度切换重算中：内容是 loading 占位，别抓到空卡
        QApplication.clipboard().setPixmap(self._grab_full_report_pixmap())
        QMessageBox.information(
            self,
            "复制图片",
            f"已复制“{self._current_tab_label()}”完整图片，可粘贴到微信 / 钉钉",
        )

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
    def _export_clicked(self) -> None:
        if not self._ui_alive() or self._export_inflight or not self.export_btn.isEnabled():
            return
        scope = self._scope_text().replace(" ", "")
        tab = self._current_tab_label().replace(" ", "")
        default_name = f"胶片报告_{scope}_{tab}.png"
        path, _ = QFileDialog.getSaveFileName(self, "导出胶片报告图片", default_name, "PNG Image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path = f"{path}.png"
        image = self._grab_full_report_pixmap().toImage()
        self._export_inflight = True
        self.export_btn.setEnabled(False)
        old_text = self.export_btn.text()
        self.export_btn.setText("导出中…")
        task = BackgroundTask(
            lambda image=image, path=path: bool(image.save(path)),
            "stats-report-export",
            None,
        )
        self._track_report_task(task)
        task.done.connect(lambda ok, path=path, old_text=old_text: self._on_export_done(path, old_text, ok))
        task.finished.connect(lambda task=task: self._forget_report_task(task))
        task.start()

    def _on_export_done(self, path: str, old_text: str, ok: object) -> None:
        if not self._ui_alive():
            return
        self._export_inflight = False
        self.export_btn.setText(old_text)
        if self._report_ready_for_export:
            self.export_btn.setEnabled(True)
        QMessageBox.information(self, "导出图片", "已导出图片" if ok else "导出失败")

    def export_png(self, path: str) -> bool:
        return bool(self._grab_full_report_pixmap().save(path))

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):  # noqa: N802
        if self.childAt(e.position().toPoint()) is None:
            self.close()
        super().mousePressEvent(e)
