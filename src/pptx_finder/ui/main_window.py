"""主窗口：双主题 + 精致结果项（焦点双态/半透明高亮/字重层级）+ P0 交互
（即时搜索 / 全键盘导航 / 命中页缩略图条 / 索引进度条）。

可测试性：conn 与 render_worker 可注入；do_index=False 时不启动磁盘索引。
"""
from __future__ import annotations

import datetime
import html
import json
import os

from PySide6.QtCore import QEvent, QMimeData, QPropertyAnimation, Qt, QStringListModel, QTimer, QUrl
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from .. import actions, db, history, search as search_mod
from ..config import GLOBAL_HOTKEY, data_dir, db_path as cfg_db_path
from ..models import FileResult
from . import theme
from .index_worker import IndexWorker
from .render_worker import RenderWorker


def _make_icon(draw, color: str = "#8A8A8A", size: int = 18) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 1.7)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    draw(p)
    p.end()
    return QIcon(pm)


def _icon_search() -> QIcon:
    return _make_icon(lambda p: (p.drawEllipse(3, 3, 8, 8), p.drawLine(10, 10, 15, 15)))


def _icon_clear() -> QIcon:
    return _make_icon(lambda p: (p.drawLine(5, 5, 13, 13), p.drawLine(13, 5, 5, 13)))


def _app_logo() -> QPixmap:
    """品牌 logo：胶片帧（暖金）+ 搜索镜（电蓝），固定品牌色，深浅主题通用。"""
    pm = QPixmap(26, 26)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#E3B572"), 2))
    p.drawRoundedRect(3, 5, 18, 15, 4, 4)
    p.setPen(QPen(QColor("#5D9BFF"), 2))
    p.drawEllipse(8, 9, 7, 7)
    p.drawLine(13, 14, 18, 19)
    p.end()
    return pm


def _load_theme() -> str:
    try:
        p = data_dir() / "ui.json"
        if p.exists():
            return json.loads(p.read_text("utf-8")).get("theme", "cloud")
    except Exception:  # noqa: BLE001
        pass
    return "cloud"


def _save_theme(name: str) -> None:
    try:
        (data_dir() / "ui.json").write_text(json.dumps({"theme": name}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _highlight(snippet: str, hlcss: str) -> str:
    """把片段里的【命中】转成半透明底高亮（不变色不加粗）。"""
    s = html.escape(snippet)
    s = s.replace("【", f'<span style="{hlcss}">').replace("】", "</span>")
    return s


def _fmt_mtime(ts: float) -> str:
    """修改时间：同年 'MM-DD HH:MM'，跨年 'YYYY-MM-DD'。"""
    try:
        dt = datetime.datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return ""
    if dt.year == datetime.datetime.now().year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def _fmt_size(n: int) -> str:
    """字节数转人类可读：'2.3 MB' / '456 KB' / '18 B'。"""
    if not n or n <= 0:
        return ""
    f = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def _elide_middle(s: str, maxlen: int = 72) -> str:
    """路径过长时中间省略，保留盘符与文件名两端。"""
    if len(s) <= maxlen:
        return s
    head = (maxlen - 1) * 2 // 3
    tail = maxlen - 1 - head
    return s[:head] + "…" + s[-tail:]


def _empty_suggestions(query: str, mode: str) -> list[str]:
    """零结果时适用的补救建议 key：去引号 / 减词 / 改搜文件名。"""
    s = []
    if any(q in query for q in ('"', '“', '”')):
        s.append("unquote")
    if len(query.split()) > 1:
        s.append("fewer")
    if mode != "仅文件名":
        s.append("filename")
    return s


def _sort_results(results: list, key: str) -> list:
    """结果排序：relevance 保持原序 / recent 按 mtime 降序 / name 按文件名升序。"""
    if key == "recent":
        return sorted(results, key=lambda r: r.mtime, reverse=True)
    if key == "name":
        return sorted(results, key=lambda r: r.name.lower())
    return list(results)


def _time_bucket(mtime: float, now_ts: float) -> str:
    """按 mtime 归入时间桶：今天 / 昨天 / 本周 / 本月 / 更早。"""
    now = datetime.datetime.fromtimestamp(now_ts)
    try:
        dt = datetime.datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return "更早"
    d = (now.date() - dt.date()).days
    if d <= 0:
        return "今天"
    if d == 1:
        return "昨天"
    if d < 7:
        return "本周"
    if d < 30:
        return "本月"
    return "更早"


def group_by_time(results: list, now_ts: float) -> list:
    """按 mtime 分时间桶，保持输入顺序。返回 [(label, [items]), ...]。"""
    buckets: dict[str, list] = {}
    order: list[str] = []
    for r in results:
        label = _time_bucket(r.mtime, now_ts)
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(r)
    return [(label, buckets[label]) for label in order]


class ResultItem(QWidget):
    """单条结果：文件名(SemiBold) + 命中页胶囊 + 版本/最新徽章 + 高亮片段 + 路径(mono灰)。"""

    def __init__(self, r: FileResult, tok: dict, hlcss: str):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._tok = tok
        self._sel = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 9, 12, 9)
        lay.setSpacing(4)

        # 第 1 行：文件名 + 命中页徽章（P1 / P3 P8，最多 3 个）
        row = QHBoxLayout()
        row.setSpacing(6)
        fn = QLabel(html.escape(r.name))
        fn.setStyleSheet(f"font-size:14px;font-weight:600;color:{tok['ink1']};background:transparent;")
        fn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(fn, 1)

        if r.hits:
            for h in r.hits[:3]:
                pg = QLabel(f"P{h.page_no}")
                pg.setStyleSheet(
                    f"font-size:11px;font-weight:700;color:{tok['acc']};"
                    f"background:rgba({tok['hl_r']},{tok['hl_g']},{tok['hl_b']},0.15);"
                    "border-radius:6px;padding:1px 7px;")
                row.addWidget(pg, 0)
        elif r.name_hit:
            nh = QLabel("文件名命中")
            nh.setStyleSheet(
                f"font-size:10.5px;font-weight:700;color:{tok['grn']};"
                f"border:1px solid {tok['bd2']};border-radius:6px;padding:1px 7px;background:transparent;")
            row.addWidget(nh, 0)
        if r.status == "filename_only":
            ext = QLabel(".ppt")
            ext.setStyleSheet(f"font-size:10px;color:{tok['ink4']};border:1px solid {tok['bd2']};border-radius:5px;padding:1px 6px;background:transparent;")
            row.addWidget(ext, 0)
        lay.addLayout(row)

        # 第 2 行：高亮片段（内容命中）/ 老格式说明（.ppt）
        if r.hits and r.hits[0].snippet:
            sn = QLabel(_highlight(r.hits[0].snippet, hlcss))
            sn.setTextFormat(Qt.RichText)
            sn.setStyleSheet(f"font-size:12px;color:{tok['ink2']};background:transparent;")
            sn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(sn)
        elif r.status == "filename_only":
            sub = QLabel("老格式 · 仅文件名搜索与预览")
            sub.setStyleSheet(f"font-size:11.5px;color:{tok['ink4']};background:transparent;")
            lay.addWidget(sub)

        # 第 3 行：修改时间（显式体现新旧）
        tm = _fmt_mtime(r.mtime)
        if tm:
            t = QLabel(tm)
            t.setStyleSheet(f"font-size:11px;color:{tok['ink3']};background:transparent;")
            lay.addWidget(t)
        self._apply("normal", True)

    def enterEvent(self, e):  # noqa: N802
        if not self._sel:
            self._apply("hover", True)

    def leaveEvent(self, e):  # noqa: N802
        if not self._sel:
            self._apply("normal", True)

    def set_selected(self, sel: bool, active: bool = True) -> None:
        self._sel = sel
        self._apply("sel" if sel else "normal", active)

    def _apply(self, state: str, active: bool) -> None:
        t = self._tok
        if state == "sel":
            bg = t["sel"] if active else t["selblur"]
            bar = t["acc"] if active else t["ink4"]
        elif state == "hover":
            bg, bar = t["hover"], t["bd2"]
        else:
            bg, bar = "transparent", "transparent"
        self.setStyleSheet(f"ResultItem{{background:{bg};border-radius:{t['radius']}px;border-left:3px solid {bar};}}")


class MainWindow(QMainWindow):
    def __init__(self, conn=None, render_worker=None, do_index=True,
                 roots: list[str] | None = None, workers: int | None = None):
        super().__init__()
        self.setWindowTitle("pptx-finder · PPTX 查询助手   v0.4.1")
        self.resize(1180, 760)

        self._theme = _load_theme()
        self._tok = theme.tok(self._theme)
        self._db_path = str(cfg_db_path())
        self._conn = conn or db.connect(self._db_path)
        db.init_db(self._conn)

        self._results: list[FileResult] = []
        self._results_raw: list[FileResult] = []  # 排序前原始序（relevance 基准）
        self._showing_recent = False  # 当前是否为「空查询默认视图（最近文件）」
        self._cur: FileResult | None = None
        self._cur_item_widget: ResultItem | None = None
        self._hit_idx = 0
        self._view_page = 1  # 当前预览页（原始页序，滚轮可脱离命中页自由翻）
        self._req_id = 0
        self._cur_pixmap: QPixmap | None = None
        self._zoom = 1.0  # 预览缩放：1.0=适配窗口，>1 放大看细节
        self._to_tray_on_close = False
        self._thumb_btns: list[QToolButton] = []

        self._render = render_worker or RenderWorker(self)
        self._render.rendered.connect(self._on_rendered)
        self._owns_render = render_worker is None
        if self._owns_render:
            self._render.start()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(280)
        self._debounce.timeout.connect(self._do_search)

        self._build_ui()
        self._apply_theme(self._theme, persist=False)

        self._indexer: IndexWorker | None = None
        if do_index:
            self._start_indexing(roots, workers)
        else:
            self._refresh_status()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QWidget()
        top.setObjectName("topBar")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(13, 12, 13, 11)
        tl.setSpacing(0)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        logo = QLabel()
        logo.setObjectName("appLogo")
        logo.setPixmap(_app_logo())
        logo.setToolTip("pptx-finder")
        bar.addWidget(logo)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("输入你记得的文字 / 文件名…（多词空格=同时含，\"引号\"=精确短语）")
        self.search_box.setMinimumHeight(42)
        self.search_box.addAction(_icon_search(), QLineEdit.LeadingPosition)
        self._clear_act = self.search_box.addAction(_icon_clear(), QLineEdit.TrailingPosition)
        self._clear_act.setVisible(False)
        self._clear_act.setToolTip("清空")
        self._clear_act.triggered.connect(self.search_box.clear)
        self.search_box.textChanged.connect(lambda: self._debounce.start())
        self.search_box.textChanged.connect(lambda t: self._clear_act.setVisible(bool(t)))
        self.search_box.installEventFilter(self)
        self._history_model = QStringListModel(self)
        self._completer = QCompleter(self._history_model, self)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.search_box.setCompleter(self._completer)
        self._completer.popup().setObjectName("historyPopup")
        bar.addWidget(self.search_box, 1)
        self.mode = QComboBox()
        self.mode.addItems(["全部", "仅文件名", "仅内容"])
        self.mode.setMinimumHeight(42)
        self.mode.currentIndexChanged.connect(self._do_search)
        bar.addWidget(self.mode)
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("ghost")
        self.theme_btn.setMinimumHeight(42)
        self.theme_btn.clicked.connect(self._show_theme_menu)
        bar.addWidget(self.theme_btn)
        tl.addLayout(bar)
        root.addWidget(top)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left.setObjectName("listPane")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        self.list_head = QWidget()
        self.list_head.setObjectName("listHeadBar")
        hr = QHBoxLayout(self.list_head)
        hr.setContentsMargins(12, 6, 8, 4)
        hr.setSpacing(8)
        self.result_count = QLabel("")
        self.result_count.setObjectName("listHead")
        hr.addWidget(self.result_count, 1)
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("sortCombo")
        self.sort_combo.addItems(["相关度", "最近修改", "文件名"])
        self.sort_combo.setToolTip("结果排序方式")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        hr.addWidget(self.sort_combo, 0)
        self.list_head.hide()
        ll.addWidget(self.list_head)
        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.currentItemChanged.connect(self._on_select)
        self.result_list.itemActivated.connect(self._on_activate)
        self.result_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._context_menu)
        ll.addWidget(self.result_list, 1)
        self._build_empty_hint(ll)
        split.addWidget(left)
        split.addWidget(self._build_preview())
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 6)
        split.setSizes([520, 660])
        wrap = QWidget()
        wrap.setObjectName("contentWrap")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 6, 16, 10)  # 中间内容区四周留白，不贴窗口边
        wl.addWidget(split)
        root.addWidget(wrap, 1)

        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status.setObjectName("statusBar")
        self.index_bar = QProgressBar()
        self.index_bar.setObjectName("indexBar")
        self.index_bar.setTextVisible(False)
        self.index_bar.setFixedWidth(200)
        self.index_bar.hide()
        self.status.addWidget(self.index_bar)
        self.pct_label = QLabel("")
        self.pct_label.setObjectName("pctLabel")
        self.status.addWidget(self.pct_label)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_dot.hide()
        self.status.addWidget(self.status_dot)
        self.status_label = QLabel("准备中…")
        self.status.addWidget(self.status_label)
        kb = QLabel('<span id="kbd"> ↑↓ </span> 选择　<span id="kbd"> ↵ </span> 打开　<span id="kbd"> Esc </span> 收起')
        kb.setTextFormat(Qt.RichText)
        self.hotkey_label = QLabel(f"全局热键 {GLOBAL_HOTKEY}")
        self.status.addPermanentWidget(kb)
        self.status.addPermanentWidget(self.hotkey_label)

        # 趣味统计「我的胶片报告」入口（非侵入注入，逻辑全在 stats_entry）
        from .stats_entry import install_stats_entry
        install_stats_entry(self)

        self._init_toast()
        self._init_spinner()

    def _build_preview(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("previewPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # 顶栏：完整路径（可复制）+ 文件元信息（大小·页数·修改时间）
        head = QWidget()
        head.setObjectName("previewHeadBar")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(2, 0, 2, 4)
        hv.setSpacing(5)
        pr = QHBoxLayout()
        pr.setSpacing(8)
        self.path_label = QLabel("← 选中左侧结果查看预览")
        self.path_label.setObjectName("pathLabel")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pr.addWidget(self.path_label, 1)
        self.copy_path_btn = QPushButton("复制路径")
        self.copy_path_btn.setObjectName("linkBtn")
        self.copy_path_btn.clicked.connect(self._act_copy_path)
        self.copy_path_btn.hide()
        pr.addWidget(self.copy_path_btn, 0)
        hv.addLayout(pr)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("metaLabel")
        hv.addWidget(self.meta_label)
        lay.addWidget(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.image_label = QLabel(
            '<div style="font-size:30px">🔎</div>'
            '<div style="color:#888;font-size:13px;margin-top:12px">选中左侧结果，预览命中页</div>')
        self.image_label.setObjectName("previewImage")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.image_label)
        # 预览区滚轮 = 按原始页序翻页（看前几页判断是不是要找的 PPT）
        self.scroll.viewport().installEventFilter(self)
        self.image_label.installEventFilter(self)
        lay.addWidget(self.scroll, 1)

        # 命中页缩略图条
        self.thumb_row = QHBoxLayout()
        self.thumb_row.setSpacing(7)
        self.thumb_row.setAlignment(Qt.AlignCenter)
        thumb_wrap = QWidget()
        thumb_wrap.setLayout(self.thumb_row)
        lay.addWidget(thumb_wrap)

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("◀ 上一命中页")
        self.prev_btn.setObjectName("navBtn")
        self.prev_btn.clicked.connect(lambda: self._step_hit(-1))
        self.page_label = QLabel("—")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_btn = QPushButton("下一命中页 ▶")
        self.next_btn.setObjectName("navBtn")
        self.next_btn.clicked.connect(lambda: self._step_hit(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next_btn)
        lay.addLayout(nav)

        ops = QHBoxLayout()
        self.goto_btn = QPushButton("↵ 打开并跳到此页")
        self.goto_btn.setObjectName("primary")
        self.goto_btn.clicked.connect(self._act_goto)
        self.open_btn = QPushButton("打开文件")
        self.open_btn.clicked.connect(self._act_open)
        self.folder_btn = QPushButton("打开文件夹")
        self.folder_btn.clicked.connect(self._act_folder)
        self.clip_btn = QPushButton("复制到剪贴板")
        self.clip_btn.setToolTip("复制文件到剪贴板，可直接粘贴到邮件 / 聊天 / 资源管理器")
        self.clip_btn.clicked.connect(self._act_copy_clipboard)
        for b in (self.goto_btn, self.open_btn, self.folder_btn, self.clip_btn):
            b.setMinimumHeight(38)
            ops.addWidget(b)
        lay.addLayout(ops)
        self._set_ops_enabled(False)
        return panel

    def _set_ops_enabled(self, on: bool) -> None:
        for w in (self.goto_btn, self.open_btn, self.folder_btn, self.clip_btn,
                  self.prev_btn, self.next_btn):
            w.setEnabled(on)

    def _update_preview_header(self, r: FileResult | None) -> None:
        """预览顶栏：完整路径（可复制）+ 大小·页数·修改时间。"""
        if r is None:
            self.path_label.setText("← 选中左侧结果查看预览")
            self.path_label.setToolTip("")
            self.meta_label.setText("")
            self.copy_path_btn.hide()
            return
        self.path_label.setText(_elide_middle(r.path))
        self.path_label.setToolTip(r.path)
        self.copy_path_btn.show()
        parts = []
        sz = _fmt_size(r.size)
        if sz:
            parts.append(sz)
        if r.page_count:
            parts.append(f"共 {r.page_count} 页")
        tm = _fmt_mtime(r.mtime)
        if tm:
            parts.append(f"修改于 {tm}")
        self.meta_label.setText("　·　".join(parts))

    # ---------- 主题 ----------
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._apply_titlebar_theme()  # 窗口显示后系统标题栏才接受深色属性
        if not getattr(self, "_did_fade", False):
            self._did_fade = True
            self.setWindowOpacity(0.0)
            self._fade = QPropertyAnimation(self, b"windowOpacity", self)
            self._fade.setDuration(200)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()

    def _apply_titlebar_theme(self) -> None:
        """Windows 系统标题栏深浅跟随风格（深色风格→深色标题栏，消除白条割裂）。"""
        try:
            import ctypes
            dark = self._theme in ("raycast", "cinema", "aurora")
            val = ctypes.c_int(1 if dark else 0)
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20（Win10 20H1+）
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()), 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:  # noqa: BLE001 非 Windows / 旧系统静默跳过
            pass

    def _apply_theme(self, name: str, persist: bool = True) -> None:
        self._theme = name
        self._tok = theme.tok(name)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_qss(name))
        self.theme_btn.setText(f"🎨 {dict(theme.THEMES).get(name, name)}")
        if persist:
            _save_theme(name)
        if self._results_raw:
            self._apply_sort_render()
            self._select_first()
        self._apply_titlebar_theme()

    def _show_theme_menu(self) -> None:
        """顶栏风格按钮 → 弹出风格菜单（当前风格打勾）。"""
        menu = QMenu(self)
        for name, label in theme.THEMES:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == self._theme)
            act.triggered.connect(lambda _=False, n=name: self._apply_theme(n))
        menu.exec(self.theme_btn.mapToGlobal(self.theme_btn.rect().bottomLeft()))

    def _toggle_theme(self) -> None:
        """循环切到下一个风格（保留的快捷切换入口）。"""
        names = [n for n, _ in theme.THEMES]
        i = names.index(self._theme) if self._theme in names else 0
        self._apply_theme(names[(i + 1) % len(names)])

    # ---------- 搜索 ----------
    def _refresh_history_model(self) -> None:
        self._history_model.setStringList(history.load_history(limit=10))

    def _do_search(self) -> None:
        query = self.search_box.text().strip()
        if not query:
            self._show_recent()
            return
        self._showing_recent = False
        results = search_mod.search(self._conn, query)
        m = self.mode.currentText()
        if m == "仅文件名":
            results = [r for r in results if r.name_hit]
        elif m == "仅内容":
            results = [r for r in results if r.hits]
        self._results_raw = results
        self._apply_sort_render()
        if results:
            self.result_count.setText(f"命中 {len(results)} 个文件")
            self.list_head.show()
            self._select_first()
        else:
            self.list_head.hide()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._show_empty_hint(query)

    def _show_recent(self) -> None:
        """空查询默认视图：列最近修改的 PPTX，打开即点（零输入也有内容）。"""
        recents = db.recent_files(self._conn, limit=20)
        self._results_raw = recents
        self._cur = None
        self._showing_recent = bool(recents)
        if recents:
            self._apply_sort_render()
            self.result_count.setText(f"最近修改 · {len(recents)} 个文件")
            self.list_head.show()
            self._select_first()
        else:
            self.result_list.clear()
            self.list_head.hide()
            self._update_preview_header(None)
            self._set_ops_enabled(False)

    def _build_empty_hint(self, parent_layout) -> None:
        """零结果引导面板（默认隐藏，零结果时覆盖结果列表位置）。"""
        self.empty_hint = QWidget()
        self.empty_hint.setObjectName("emptyHint")
        v = QVBoxLayout(self.empty_hint)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(11)
        icon = QLabel("🔍")
        icon.setObjectName("emptyIcon")
        icon.setAlignment(Qt.AlignCenter)
        v.addWidget(icon)
        self._empty_query_label = QLabel("没找到")
        self._empty_query_label.setObjectName("emptyTitle")
        self._empty_query_label.setAlignment(Qt.AlignCenter)
        self._empty_query_label.setWordWrap(True)
        v.addWidget(self._empty_query_label)
        tip = QLabel("换个说法试试")
        tip.setObjectName("emptyTip")
        tip.setAlignment(Qt.AlignCenter)
        v.addWidget(tip)
        self._sugg_btns: dict[str, QPushButton] = {}
        for key, text in (("unquote", "去掉引号再搜"), ("fewer", "只用第一个词"), ("filename", "改搜文件名")):
            b = QPushButton(text)
            b.setObjectName("suggBtn")
            b.clicked.connect(lambda _=False, k=key: self._apply_suggestion(k))
            v.addWidget(b, 0, Qt.AlignCenter)
            self._sugg_btns[key] = b
        self.empty_hint.hide()
        parent_layout.addWidget(self.empty_hint, 1)

    def _show_empty_hint(self, query: str) -> None:
        """零结果引导：列表让位，给「没找到 + 可点建议」。"""
        self.result_list.hide()
        self._empty_query_label.setText(f"没找到「{query}」")
        sugg = _empty_suggestions(query, self.mode.currentText())
        for key, btn in self._sugg_btns.items():
            btn.setVisible(key in sugg)
        self.empty_hint.show()

    def _hide_empty_hint(self) -> None:
        if getattr(self, "empty_hint", None) is not None:
            self.empty_hint.hide()
            self.result_list.show()

    def _apply_suggestion(self, key: str) -> None:
        q = self.search_box.text()
        if key == "unquote":
            for ch in ('"', '“', '”'):
                q = q.replace(ch, "")
            self.search_box.setText(q)
        elif key == "fewer":
            parts = q.split()
            if parts:
                self.search_box.setText(parts[0])
        elif key == "filename":
            self.mode.setCurrentText("仅文件名")
        self._do_search()

    def _sort_key(self) -> str:
        return {"相关度": "relevance", "最近修改": "recent", "文件名": "name"}.get(
            self.sort_combo.currentText(), "relevance")

    def _apply_sort_render(self) -> None:
        self._results = _sort_results(self._results_raw, self._sort_key())
        self._render_results(self._results)

    def _on_sort_changed(self) -> None:
        if self._results_raw:
            self._apply_sort_render()
            if self._results:
                self._select_first()

    def _render_results(self, results: list[FileResult]) -> None:
        self._hide_empty_hint()
        self.result_list.clear()
        hlcss = theme.highlight_css(self._theme)
        if self._should_group_by_time():
            now_ts = datetime.datetime.now().timestamp()
            idx = 0
            for label, items in group_by_time(results, now_ts):
                self._add_section_header(f"{label} · {len(items)}")
                for r in items:
                    self._add_result_item(idx, r, hlcss)
                    idx += 1
        else:
            for i, r in enumerate(results):
                self._add_result_item(i, r, hlcss)

    def _should_group_by_time(self) -> bool:
        """时间分组仅在「时间序」下生效：最近修改排序，或空查询默认视图。"""
        key = self._sort_key()
        if key == "recent":
            return True
        return self._showing_recent and key == "relevance"

    def _add_result_item(self, idx: int, r: FileResult, hlcss: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, idx)
        item.setToolTip(r.path)
        w = ResultItem(r, self._tok, hlcss)
        item.setSizeHint(w.sizeHint())
        self.result_list.addItem(item)
        self.result_list.setItemWidget(item, w)

    def _add_section_header(self, label: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, None)
        item.setFlags(Qt.NoItemFlags)  # 分组头：不可选不可交互
        lbl = QLabel(label)
        lbl.setObjectName("sectionHead")
        item.setSizeHint(lbl.sizeHint())
        self.result_list.addItem(item)
        self.result_list.setItemWidget(item, lbl)

    def _first_selectable_row(self) -> int:
        for i in range(self.result_list.count()):
            if self.result_list.item(i).data(Qt.UserRole) is not None:
                return i
        return -1

    def _select_first(self) -> None:
        row = self._first_selectable_row()
        if row >= 0:
            self.result_list.setCurrentRow(row)

    # ---------- 选择 / 预览 ----------
    def _on_select(self, cur: QListWidgetItem | None, prev: QListWidgetItem | None = None) -> None:
        if prev is not None:
            pw = self.result_list.itemWidget(prev)
            if isinstance(pw, ResultItem):
                pw.set_selected(False)
        if cur is None:
            self._cur = None
            self._cur_item_widget = None
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            return
        idx = cur.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        self._cur = self._results[idx]
        self._hit_idx = 0
        self._view_page = self._current_page()  # 初始定位首个命中页（无命中=第1页）
        w = self.result_list.itemWidget(cur)
        if isinstance(w, ResultItem):
            w.set_selected(True, self.isActiveWindow())
        self._cur_item_widget = w
        self._update_preview_header(self._cur)
        self._set_ops_enabled(True)
        self._populate_thumbs()
        self._request_preview()

    def _current_page(self) -> int:
        if self._cur and self._cur.hits:
            return self._cur.hits[self._hit_idx].page_no
        return 1

    def _populate_thumbs(self) -> None:
        while self.thumb_row.count():
            it = self.thumb_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._thumb_btns = []
        if not self._cur or not self._cur.hits:
            return
        for i, h in enumerate(self._cur.hits[:12]):
            b = QToolButton()
            b.setObjectName("thumb")
            b.setText(f"第{h.page_no}页")
            b.setCheckable(True)
            b.setChecked(i == self._hit_idx)
            b.setFixedSize(64, 34)
            b.clicked.connect(lambda _=False, i=i: self._goto_hit(i))
            self.thumb_row.addWidget(b)
            self._thumb_btns.append(b)

    def _goto_hit(self, i: int) -> None:
        self._hit_idx = i
        if self._cur and self._cur.hits:
            self._view_page = self._cur.hits[i].page_no
        self._request_preview()

    def _step_hit(self, delta: int) -> None:
        if not self._cur or not self._cur.hits:
            return
        self._hit_idx = max(0, min(len(self._cur.hits) - 1, self._hit_idx + delta))
        self._view_page = self._cur.hits[self._hit_idx].page_no
        self._request_preview()

    def _init_spinner(self) -> None:
        self._spin_idx = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._tick_spinner)

    def _tick_spinner(self) -> None:
        ch = "◐◓◑◒"[self._spin_idx % 4]
        self._spin_idx += 1
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="font-size:30px;color:#9a968c">{ch}</div>'
            '<div style="color:#888;font-size:13px;margin-top:12px">正在渲染预览…</div>')

    def _start_spinner(self) -> None:
        self._spin_idx = 0
        self._tick_spinner()
        self._spin_timer.start(90)

    def _stop_spinner(self) -> None:
        self._spin_timer.stop()

    def _request_preview(self) -> None:
        if not self._cur:
            return
        self._zoom = 1.0
        page = self._view_page
        hits = self._cur.hits or []
        total = self._cur.page_count or 0
        n = len(hits)
        hit_pages = {h.page_no for h in hits}
        # 页码：第 X / 共 N 页（滚轮可在原始页序间自由翻；命中页加标记）
        if total:
            tag = "　·　命中页" if page in hit_pages else ""
            self.page_label.setText(f"第 {page} / {total} 页{tag}")
        else:
            self.page_label.setText(f"第 {page} 页")
        # 上/下「命中页」按钮：在命中页之间跳
        self.prev_btn.setEnabled(n > 0 and self._hit_idx > 0)
        self.next_btn.setEnabled(n > 0 and self._hit_idx < n - 1)
        # 缩略图高亮：当前页正好是某命中页就点亮它
        for i, b in enumerate(self._thumb_btns):
            b.setChecked(i < n and hits[i].page_no == page)
        self._start_spinner()
        self._req_id += 1
        self._render.request(self._req_id, self._cur.path, page, cache_key=None)

    def _wheel_page(self, delta_y: int) -> None:
        """预览区滚轮：按原始页序上下翻页（向上滚=上一页，向下滚=下一页）。"""
        if not self._cur:
            return
        total = self._cur.page_count or 0
        if total <= 0:
            return  # .ppt / 未解析，页数未知，不翻页
        new = self._view_page + (-1 if delta_y > 0 else 1)
        new = max(1, min(total, new))
        if new == self._view_page:
            return
        self._view_page = new
        for i, h in enumerate(self._cur.hits or []):
            if h.page_no == new:
                self._hit_idx = i  # 翻到命中页时同步，让上/下命中页按钮接续
                break
        self._request_preview()

    def _zoom_by(self, factor: float) -> None:
        if self._cur_pixmap is None:
            return
        self._zoom = max(1.0, min(5.0, self._zoom * factor))
        self._update_pixmap()

    def _toggle_zoom(self) -> None:
        if self._cur_pixmap is None:
            return
        self._zoom = 1.0 if self._zoom > 1.0 else 2.0
        self._update_pixmap()
        self._toast("原尺寸放大 · 再双击还原" if self._zoom > 1.0 else "已适配窗口")

    def _on_rendered(self, req_id: int, png: str) -> None:
        if req_id != self._req_id:
            return
        self._stop_spinner()
        if not png or not os.path.exists(png):
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(
                '<div style="font-size:30px">📄</div>'
                '<div style="color:#888;font-size:13px;margin-top:12px">此页暂时无法预览<br>'
                '点「打开文件」直接查看</div>')
            self._cur_pixmap = None
            return
        pm = QPixmap(png)
        self._cur_pixmap = pm if not pm.isNull() else None
        self._update_pixmap()

    def _update_pixmap(self) -> None:
        if self._cur_pixmap is None:
            return
        vp = self.scroll.viewport().size()
        fitted = self._cur_pixmap.scaled(
            vp.width() - 6, vp.height() - 6, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setText("")
        if self._zoom <= 1.0:
            self.scroll.setWidgetResizable(True)
            self.image_label.setPixmap(fitted)
        else:
            self.scroll.setWidgetResizable(False)
            scaled = self._cur_pixmap.scaled(
                int(fitted.width() * self._zoom), int(fitted.height() * self._zoom),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
            self.image_label.resize(scaled.size())

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._update_pixmap()
        if getattr(self, "_toast_label", None) is not None and self._toast_label.isVisible():
            self._reposition_toast()

    def changeEvent(self, e):  # noqa: N802
        if e.type() == QEvent.ActivationChange and self._cur_item_widget is not None:
            self._cur_item_widget.set_selected(True, self.isActiveWindow())
        super().changeEvent(e)

    # ---------- 键盘 ----------
    def eventFilter(self, obj, ev):  # noqa: N802
        et = ev.type()
        if et in (QEvent.Wheel, QEvent.MouseButtonDblClick) and obj in (self.image_label, self.scroll.viewport()):
            if et == QEvent.Wheel:
                if ev.modifiers() & Qt.ControlModifier:
                    self._zoom_by(1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)
                else:
                    self._wheel_page(ev.angleDelta().y())
            else:
                self._toggle_zoom()
            return True
        if obj is self.search_box and ev.type() == QEvent.FocusIn and not self.search_box.text():
            self._refresh_history_model()
            if self._history_model.stringList():
                self._completer.complete()
        if obj is self.search_box and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                n = self.result_list.count()
                if n:
                    cur = max(0, self.result_list.currentRow())
                    nr = min(n - 1, cur + 1) if k == Qt.Key_Down else max(0, cur - 1)
                    self.result_list.setCurrentRow(nr)
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self._cur:
                    self._act_goto()
                return True
            if k == Qt.Key_Escape:
                if self.search_box.text():
                    self.search_box.clear()
                elif self._to_tray_on_close:
                    self.hide()
                return True
        return super().eventFilter(obj, ev)

    # ---------- 打开动作 ----------
    def _act_open(self) -> None:
        if self._cur and not actions.open_file(self._cur.path):
            self._toast("文件已移动或删除")

    def _act_folder(self) -> None:
        if self._cur and not actions.open_folder(self._cur.path):
            self._toast("找不到所在文件夹")

    def _act_copy_path(self) -> None:
        if self._cur:
            QApplication.clipboard().setText(self._cur.path)
            self._toast("已复制完整路径")

    def _act_copy_clipboard(self) -> None:
        """复制文件本体到剪贴板（Windows CF_HDROP），可粘贴到邮件 / 聊天 / 资源管理器。"""
        if not self._cur:
            return
        if not os.path.exists(self._cur.path):
            self._toast("文件已移动或删除")
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self._cur.path)])
        QApplication.clipboard().setMimeData(mime)
        self._toast("已复制文件到剪贴板，可粘贴到邮件 / 聊天")

    def _act_goto(self) -> None:
        if not self._cur:
            return
        q = self.search_box.text().strip()
        if q:
            history.add_history(q)
            self._refresh_history_model()
        page = self._view_page
        opened, jumped = actions.open_at_page(self._cur.path, page)
        if not opened:
            self._toast("文件已移动或删除")
        elif not jumped:
            self._toast(f"已打开，但未能自动跳到第 {page} 页")

    def _on_activate(self, _item) -> None:
        self._act_goto()

    def _context_menu(self, pos) -> None:
        item = self.result_list.itemAt(pos)
        if item is None:
            return
        idx = item.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        r = self._results[idx]
        menu = QMenu(self)
        menu.addAction("打开文件", lambda: actions.open_file(r.path))
        menu.addAction("打开并跳到命中页", lambda: actions.open_at_page(r.path, r.hits[0].page_no if r.hits else 1))
        menu.addAction("打开所在文件夹", lambda: actions.open_folder(r.path))
        menu.addSeparator()
        menu.addAction("复制完整路径", lambda: QApplication.clipboard().setText(r.path))
        menu.addAction("复制文件名", lambda: QApplication.clipboard().setText(r.name))
        menu.exec(self.result_list.mapToGlobal(pos))

    def _init_toast(self) -> None:
        """中下方浮层提示：一次性操作反馈不再污染状态栏。"""
        self._toast_label = QLabel(self)
        self._toast_label.setObjectName("toast")
        self._toast_label.setAlignment(Qt.AlignCenter)
        self._toast_label.hide()
        self._toast_fx = QGraphicsOpacityEffect(self._toast_label)
        self._toast_label.setGraphicsEffect(self._toast_fx)
        self._toast_fade = QPropertyAnimation(self._toast_fx, b"opacity", self)
        self._toast_fade.setDuration(160)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

    def _reposition_toast(self) -> None:
        lbl = self._toast_label
        x = (self.width() - lbl.width()) // 2
        y = self.height() - lbl.height() - 64  # 悬于状态栏上方
        lbl.move(max(8, x), max(8, y))

    def _toast(self, msg: str) -> None:
        lbl = self._toast_label
        lbl.setText(msg)
        lbl.adjustSize()
        self._reposition_toast()
        lbl.show()
        lbl.raise_()
        self._toast_fade.stop()
        self._toast_fade.setStartValue(self._toast_fx.opacity())
        self._toast_fade.setEndValue(1.0)
        self._toast_fade.start()
        self._toast_timer.start(1800)

    def _hide_toast(self) -> None:
        self._toast_fade.stop()
        self._toast_fade.setStartValue(self._toast_fx.opacity())
        self._toast_fade.setEndValue(0.0)
        self._toast_fade.start()
        QTimer.singleShot(200, self._toast_label.hide)

    # ---------- 索引 ----------
    def _start_indexing(self, roots: list[str] | None, workers: int | None) -> None:
        from ..scanner import fixed_drives
        if not roots:
            env = os.environ.get("PPTX_FINDER_ROOTS", "").strip()
            if env:
                roots = [r for r in env.split(os.pathsep) if r]
        roots = roots or fixed_drives()
        self._indexer = IndexWorker(self._db_path, roots, workers=workers)
        self._indexer.progress.connect(self._on_index_progress)
        self._indexer.finished_index.connect(self._on_index_done)
        self.index_bar.setRange(0, 0)
        self.index_bar.show()
        self.status_dot.hide()
        self.status_label.setText(f"开始索引：{', '.join(roots)}")
        self._indexer.start()

    def _on_index_progress(self, done: int, total: int, cur: str) -> None:
        self.status_dot.hide()
        self.index_bar.show()
        if total < 0:
            self.index_bar.setRange(0, 0)  # busy：进度条来回流动（扫描，总数未知）
            self.pct_label.setText("")
            self.status_label.setText(f"扫描磁盘中…　{cur}（可边扫边搜）")
        else:
            self.index_bar.setRange(0, max(1, total))
            self.index_bar.setValue(done)
            self.pct_label.setText(f"{int(done / max(1, total) * 100)}%")
            self.status_label.setText(f"正在索引内容　{done}/{total}　·　{os.path.basename(cur)}")

    def _on_index_done(self, summary: dict) -> None:
        self.index_bar.hide()
        self.pct_label.setText("")
        self._refresh_status(summary)

    def _refresh_status(self, summary: dict | None = None) -> None:
        try:
            s = db.stats(self._conn)
            extra = ""
            if summary and "deleted" in summary:
                extra = f"（更新 {summary.get('indexed', 0)}，移除 {summary.get('deleted', 0)}）"
            self.status_dot.show()
            self.status_label.setText(f"索引就绪：{s['file_count']} 个文件 · {s['page_count']} 页{extra}")
        except Exception as e:  # noqa: BLE001
            self.status_dot.hide()
            self.status_label.setText(f"数据库读取异常：{e}")

    # ---------- 生命周期 ----------
    def closeEvent(self, e):  # noqa: N802
        if self._to_tray_on_close:
            e.ignore()
            self.hide()
            return
        self._shutdown()
        e.accept()

    def _shutdown(self) -> None:
        if self._indexer is not None:
            self._indexer.stop()
            self._indexer.wait(5000)
        if self._owns_render:
            self._render.stop()
            if not self._render.wait(8000):
                self._render.terminate()
                self._render.wait(2000)
