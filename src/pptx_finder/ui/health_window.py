"""库体检中心：把「健康诊断」从给 App 量血压，升级成给用户 PPT 库做体检。

体检报告（health.scan_health）→ 5 类病灶卡片；头号治疗=重复回收（勾选重复组、
一键送系统回收站，可还原）。扫描 / 回收都走后台线程（BackgroundTask），不卡 UI。
样式直接复用全局 QSS 的玻璃卡 token（dashCard / dashTitle / primary…）+ 纯色窗底。
"""
from __future__ import annotations

import html
import os

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import actions  # 独立窗（embedded=False）拿不到主窗钩子时的降级：资源管理器定位文件
from .. import health
from ..health import human_bytes
from .bg_task import BackgroundTask
from .index_activity_bar import IndexActivityBar

_DUP_SHOW_LIMIT = 40  # 极端库下避免一次塞太多行卡 UI
_RECYCLE_CHUNK = 20   # 底层 _shell_recycle 单次批量无进度回调 → 分块串行换确定态进度
_EXAMPLE_SHOW_LIMIT = 10  # 病灶卡最多列出的可点示例条数（数据层已 cap 10，UI 侧再挡一道）


class HealthWindow(QWidget):
    """库体检窗口。scan_fn() -> HealthReport（后台调）；recycle_fn(paths) -> dict（后台调）。"""

    recycle_progress = Signal(int, int)  # 分块回收进度 (done, total)，后台线程 emit，跨线程安全

    def __init__(self, tok: dict, scan_fn, recycle_fn, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self._embedded = embedded  # 嵌入主窗健康页时跳过顶层窗语义（Window flag/标题/尺寸/纯色窗底）
        self._tok = tok or {}
        self._scan_fn = scan_fn
        self._recycle_fn = recycle_fn
        self._report: health.HealthReport | None = None
        self._scan_tasks: list[BackgroundTask] = []
        self._recycle_tasks: list[BackgroundTask] = []
        self._scan_inflight = False
        self._recycle_inflight = False
        self._dup_checks: list[tuple[QCheckBox, health.DuplicateGroup]] = []
        self._recycle_btn: QPushButton | None = None
        self._recycle_all_btn: QPushButton | None = None
        self._progress: IndexActivityBar | None = None
        self._closing = False
        self._closing_owner = parent
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self.recycle_progress.connect(self._on_recycle_progress)

        self.setObjectName("healthWin")
        if not self._embedded:
            self.setWindowFlag(Qt.Window, True)   # 独立顶层窗口（即便有 parent）
            self.setWindowTitle("库体检 · PPT Doctor")
            self.resize(720, 760)
        self._build()
        self._apply_window_bg()
        self.refresh()

    # ---------- 结构 ----------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("🩺 库体检报告")
        title.setObjectName("dashTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._score_lab = QLabel("—")
        self._score_lab.setObjectName("kpiNum")
        self._score_lab.setToolTip("库健康分（0-100）：重复 / 解析失败扣最狠，僵尸 / 命名诅咒次之")
        head.addWidget(self._score_lab)
        self._refresh_btn = QPushButton("重新体检")
        self._refresh_btn.setObjectName("suggBtn")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        root.addLayout(head)

        self._summary = QLabel("正在体检…")
        self._summary.setObjectName("dashSub")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 2)
        self._body.setSpacing(12)
        self._body.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def _apply_window_bg(self) -> None:
        if self._embedded:
            return  # 嵌入主窗页面：透明底随主窗 central，不套纯色窗底
        win = self._tok.get("win", "#1d1d1f")
        self.setStyleSheet(f"QWidget#healthWin {{ background: {win}; }}")

    def set_theme(self, tok: dict) -> None:
        self._tok = tok or self._tok
        self._apply_window_bg()
        if self._report is not None:
            self._render(self._report)

    # ---------- 生命周期 ----------
    def _ui_alive(self) -> bool:
        if self._closing or not _qt_is_valid(self):
            return False
        owner = self._closing_owner
        try:
            return owner is None or not getattr(owner, "_closing", False)
        except RuntimeError:
            return False

    def _track(self, task: BackgroundTask, bucket: list) -> None:
        bucket.append(task)
        if self._parent_bg_tasks is not None and task not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(task)

    def _forget(self, task: BackgroundTask, bucket: list) -> None:
        try:
            if task in bucket:
                bucket.remove(task)
        except RuntimeError:
            pass
        if self._parent_bg_tasks is not None and task in self._parent_bg_tasks:
            self._parent_bg_tasks.remove(task)

    def closeEvent(self, event):  # noqa: N802
        self._closing = True
        super().closeEvent(event)

    # ---------- 扫描 ----------
    def refresh(self) -> None:
        if self._scan_inflight or self._recycle_inflight or not self._ui_alive():
            return
        self._scan_inflight = True
        self._summary.setText("正在体检…")
        self._set_controls_enabled(False)
        scan_fn = self._scan_fn
        task = BackgroundTask(scan_fn, "health-scan", self)
        self._track(task, self._scan_tasks)
        task.done.connect(self._on_scanned)
        task.finished.connect(lambda t=task: self._forget(t, self._scan_tasks))
        task.start()

    def _on_scanned(self, report: object) -> None:
        if not self._ui_alive():
            return
        self._scan_inflight = False
        if not isinstance(report, health.HealthReport):
            self._summary.setText("体检失败，请点「重新体检」重试")
            self._set_controls_enabled(True)
            return
        self._report = report
        self._render(report)
        self._set_controls_enabled(True)

    def _set_controls_enabled(self, on: bool) -> None:
        self._refresh_btn.setEnabled(on)
        for attr in ("_recycle_btn", "_recycle_all_btn"):
            btn = getattr(self, attr)
            if btn is not None:
                try:
                    btn.setEnabled(on)
                except RuntimeError:
                    setattr(self, attr, None)

    # ---------- 渲染 ----------
    def _render(self, r: health.HealthReport) -> None:
        self._score_lab.setText(str(r.score) if r.deck_count else "—")
        self._score_lab.setStyleSheet(f"color:{self._score_color(r.score)}; font-weight:800;")
        self._summary.setText(self._verdict(r))
        self._clear(self._body)
        self._dup_checks = []
        self._recycle_btn = None
        self._recycle_all_btn = None
        self._progress = None
        self._body.addWidget(self._dup_card(r))
        self._body.addWidget(self._zombie_card(r))
        self._body.addWidget(self._curse_card(r))
        self._body.addWidget(self._bloat_card(r))
        self._body.addWidget(self._parse_card(r))
        self._body.addStretch(1)

    def _score_color(self, score: int) -> str:
        if score >= 85:
            return self._tok.get("grn", "#34c759")
        if score >= 60:
            return "#ff9f0a"
        return "#ff453a"

    def _verdict(self, r: health.HealthReport) -> str:
        if r.deck_count == 0:
            return "库里还没有 PPT，等索引跑完再来体检。"
        bits = []
        if r.duplicate_redundant:
            bits.append(f"{r.duplicate_redundant} 份重复可回收 {human_bytes(r.duplicate_reclaimable)}")
        if r.zombie_count:
            bits.append(f"{r.zombie_count} 份僵尸冷文件")
        if r.parse_failed:
            bits.append(f"{r.parse_failed} 份解析失败")
        if not bits:
            return f"共 {r.deck_count} 份 · 库很健康，没发现明显问题 ✅"
        return f"共 {r.deck_count} 份 · " + "　·　".join(bits)

    # ---------- 卡片 ----------
    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        w = QFrame()
        w.setObjectName("dashCard")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 15, 18, 16)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("dashCardT")
        lay.addWidget(t)
        return w, lay

    def _line(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("recName")
        lbl.setWordWrap(True)
        return lbl

    def _muted(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("dashSub")
        lbl.setWordWrap(True)
        return lbl

    # ---------- 病灶条目链接 ----------
    def _mk_link(self, name: str, path: str) -> QLabel:
        """可点条目：acc 色链接、hover 下划线、tooltip 全路径；点击经主窗钩子跳转定位。"""
        disp = (name or os.path.basename(path)).strip() or path
        if len(disp) > 26:
            disp = disp[:25] + "…"
        acc = self._tok.get("acc", "#0A84FF")
        esc = html.escape(disp)
        lbl = QLabel()
        lbl.setObjectName("healthLink")
        lbl.setTextFormat(Qt.RichText)
        lbl.setOpenExternalLinks(False)
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.setToolTip(path)
        plain = f'<a href="loc" style="color:{acc}; text-decoration:none;">{esc}</a>'
        hover = f'<a href="loc" style="color:{acc}; text-decoration:underline;">{esc}</a>'
        lbl.setProperty("_link_plain", plain)  # eventFilter hover 切换下划线用
        lbl.setProperty("_link_hover", hover)
        lbl.setText(plain)
        lbl.installEventFilter(self)
        lbl.linkActivated.connect(lambda _l, p=path, n=name: self._activate_health_link(p, n))
        return lbl

    def eventFilter(self, obj, event):  # noqa: N802
        if isinstance(obj, QLabel) and obj.objectName() == "healthLink":
            if event.type() == QEvent.Enter:
                obj.setText(obj.property("_link_hover") or obj.text())
            elif event.type() == QEvent.Leave:
                obj.setText(obj.property("_link_plain") or obj.text())
        return super().eventFilter(obj, event)

    def _activate_health_link(self, path: str, name: str) -> None:
        """统一跳转：主窗在手（embedded）经 _locate_health_item 切搜索页定位；
        独立窗拿不到钩子时降级为资源管理器打开所在文件夹。"""
        owner = self._closing_owner
        try:
            hook = getattr(owner, "_locate_health_item", None)
        except RuntimeError:
            hook = None
        if callable(hook):
            hook(path, name)
            return
        actions.open_folder(path)

    def _add_example_links(self, lay: QVBoxLayout, examples) -> None:
        for ex in examples[:_EXAMPLE_SHOW_LIMIT]:
            lay.addWidget(self._mk_link(ex.name, ex.path))

    def _dup_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🧹 重复堆积")
        groups = r.duplicate_groups
        if not groups:
            lay.addWidget(self._muted("没有完全相同的副本，干净 ✅"))
            return w
        lay.addWidget(self._muted(
            f"发现 {len(groups)} 组完全相同的副本，共 {r.duplicate_redundant} 份冗余 · "
            f"可回收 {human_bytes(r.duplicate_reclaimable)}"))
        shown = groups[:_DUP_SHOW_LIMIT]
        for g in shown:
            row_g = QHBoxLayout()
            row_g.setSpacing(8)
            # 文件名做成可点链接（定位保留项）；复选框结构不变，_recycle_selected 遍历不受影响
            row_g.addWidget(self._mk_link(os.path.basename(g.keep_path), g.keep_path))
            cb = QCheckBox(f"{g.copies} 份　·　可回收 {human_bytes(g.reclaimable)}")
            cb.setChecked(True)
            cb.setToolTip(
                "保留：" + g.keep_path
                + "\n送回收站其余 " + str(g.redundant) + " 份：\n" + "\n".join(g.paths[1:]))
            row_g.addWidget(cb, 1)
            lay.addLayout(row_g)
            self._dup_checks.append((cb, g))
        if len(groups) > len(shown):
            lay.addWidget(self._muted(f"（仅列出可回收最多的前 {len(shown)} 组，共 {len(groups)} 组）"))
        row = QHBoxLayout()
        row.addStretch(1)
        self._progress = IndexActivityBar()
        self._progress.set_accent_color(self._tok.get("acc", "#0A84FF"))
        self._progress.setRange(0, 100)
        self._progress.hide()  # 回收开始才显示，完成后隐藏
        row.addWidget(self._progress)
        self._recycle_btn = QPushButton("一键回收所选（送回收站，可还原）")
        self._recycle_btn.setObjectName("primary")
        self._recycle_btn.setToolTip("每组保留 1 份，其余送入系统回收站，可随时还原")
        self._recycle_btn.clicked.connect(self._recycle_selected)
        row.addWidget(self._recycle_btn)
        self._recycle_all_btn = QPushButton("全部回收")
        self._recycle_all_btn.setObjectName("primary")
        self._recycle_all_btn.setToolTip("全部重复组各保留 1 份，其余送入系统回收站（含未展开列出的组）")
        self._recycle_all_btn.clicked.connect(self._recycle_all)
        row.addWidget(self._recycle_all_btn)
        lay.addLayout(row)
        return w

    def _zombie_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🧟 僵尸冷文件")
        if r.zombie_count:
            lay.addWidget(self._line(f"{r.zombie_count} 份超过一年没动过，占 {human_bytes(r.zombie_bytes)}"))
            self._add_example_links(lay, r.zombie_examples)
            lay.addWidget(self._muted("老旧素材可考虑归档，给常用库瘦身。"))
        else:
            lay.addWidget(self._muted("没有一年以上没碰的冷文件 ✅"))
        return w

    def _curse_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("⚠️ 终版诅咒")
        if r.curse_count:
            lay.addWidget(self._line(f"{r.curse_count} 份命中「最终版 / 终版 / 定稿 / v9」等命名梗"))
            self._add_example_links(lay, r.curse_examples)
            lay.addWidget(self._muted("「最终版」往往不是最终版——交给版本管理，别再手动改名续命。"))
        else:
            lay.addWidget(self._muted("命名很克制，没有终版诅咒 ✅"))
        return w

    def _bloat_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🐘 体积巨无霸 · 超长页数")
        if r.bloat_biggest:
            lay.addWidget(self._bloat_row("最大", r.bloat_biggest, human_bytes(r.bloat_biggest[1])))
        if r.bloat_longest:
            lay.addWidget(self._bloat_row("最长", r.bloat_longest, f"{r.bloat_longest[1]} 页"))
        if not r.bloat_biggest and not r.bloat_longest:
            lay.addWidget(self._muted("暂无数据"))
        return w

    def _bloat_row(self, prefix: str, item, value: str) -> QWidget:
        """巨无霸条目：名字可点跳转（数据层已带 path；兼容旧二元组时退化为纯文本）。"""
        name = item[0]
        path = item[2] if len(item) > 2 else ""
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._line(f"{prefix}："))
        if path:
            row.addWidget(self._mk_link(name, path))
        else:
            row.addWidget(self._line(name))
        row.addWidget(self._muted(f"·　{value}"))
        row.addStretch(1)
        return w

    def _parse_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🩹 解析失败")
        if r.parse_failed:
            detail = "、".join(f"{k}×{v}" for k, v in r.parse_failed_by_status.items())
            lay.addWidget(self._line(f"{r.parse_failed} 份没能提取内容（{detail}）"))
            self._add_example_links(lay, r.parse_failed_examples)
            lay.addWidget(self._muted("多为加密 / 损坏 / 超大跳过；这些文件搜不到内文。"))
        else:
            lay.addWidget(self._muted("全部文件都成功解析 ✅"))
        return w

    @staticmethod
    def _clear(box: QVBoxLayout) -> None:
        while box.count():
            it = box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    # ---------- 治疗：回收 ----------
    def _recycle_selected(self) -> None:
        if self._recycle_inflight or self._scan_inflight or not self._ui_alive():
            return
        paths: list[str] = []
        freed_est = 0
        for cb, g in self._dup_checks:
            if cb.isChecked():
                paths.extend(g.paths[1:])
                freed_est += g.reclaimable
        if not paths:
            QMessageBox.information(self, "回收", "没有勾选要清理的重复组")
            return
        msg = (f"将把 {len(paths)} 份冗余副本送入系统回收站（每组保留 1 份），"
               f"预计回收 {human_bytes(freed_est)}。\n\n文件进回收站，可随时还原。继续？")
        if QMessageBox.question(self, "一键回收重复", msg) != QMessageBox.Yes:
            return
        self._start_recycle(paths)

    def _recycle_all(self) -> None:
        """全部回收：收集 report 全量重复组（不受 _DUP_SHOW_LIMIT 展示上限影响），每组保留 keep_path。"""
        if self._recycle_inflight or self._scan_inflight or not self._ui_alive():
            return
        r = self._report
        if r is None:
            return
        paths: list[str] = []
        freed_est = 0
        for g in r.duplicate_groups:
            paths.extend(g.paths[1:])
            freed_est += g.reclaimable
        if not paths:
            QMessageBox.information(self, "回收", "没有可清理的重复组")
            return
        msg = (f"将把全部 {len(r.duplicate_groups)} 组共 {len(paths)} 份冗余副本送入系统回收站"
               f"（每组保留 1 份），预计回收 {human_bytes(freed_est)}。\n\n文件进回收站，可随时还原。继续？")
        if QMessageBox.question(self, "全部回收重复", msg) != QMessageBox.Yes:
            return
        self._start_recycle(paths)

    def _start_recycle(self, paths: list[str]) -> None:
        """统一回收入口：一个后台任务内按 _RECYCLE_CHUNK 分块串行调 recycle_fn，
        每块完成经 recycle_progress Signal 回报 (done, total)；各块结果汇总成原 dict 形状。"""
        self._recycle_inflight = True
        self._summary.setText("正在送回收站…")
        self._set_controls_enabled(False)
        total = len(paths)
        bar = self._progress
        if bar is not None:
            bar.setRange(0, total)
            bar.setValue(0)
            bar.show()
        recycle_fn = self._recycle_fn

        def _run() -> dict:
            merged = {"ok": True, "recycled": 0, "recycled_paths": [], "failed": [],
                      "freed_bytes": 0, "index_deleted": 0, "error": ""}
            for i in range(0, total, _RECYCLE_CHUNK):
                chunk = paths[i:i + _RECYCLE_CHUNK]
                res = recycle_fn(chunk) or {}
                merged["recycled"] += int(res.get("recycled", 0) or 0)
                merged["recycled_paths"].extend(res.get("recycled_paths") or [])
                merged["failed"].extend(res.get("failed") or [])
                merged["freed_bytes"] += int(res.get("freed_bytes", 0) or 0)
                merged["index_deleted"] += int(res.get("index_deleted", 0) or 0)
                if res.get("index_error"):
                    merged["index_error"] = res["index_error"]
                if not res.get("ok"):
                    merged["ok"] = False
                    if not merged["error"]:
                        merged["error"] = str(res.get("error", "") or "")
                done = min(total, i + len(chunk))
                try:
                    self.recycle_progress.emit(done, total)
                except RuntimeError:
                    pass  # 窗口已销毁：进度无人接收，回收继续跑完
            return merged

        task = BackgroundTask(_run, "health-recycle", self)
        self._track(task, self._recycle_tasks)
        task.done.connect(self._on_recycled)
        task.finished.connect(lambda t=task: self._forget(t, self._recycle_tasks))
        task.start()

    def _on_recycle_progress(self, done: int, total: int) -> None:
        if not self._ui_alive():
            return
        bar = self._progress
        if bar is not None:
            bar.setValue(done)
        self._summary.setText(f"正在送回收站… {done}/{total}")

    def _on_recycled(self, result: object) -> None:
        if not self._ui_alive():
            return
        self._recycle_inflight = False
        if self._progress is not None:
            self._progress.hide()
        res = result if isinstance(result, dict) else {}
        n = int(res.get("recycled", 0))
        freed = int(res.get("freed_bytes", 0))
        if res.get("ok"):
            QMessageBox.information(
                self, "已回收",
                f"已把 {n} 份送入回收站，回收 {human_bytes(freed)}。可在系统回收站还原。")
        else:
            QMessageBox.warning(
                self, "回收未完成",
                f"已回收 {n} 份（{human_bytes(freed)}）；部分未成功：{res.get('error', '') or '请稍后重试'}")
        owner = self._closing_owner
        try:
            hook = getattr(owner, "_after_health_recycle", None)
        except RuntimeError:
            hook = None
        if callable(hook):
            hook(res)
        self.refresh()
