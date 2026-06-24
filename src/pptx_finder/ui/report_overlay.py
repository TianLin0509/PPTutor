"""报告浮层 ReportOverlay：盖在主界面上的「我的胶片报告」卡片流。

- 文案格式化纯函数（human_bytes / redmansion_equiv / hour_label）便于单测。
- 跟随主题（tok 传入），用内联样式而非全局 QSS（不依赖 theme 模块，互不冲突）。
- 给定 conn 时 header 提供「全部 / 本年」年度切换；导出 PNG；Esc / 点遮罩关闭。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import db
from .. import stats
from .bg_task import BackgroundTask
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


def _conn_path(conn) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None
        return row["file"] if hasattr(row, "keys") else row[2]
    except Exception:  # noqa: BLE001
        return None


def _build_report_off_ui(conn, *, year=None):
    path = _conn_path(conn)
    if path:
        own = db.connect(path)
        try:
            return stats.build_report(own, year=year)
        finally:
            own.close()
    return stats.build_report(conn, year=year)


# ---------- 固定暗黑胶片质感（不跟随 app 主题，保证报告/导出图始终高级一致） ----------
FILM = {
    "win": "#16131f", "card0": "#1d1a2a", "card1": "#15121e",
    "bd": "rgba(255,255,255,0.09)",
    "ink1": "#f4f1ea", "ink2": "#d7d2e0", "ink3": "#9a94a8", "ink4": "#7a7488",
    "field": "#241f33", "canvas": "rgba(255,255,255,0.035)",
    "acc": "#ff8c42", "accd": "#ff6b6b", "acctext": "#1a1320",
    "hl_r": "255", "hl_g": "140", "hl_b": "66",
    "roast": "#ff9f6b",
}


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

    def __init__(self, report, tok, parent=None, *, conn=None):
        super().__init__(parent)
        tok = FILM  # 固定暗黑胶片质感，忽略传入主题（报告自成一套，导出图也始终高级一致）
        self._tok = tok
        self._rolls: list[RollNumber] = []
        self._conn = conn
        self._closing_owner = parent
        self.current_report = report
        self.current_year = report.scope_year
        self._switch_token = 0
        self._switch_inflight: tuple[int, int | None] | None = None
        self._report_ready_for_export = True
        self._export_inflight = False
        self._report_tasks: list[BackgroundTask] = []
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
        self._card.setFixedWidth(480)
        self._card.setStyleSheet(
            "#repCard{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {tok['card0']},stop:1 {tok['card1']});"
            f"border:1px solid {tok['bd']};border-radius:18px;}}")
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

        self.copy_btn = QPushButton("复制")
        self.copy_btn.setToolTip("复制报告图片到剪贴板，可直接粘贴到微信 / 钉钉")
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
            "font-size:22px;font-weight:600;border-radius:8px;padding:0;}}"
            "QPushButton:hover{background:rgba(255,69,58,0.9);color:#ffffff;}")
        self.close_btn.clicked.connect(self.close)
        head.addWidget(self.close_btn)
        self._card_lay.addLayout(head)

    def switch_year(self, year: int | None) -> None:
        if not self._ui_alive():
            return
        if year == self.current_year and self.current_report.scope_year == year:
            return
        if self._switch_inflight is not None:
            return
        self.current_year = year
        if self._conn is not None:
            self._switch_token += 1
            token = self._switch_token
            self._switch_inflight = (token, year)
            self._report_ready_for_export = False
            self._set_year_buttons_enabled(False)
            self.export_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            self._show_loading(year)
            task = BackgroundTask(
                lambda year=year: _build_report_off_ui(self._conn, year=year),
                "stats-report-switch",
                None,
            )
            self._track_report_task(task)
            task.done.connect(
                lambda report, token=token, year=year: self._on_report_switched(token, year, report))
            task.finished.connect(
                lambda task=task: self._forget_report_task(task))
            task.start()
        else:
            self._fill_content()

    def _set_year_buttons_enabled(self, enabled: bool) -> None:
        for btn_name in ("_all_btn", "_year_btn"):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                btn.setEnabled(enabled)

    def _show_loading(self, year: int | None) -> None:
        while self._content_lay.count():
            it = self._content_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if self._conn is not None:
            self._all_btn.setChecked(year is None)
            self._year_btn.setChecked(year is not None)
        scope = f"{year} 年" if year else "全部历史"
        lab = QLabel(f"正在生成 {scope} 胶片报告…")
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{self._tok['ink3']};background:transparent;border:none;font-size:12.5px;")
        self._content_lay.addWidget(lab)

    def _show_switch_error(self, year: int | None) -> None:
        while self._content_lay.count():
            it = self._content_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if self._conn is not None:
            self._all_btn.setChecked(year is None)
            self._year_btn.setChecked(year is not None)
        scope = f"{year} 年" if year else "全部历史"
        lab = QLabel(f"{scope}胶片报告生成失败，请稍后重试。")
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{self._tok['ink3']};background:transparent;border:none;font-size:12.5px;")
        self._content_lay.addWidget(lab)

    def _on_report_switched(self, token: int, year: int | None, report: object) -> None:
        if not self._ui_alive() or token != self._switch_token:
            return
        if self._switch_inflight == (token, year):
            self._switch_inflight = None
        self._set_year_buttons_enabled(True)
        self.copy_btn.setEnabled(True)
        if report is None:
            self._show_switch_error(year)
            return
        self.current_year = year
        self.current_report = report
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


    def _fill_content(self) -> None:
        while self._content_lay.count():
            it = self._content_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._rolls = []
        report = self.current_report
        if self._conn is not None:
            self._all_btn.setChecked(self.current_year is None)
            self._year_btn.setChecked(self.current_year is not None)

        self._content_lay.addWidget(self._hero(report))
        self._content_lay.addWidget(self._persona_hero(report.persona))   # 称号前置：最该被晒的一张
        self._content_lay.addWidget(self._liver_card(report))
        self._content_lay.addWidget(self._drama_card(report.drama))
        self._content_lay.addWidget(self._scale_card(report.scale))

        self._card.adjustSize()
        for r in self._rolls:
            r.start()

    # ---- 卡片构建（暗黑胶片质感） ----
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
        scope = f"{self.current_year} 年" if self.current_year else "全部历史"
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

    def _scale_card(self, sc) -> QFrame:
        slines = []
        if sc.longest_name:
            slines.append(f"最长：《{sc.longest_name}》<b>{sc.longest_pages}</b> 页")
        slines.append(f"累计码字 {sc.total_chars:,} ≈ <b>{redmansion_equiv(sc.total_chars)}</b>")
        slines.append(f"磁盘占用 <b>{human_bytes(sc.total_bytes)}</b>")
        return self._section("📊 规模仓鼠", slines)

    def _finish_rolls(self) -> None:
        for r in getattr(self, "_rolls", []):
            try:
                r.finish()
            except Exception:  # noqa: BLE001
                pass

    def _copy_clicked(self) -> None:
        if not self._ui_alive() or self._switch_inflight is not None:
            return   # 年度切换重算中：内容是 loading 占位，别抓到空卡
        self._finish_rolls()
        self._card.adjustSize()
        QApplication.clipboard().setPixmap(self._card.grab())
        QMessageBox.information(self, "复制图片", "已复制报告图片，可粘贴到微信 / 钉钉")

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
        scope = str(self.current_year) if self.current_year is not None else "全部历史"
        default_name = f"胶片报告_{scope}.png"
        path, _ = QFileDialog.getSaveFileName(self, "导出胶片报告图片", default_name, "PNG Image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path = f"{path}.png"
        self._finish_rolls()
        self._card.adjustSize()
        image = self._card.grab().toImage()
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
        self._finish_rolls()
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
