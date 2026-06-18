"""主窗口：双主题 + 精致结果项（焦点双态/半透明高亮/字重层级）+ P0 交互
（即时搜索 / 全键盘导航 / 命中页缩略图条 / 索引进度条）。

可测试性：conn 与 render_worker 可注入；do_index=False 时不启动磁盘索引。
"""
from __future__ import annotations

import datetime
import html
import json
import os

from PySide6.QtCore import QEvent, QMimeData, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from .. import actions, db, search as search_mod
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
            bg, bar = t["hover"], "transparent"
        else:
            bg, bar = "transparent", "transparent"
        self.setStyleSheet(f"ResultItem{{background:{bg};border-radius:{t['radius']}px;border-left:3px solid {bar};}}")


class MainWindow(QMainWindow):
    def __init__(self, conn=None, render_worker=None, do_index=True,
                 roots: list[str] | None = None, workers: int | None = None):
        super().__init__()
        self.setWindowTitle("pptx-finder · PPTX 查询助手   v0.4.0")
        self.resize(1180, 760)

        self._theme = _load_theme()
        self._tok = theme.tok(self._theme)
        self._db_path = str(cfg_db_path())
        self._conn = conn or db.connect(self._db_path)
        db.init_db(self._conn)

        self._results: list[FileResult] = []
        self._cur: FileResult | None = None
        self._cur_item_widget: ResultItem | None = None
        self._hit_idx = 0
        self._view_page = 1  # 当前预览页（原始页序，滚轮可脱离命中页自由翻）
        self._req_id = 0
        self._cur_pixmap: QPixmap | None = None
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
        self.result_count = QLabel("")
        self.result_count.setObjectName("listHead")
        self.result_count.hide()
        ll.addWidget(self.result_count)
        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.currentItemChanged.connect(self._on_select)
        self.result_list.itemActivated.connect(self._on_activate)
        self.result_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._context_menu)
        ll.addWidget(self.result_list, 1)
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
        self.image_label = QLabel("← 选中左侧结果查看预览")
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
        if self._results:
            self._render_results(self._results)
            self.result_list.setCurrentRow(0)
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
    def _do_search(self) -> None:
        query = self.search_box.text().strip()
        if not query:
            self.result_list.clear()
            self._results = []
            self._cur = None
            self.result_count.hide()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            return
        results = search_mod.search(self._conn, query)
        m = self.mode.currentText()
        if m == "仅文件名":
            results = [r for r in results if r.name_hit]
        elif m == "仅内容":
            results = [r for r in results if r.hits]
        self._results = results
        self._render_results(results)
        self.result_count.setText(f"命中 {len(results)} 个文件")
        self.result_count.show()
        if results:
            self.result_list.setCurrentRow(0)
        else:
            self._update_preview_header(None)

    def _render_results(self, results: list[FileResult]) -> None:
        self.result_list.clear()
        hlcss = theme.highlight_css(self._theme)
        for i, r in enumerate(results):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, i)
            item.setToolTip(r.path)
            w = ResultItem(r, self._tok, hlcss)
            item.setSizeHint(w.sizeHint())
            self.result_list.addItem(item)
            self.result_list.setItemWidget(item, w)

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

    def _request_preview(self) -> None:
        if not self._cur:
            return
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
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("渲染中…")
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

    def _on_rendered(self, req_id: int, png: str) -> None:
        if req_id != self._req_id:
            return
        if not png or not os.path.exists(png):
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("无法预览此页，可直接点「打开文件」查看")
            self._cur_pixmap = None
            return
        pm = QPixmap(png)
        self._cur_pixmap = pm if not pm.isNull() else None
        self._update_pixmap()

    def _update_pixmap(self) -> None:
        if self._cur_pixmap is None:
            return
        vp = self.scroll.viewport().size()
        self.image_label.setText("")
        self.image_label.setPixmap(self._cur_pixmap.scaled(
            vp.width() - 6, vp.height() - 6, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._update_pixmap()

    def changeEvent(self, e):  # noqa: N802
        if e.type() == QEvent.ActivationChange and self._cur_item_widget is not None:
            self._cur_item_widget.set_selected(True, self.isActiveWindow())
        super().changeEvent(e)

    # ---------- 键盘 ----------
    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Wheel and obj in (self.image_label, self.scroll.viewport()):
            self._wheel_page(ev.angleDelta().y())
            return True
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

    def _toast(self, msg: str) -> None:
        self.status_label.setText(msg)

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
