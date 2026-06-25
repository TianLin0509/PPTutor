"""库体检中心：把「健康诊断」从给 App 量血压，升级成给用户 PPT 库做体检。

体检报告（health.scan_health）→ 5 类病灶卡片；头号治疗=重复回收（勾选重复组、
一键送系统回收站，可还原）。扫描 / 回收都走后台线程（BackgroundTask），不卡 UI。
样式直接复用全局 QSS 的玻璃卡 token（dashCard / dashTitle / primary…）+ 纯色窗底。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
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

from .. import health
from ..health import human_bytes
from .bg_task import BackgroundTask

_DUP_SHOW_LIMIT = 40  # 极端库下避免一次塞太多行卡 UI


class HealthWindow(QWidget):
    """库体检窗口。scan_fn() -> HealthReport（后台调）；recycle_fn(paths) -> dict（后台调）。"""

    def __init__(self, tok: dict, scan_fn, recycle_fn, parent=None):
        super().__init__(parent)
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
        self._closing = False
        self._closing_owner = parent
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None

        self.setObjectName("healthWin")
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
        btn = self._recycle_btn
        if btn is not None:
            try:
                btn.setEnabled(on)
            except RuntimeError:
                self._recycle_btn = None

    # ---------- 渲染 ----------
    def _render(self, r: health.HealthReport) -> None:
        self._score_lab.setText(str(r.score) if r.deck_count else "—")
        self._score_lab.setStyleSheet(f"color:{self._score_color(r.score)}; font-weight:800;")
        self._summary.setText(self._verdict(r))
        self._clear(self._body)
        self._dup_checks = []
        self._recycle_btn = None
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
            cb = QCheckBox(
                f"{os.path.basename(g.keep_path)}　·　{g.copies} 份　·　可回收 {human_bytes(g.reclaimable)}")
            cb.setChecked(True)
            cb.setToolTip(
                "保留：" + g.keep_path
                + "\n送回收站其余 " + str(g.redundant) + " 份：\n" + "\n".join(g.paths[1:]))
            lay.addWidget(cb)
            self._dup_checks.append((cb, g))
        if len(groups) > len(shown):
            lay.addWidget(self._muted(f"（仅列出可回收最多的前 {len(shown)} 组，共 {len(groups)} 组）"))
        row = QHBoxLayout()
        row.addStretch(1)
        self._recycle_btn = QPushButton("一键回收所选（送回收站，可还原）")
        self._recycle_btn.setObjectName("primary")
        self._recycle_btn.setToolTip("每组保留 1 份，其余送入系统回收站，可随时还原")
        self._recycle_btn.clicked.connect(self._recycle_selected)
        row.addWidget(self._recycle_btn)
        lay.addLayout(row)
        return w

    def _zombie_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🧟 僵尸冷文件")
        if r.zombie_count:
            lay.addWidget(self._line(f"{r.zombie_count} 份超过一年没动过，占 {human_bytes(r.zombie_bytes)}"))
            lay.addWidget(self._muted("老旧素材可考虑归档，给常用库瘦身。"))
        else:
            lay.addWidget(self._muted("没有一年以上没碰的冷文件 ✅"))
        return w

    def _curse_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("⚠️ 终版诅咒")
        if r.curse_count:
            lay.addWidget(self._line(f"{r.curse_count} 份命中「最终版 / 终版 / 定稿 / v9」等命名梗"))
            lay.addWidget(self._muted("「最终版」往往不是最终版——交给版本管理，别再手动改名续命。"))
        else:
            lay.addWidget(self._muted("命名很克制，没有终版诅咒 ✅"))
        return w

    def _bloat_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🐘 体积巨无霸 · 超长页数")
        if r.bloat_biggest:
            lay.addWidget(self._line(f"最大：{r.bloat_biggest[0]}　·　{human_bytes(r.bloat_biggest[1])}"))
        if r.bloat_longest:
            lay.addWidget(self._line(f"最长：{r.bloat_longest[0]}　·　{r.bloat_longest[1]} 页"))
        if not r.bloat_biggest and not r.bloat_longest:
            lay.addWidget(self._muted("暂无数据"))
        return w

    def _parse_card(self, r: health.HealthReport) -> QFrame:
        w, lay = self._card("🩹 解析失败")
        if r.parse_failed:
            detail = "、".join(f"{k}×{v}" for k, v in r.parse_failed_by_status.items())
            lay.addWidget(self._line(f"{r.parse_failed} 份没能提取内容（{detail}）"))
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
        self._recycle_inflight = True
        self._summary.setText("正在送回收站…")
        self._set_controls_enabled(False)
        recycle_fn = self._recycle_fn
        task = BackgroundTask(lambda: recycle_fn(paths), "health-recycle", self)
        self._track(task, self._recycle_tasks)
        task.done.connect(self._on_recycled)
        task.finished.connect(lambda t=task: self._forget(t, self._recycle_tasks))
        task.start()

    def _on_recycled(self, result: object) -> None:
        if not self._ui_alive():
            return
        self._recycle_inflight = False
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
        self.refresh()
