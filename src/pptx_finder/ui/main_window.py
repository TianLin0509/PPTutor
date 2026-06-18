"""主窗口：双主题 + 精致结果项（焦点双态/半透明高亮/字重层级）+ P0 交互
（即时搜索 / 全键盘导航 / 命中页缩略图条 / 索引进度条）。

可测试性：conn 与 render_worker 可注入；do_index=False 时不启动磁盘索引。
"""
from __future__ import annotations

import html
import json
import os

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QPixmap
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


class ResultItem(QWidget):
    """单条结果：文件名(SemiBold) + 命中页胶囊 + 版本/最新徽章 + 高亮片段 + 路径(mono灰)。"""

    def __init__(self, r: FileResult, tok: dict, hlcss: str):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._tok = tok
        self._sel = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(13, 9, 12, 10)
        lay.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(8)
        fn = QLabel(html.escape(r.name))
        fn.setStyleSheet(f"font-size:14px;font-weight:600;color:{tok['ink1']};background:transparent;")
        fn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(fn, 1)

        if r.hits:
            pages = "、".join(str(h.page_no) for h in r.hits[:6])
            more = "…" if len(r.hits) > 6 else ""
            pg = QLabel(f"命中 {len(r.hits)} 页 · 第 {pages}{more} 页")
            pg.setStyleSheet(
                f"font-size:11px;font-weight:600;color:{tok['accd']};"
                f"background:rgba({tok['hl_r']},{tok['hl_g']},{tok['hl_b']},0.14);"
                "border-radius:6px;padding:2px 8px;")
            row.addWidget(pg, 0)
        elif r.name_hit:
            pg = QLabel("文件名命中")
            pg.setStyleSheet(f"font-size:11px;font-weight:600;color:{tok['grn']};background:transparent;")
            row.addWidget(pg, 0)

        if r.status == "filename_only":
            ext = QLabel(".ppt")
            ext.setStyleSheet(f"font-size:10.5px;color:{tok['ink4']};border:1px solid {tok['bd2']};border-radius:5px;padding:1px 6px;background:transparent;")
            row.addWidget(ext, 0)
        if r.group_id is not None:
            vg = QLabel("★ 最新版" if r.is_latest else "版本")
            col = tok["grn"] if r.is_latest else tok["ink3"]
            vg.setStyleSheet(f"font-size:10.5px;font-weight:600;color:{col};border:1px solid {tok['bd2']};border-radius:5px;padding:1px 7px;background:transparent;")
            row.addWidget(vg, 0)
        lay.addLayout(row)

        if r.hits and r.hits[0].snippet:
            sn = QLabel(_highlight(r.hits[0].snippet, hlcss))
            sn.setTextFormat(Qt.RichText)
            sn.setStyleSheet(f"font-size:12.5px;color:{tok['ink2']};background:transparent;")
            sn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(sn)

        pa = QLabel(html.escape(r.path))
        pa.setStyleSheet(f'font-size:11px;color:{tok["ink4"]};font-family:"Cascadia Code","Consolas",monospace;background:transparent;')
        pa.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(pa)
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
        self.setStyleSheet(f"ResultItem{{background:{bg};border-radius:9px;border-left:3px solid {bar};}}")


class MainWindow(QMainWindow):
    def __init__(self, conn=None, render_worker=None, do_index=True,
                 roots: list[str] | None = None, workers: int | None = None):
        super().__init__()
        self.setWindowTitle("pptx-finder · PPTX 查询助手   v0.2.0")
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
        self.search_box.textChanged.connect(lambda: self._debounce.start())
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
        self.theme_btn.clicked.connect(self._toggle_theme)
        bar.addWidget(self.theme_btn)
        tl.addLayout(bar)
        root.addWidget(top)

        split = QSplitter(Qt.Horizontal)
        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.currentItemChanged.connect(self._on_select)
        self.result_list.itemActivated.connect(self._on_activate)
        self.result_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._context_menu)
        split.addWidget(self.result_list)
        split.addWidget(self._build_preview())
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 6)
        split.setSizes([520, 660])
        root.addWidget(split, 1)

        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status.setObjectName("statusBar")
        self.index_bar = QProgressBar()
        self.index_bar.setObjectName("indexBar")
        self.index_bar.setTextVisible(False)
        self.index_bar.setFixedWidth(120)
        self.index_bar.hide()
        self.status.addWidget(self.index_bar)
        self.status_label = QLabel("准备中…")
        self.status.addWidget(self.status_label)
        kb = QLabel('<span id="kbd"> ↑↓ </span> 选择　<span id="kbd"> ↵ </span> 打开　<span id="kbd"> Esc </span> 收起')
        kb.setTextFormat(Qt.RichText)
        self.hotkey_label = QLabel(f"全局热键 {GLOBAL_HOTKEY}")
        self.status.addPermanentWidget(kb)
        self.status.addPermanentWidget(self.hotkey_label)

    def _build_preview(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("previewPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        self.preview_title = QLabel("预览")
        self.preview_title.setObjectName("previewHead")
        lay.addWidget(self.preview_title)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.image_label = QLabel("← 选中左侧结果查看预览")
        self.image_label.setObjectName("previewImage")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.image_label)
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
        for b in (self.goto_btn, self.open_btn, self.folder_btn):
            b.setMinimumHeight(38)
            ops.addWidget(b)
        lay.addLayout(ops)
        self._set_ops_enabled(False)
        return panel

    def _set_ops_enabled(self, on: bool) -> None:
        for w in (self.goto_btn, self.open_btn, self.folder_btn, self.prev_btn, self.next_btn):
            w.setEnabled(on)

    # ---------- 主题 ----------
    def _apply_theme(self, name: str, persist: bool = True) -> None:
        self._theme = name
        self._tok = theme.tok(name)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_qss(name))
        self.theme_btn.setText("🌙 深色" if name == "cloud" else "☀ 云白")
        if persist:
            _save_theme(name)
        if self._results:
            self._render_results(self._results)
            self.result_list.setCurrentRow(0)

    def _toggle_theme(self) -> None:
        self._apply_theme("raycast" if self._theme == "cloud" else "cloud")

    # ---------- 搜索 ----------
    def _do_search(self) -> None:
        query = self.search_box.text().strip()
        if not query:
            self.result_list.clear()
            self._results = []
            self._cur = None
            self.preview_title.setText("预览")
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
        self.preview_title.setText(f"预览　·　命中 {len(results)} 个文件")
        if results:
            self.result_list.setCurrentRow(0)

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
            self._set_ops_enabled(False)
            return
        idx = cur.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        self._cur = self._results[idx]
        self._hit_idx = 0
        w = self.result_list.itemWidget(cur)
        if isinstance(w, ResultItem):
            w.set_selected(True, self.isActiveWindow())
        self._cur_item_widget = w
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
        self._request_preview()

    def _step_hit(self, delta: int) -> None:
        if not self._cur or not self._cur.hits:
            return
        self._hit_idx = max(0, min(len(self._cur.hits) - 1, self._hit_idx + delta))
        self._request_preview()

    def _request_preview(self) -> None:
        if not self._cur:
            return
        page = self._current_page()
        n = len(self._cur.hits) if self._cur.hits else 0
        if n:
            self.page_label.setText(f"命中 {self._hit_idx + 1}/{n}　第 {page} 页")
            self.prev_btn.setEnabled(self._hit_idx > 0)
            self.next_btn.setEnabled(self._hit_idx < n - 1)
        else:
            self.page_label.setText(f"第 {page} 页")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
        for i, b in enumerate(self._thumb_btns):
            b.setChecked(i == self._hit_idx)
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("渲染中…")
        self._req_id += 1
        self._render.request(self._req_id, self._cur.path, page, cache_key=None)

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

    def _act_goto(self) -> None:
        if not self._cur:
            return
        page = self._current_page()
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
        self.status_label.setText(f"开始索引：{', '.join(roots)}")
        self._indexer.start()

    def _on_index_progress(self, done: int, total: int, cur: str) -> None:
        if total < 0:
            self.index_bar.setRange(0, 0)  # busy
            self.status_label.setText(f"扫描磁盘中… {cur}（可边扫边搜）")
        else:
            self.index_bar.setRange(0, max(1, total))
            self.index_bar.setValue(done)
            self.status_label.setText(f"索引中… {done}/{total}　{os.path.basename(cur)}")

    def _on_index_done(self, summary: dict) -> None:
        self.index_bar.hide()
        self._refresh_status(summary)

    def _refresh_status(self, summary: dict | None = None) -> None:
        try:
            s = db.stats(self._conn)
            extra = ""
            if summary and "deleted" in summary:
                extra = f"（更新 {summary.get('indexed', 0)}，移除 {summary.get('deleted', 0)}）"
            self.status_label.setText(f"索引就绪：{s['file_count']} 个文件 · {s['page_count']} 页{extra}")
        except Exception as e:  # noqa: BLE001
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
