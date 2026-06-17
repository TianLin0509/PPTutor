"""主窗口：搜索栏 + 结果列表（命中徽章/片段） + 预览面板（命中页导航 + 打开动作）。

可测试性：conn 与 render_worker 可注入；do_index=False 时不启动磁盘索引（E2E 用）。
"""
from __future__ import annotations

import html
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QPushButton, QScrollArea, QSplitter,
    QVBoxLayout, QWidget,
)

from .. import actions, db, search as search_mod
from ..config import GLOBAL_HOTKEY, db_path as cfg_db_path
from ..models import FileResult
from .index_worker import IndexWorker
from .render_worker import RenderWorker

ACCENT = "#0071e3"
GRAY = "#6e6e73"


def _highlight(snippet: str) -> str:
    """把片段里的【命中】转成高亮 HTML。"""
    s = html.escape(snippet)
    s = s.replace("【", f'<b style="color:{ACCENT}">').replace("】", "</b>")
    return s


class ResultItem(QWidget):
    def __init__(self, r: FileResult):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(f"<b>{html.escape(r.name)}</b>")
        name.setTextFormat(Qt.RichText)
        top.addWidget(name)

        if r.hits:
            pages = "、".join(str(h.page_no) for h in r.hits[:8])
            more = "…" if len(r.hits) > 8 else ""
            badge_txt = f"命中 {len(r.hits)} 页：第 {pages}{more} 页"
            color = ACCENT
        elif r.name_hit:
            badge_txt = "文件名命中"
            color = "#34c759"
        else:
            badge_txt = ""
            color = GRAY
        if r.status == "filename_only":
            badge_txt += "  · .ppt（仅文件名）"
        badge = QLabel(badge_txt)
        badge.setStyleSheet(f"color:{color}; font-size:12px;")
        top.addWidget(badge)
        if r.group_id is not None:
            vtxt = "📚 版本组" + ("　★ 最新版" if r.is_latest else "")
            vlbl = QLabel(vtxt)
            vlbl.setStyleSheet("color:#ff9f0a; font-size:12px;")
            top.addWidget(vlbl)
        top.addStretch(1)
        lay.addLayout(top)

        snippet = r.hits[0].snippet if r.hits else ""
        if snippet:
            sn = QLabel(_highlight(snippet))
            sn.setTextFormat(Qt.RichText)
            sn.setStyleSheet(f"color:{GRAY}; font-size:12px;")
            sn.setWordWrap(False)
            lay.addWidget(sn)

        path_lbl = QLabel(html.escape(r.path))
        path_lbl.setStyleSheet(f"color:{GRAY}; font-size:11px;")
        lay.addWidget(path_lbl)


class MainWindow(QMainWindow):
    def __init__(self, conn=None, render_worker=None, do_index=True,
                 roots: list[str] | None = None, workers: int | None = None):
        super().__init__()
        self.setWindowTitle("pptx-finder · PPTX 查询助手   v0.1.0")
        self.resize(1180, 740)

        self._db_path = str(cfg_db_path())
        self._conn = conn or db.connect(self._db_path)
        db.init_db(self._conn)

        self._results: list[FileResult] = []
        self._cur: FileResult | None = None
        self._hit_idx = 0
        self._req_id = 0
        self._cur_pixmap: QPixmap | None = None
        self._to_tray_on_close = False

        self._render = render_worker or RenderWorker(self)
        self._render.rendered.connect(self._on_rendered)
        self._owns_render = render_worker is None
        if self._owns_render:
            self._render.start()

        self._build_ui()

        self._indexer: IndexWorker | None = None
        if do_index:
            self._start_indexing(roots, workers)
        else:
            self._refresh_status()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(8)

        bar = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("输入你记得的文字 / 文件名…（多词空格=同时含，\"引号\"=精确短语）")
        self.search_box.returnPressed.connect(self._do_search)
        self.search_box.setMinimumHeight(34)
        bar.addWidget(self.search_box, 1)

        self.mode = QComboBox()
        self.mode.addItems(["全部", "仅文件名", "仅内容"])
        self.mode.currentIndexChanged.connect(self._do_search)
        bar.addWidget(self.mode)

        btn = QPushButton("搜索")
        btn.setMinimumHeight(34)
        btn.clicked.connect(self._do_search)
        bar.addWidget(btn)
        root.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.result_list = QListWidget()
        self.result_list.currentItemChanged.connect(self._on_select)
        self.result_list.itemActivated.connect(self._on_activate)  # 双击/回车 → 打开跳页
        split.addWidget(self.result_list)

        split.addWidget(self._build_preview())
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 6)
        root.addWidget(split, 1)

        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status_label = QLabel("准备中…")
        self.status.addWidget(self.status_label)
        self.hotkey_label = QLabel(f"全局热键 {GLOBAL_HOTKEY}")
        self.hotkey_label.setStyleSheet(f"color:{GRAY};")
        self.status.addPermanentWidget(self.hotkey_label)

    def _build_preview(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.setSpacing(6)

        self.preview_title = QLabel("预览")
        self.preview_title.setStyleSheet("font-weight:600;")
        lay.addWidget(self.preview_title)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.StyledPanel)
        self.image_label = QLabel("← 选中左侧结果查看预览")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(f"color:{GRAY};")
        self.scroll.setWidget(self.image_label)
        lay.addWidget(self.scroll, 1)

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("◀ 上一命中页")
        self.prev_btn.clicked.connect(lambda: self._step_hit(-1))
        self.page_label = QLabel("—")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_btn = QPushButton("下一命中页 ▶")
        self.next_btn.clicked.connect(lambda: self._step_hit(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next_btn)
        lay.addLayout(nav)

        ops = QHBoxLayout()
        self.open_btn = QPushButton("打开文件")
        self.open_btn.clicked.connect(self._act_open)
        self.folder_btn = QPushButton("打开所在文件夹")
        self.folder_btn.clicked.connect(self._act_folder)
        self.goto_btn = QPushButton("打开并跳到此页")
        self.goto_btn.clicked.connect(self._act_goto)
        ops.addWidget(self.open_btn)
        ops.addWidget(self.folder_btn)
        ops.addWidget(self.goto_btn)
        lay.addLayout(ops)

        self._set_ops_enabled(False)
        return panel

    def _set_ops_enabled(self, on: bool) -> None:
        for w in (self.open_btn, self.folder_btn, self.goto_btn, self.prev_btn, self.next_btn):
            w.setEnabled(on)

    # ---------- 搜索 ----------
    def _do_search(self) -> None:
        query = self.search_box.text().strip()
        self.result_list.clear()
        self._results = []
        if not query:
            self.preview_title.setText("预览")
            return
        results = search_mod.search(self._conn, query)
        m = self.mode.currentText()
        if m == "仅文件名":
            results = [r for r in results if r.name_hit]
        elif m == "仅内容":
            results = [r for r in results if r.hits]
        self._results = results
        for i, r in enumerate(results):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, i)
            item.setToolTip(r.path)
            w = ResultItem(r)
            item.setSizeHint(w.sizeHint())
            self.result_list.addItem(item)
            self.result_list.setItemWidget(item, w)
        self.preview_title.setText(f"预览　·　命中 {len(results)} 个文件")
        if results:
            self.result_list.setCurrentRow(0)

    # ---------- 选择 / 预览 ----------
    def _on_select(self, cur: QListWidgetItem | None, _prev=None) -> None:
        if cur is None:
            self._cur = None
            self._set_ops_enabled(False)
            return
        idx = cur.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        self._cur = self._results[idx]
        self._hit_idx = 0
        self._set_ops_enabled(True)
        self._request_preview()

    def _current_page(self) -> int:
        if self._cur and self._cur.hits:
            return self._cur.hits[self._hit_idx].page_no
        return 1

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
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("渲染中…")
        self._req_id += 1
        self._render.request(self._req_id, self._cur.path, page, cache_key=None)

    def _step_hit(self, delta: int) -> None:
        if not self._cur or not self._cur.hits:
            return
        self._hit_idx = max(0, min(len(self._cur.hits) - 1, self._hit_idx + delta))
        self._request_preview()

    def _on_rendered(self, req_id: int, png: str) -> None:
        if req_id != self._req_id:
            return  # 过期请求
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
        scaled = self._cur_pixmap.scaled(
            vp.width() - 4, vp.height() - 4, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.image_label.setText("")
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, e):  # noqa: N802 Qt 命名
        super().resizeEvent(e)
        self._update_pixmap()

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
        self.status_label.setText(f"开始索引：{', '.join(roots)}")
        self._indexer.start()

    def _on_index_progress(self, done: int, total: int, cur: str) -> None:
        name = os.path.basename(cur)
        self.status_label.setText(f"索引中… {done}/{total}　{name}")

    def _on_index_done(self, summary: dict) -> None:
        self._refresh_status(summary)

    def _refresh_status(self, summary: dict | None = None) -> None:
        try:
            s = db.stats(self._conn)
            extra = ""
            if summary and "deleted" in summary:
                extra = f"（新增/更新 {summary.get('indexed', 0)}，移除 {summary.get('deleted', 0)}）"
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
            if not self._render.wait(8000):  # COM 退出可能稍慢
                self._render.terminate()
                self._render.wait(2000)
