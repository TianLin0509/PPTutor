"""Single-file PPT slimming window."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
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

from .. import slim
from .bg_task import BackgroundTask


class SlimWindow(QWidget):
    """Analyze and create a slimmed copy of one PPTX without using PowerPoint COM."""

    def __init__(self, tok: dict, path: str, parent=None, *, analyze_fn=None, slim_fn=None):
        qt_parent = parent if isinstance(parent, QWidget) else None
        super().__init__(qt_parent)
        self._tok = tok or {}
        self._path = os.path.abspath(path)
        self._analyze_fn = analyze_fn or slim.analyze_pptx
        self._slim_fn = slim_fn or slim.slim_pptx
        self._report: slim.SlimReport | None = None
        self._closing = False
        self._closing_owner = parent
        self._scan_inflight = False
        self._slim_inflight = False
        self._tasks: list[BackgroundTask] = []
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None

        self.setObjectName("slimWin")
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle("PPT 瘦身 · PPT Doctor")
        self.resize(760, 720)
        self._build()
        self._apply_window_bg()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("PPT 瘦身体检")
        title.setObjectName("dashTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._refresh_btn = QPushButton("重新分析")
        self._refresh_btn.setObjectName("suggBtn")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        root.addLayout(head)

        self._path_label = QLabel(self._path)
        self._path_label.setObjectName("dashSub")
        self._path_label.setWordWrap(True)
        root.addWidget(self._path_label)

        self._summary = QLabel("正在分析…")
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

        foot = QHBoxLayout()
        foot.addStretch(1)
        self._make_btn = QPushButton("生成瘦身副本")
        self._make_btn.setObjectName("primary")
        self._make_btn.setEnabled(False)
        self._make_btn.setToolTip("只生成新 PPTX，不覆盖源文件，也不启动 PowerPoint")
        self._make_btn.clicked.connect(self._make_slim_copy)
        foot.addWidget(self._make_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.setObjectName("suggBtn")
        self._close_btn.clicked.connect(self.close)
        foot.addWidget(self._close_btn)
        root.addLayout(foot)

    def _apply_window_bg(self) -> None:
        win = self._tok.get("win", "#1d1d1f")
        self.setStyleSheet(f"QWidget#slimWin {{ background: {win}; }}")

    def _ui_alive(self) -> bool:
        if self._closing or not _qt_is_valid(self):
            return False
        owner = self._closing_owner
        try:
            return owner is None or not getattr(owner, "_closing", False)
        except RuntimeError:
            return False

    def _track(self, task: BackgroundTask) -> None:
        self._tasks.append(task)
        if self._parent_bg_tasks is not None and task not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(task)

    def _forget(self, task: BackgroundTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        if self._parent_bg_tasks is not None and task in self._parent_bg_tasks:
            self._parent_bg_tasks.remove(task)

    def closeEvent(self, event):  # noqa: N802
        self._closing = True
        super().closeEvent(event)

    def refresh(self) -> None:
        if self._scan_inflight or self._slim_inflight or not self._ui_alive():
            return
        self._scan_inflight = True
        self._report = None
        self._summary.setText("正在分析 PPTX 包结构…")
        self._make_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._clear(self._body)
        self._body.addWidget(self._muted("正在读取 ZIP/OpenXML 结构，不会启动 PowerPoint。"))
        task = BackgroundTask(lambda: self._analyze_fn(self._path), "ppt-slim-analyze", self)
        self._track(task)
        task.done.connect(self._on_analyzed)
        task.finished.connect(lambda t=task: self._forget(t))
        task.start()

    def _on_analyzed(self, report: object) -> None:
        if not self._ui_alive():
            return
        self._scan_inflight = False
        self._refresh_btn.setEnabled(True)
        if not isinstance(report, slim.SlimReport):
            self._summary.setText("分析失败：这可能不是有效 PPTX，或文件正被占用。")
            self._make_btn.setEnabled(False)
            return
        self._report = report
        self._render_report(report)
        self._make_btn.setEnabled(True)

    def _render_report(self, r: slim.SlimReport) -> None:
        self._summary.setText(
            f"原始大小 {slim.human_bytes(r.original_size)} · "
            f"低风险预计可省 {slim.human_bytes(r.low_risk_reclaimable)} · "
            f"{r.package_parts} 个包内文件"
        )
        self._clear(self._body)
        self._body.addWidget(self._section("体积来源", [
            f"{b.label}：{b.count} 个 · {slim.human_bytes(b.compressed_bytes)}"
            for b in r.buckets[:8]
        ] or ["暂无数据"]))
        self._body.addWidget(self._section("低风险可自动处理", [
            "重新打包 PPTX：清理 zip 结构并使用稳定压缩",
            f"完全重复媒体：{len(r.duplicate_media_groups)} 组 · 可省 {slim.human_bytes(r.duplicate_media_reclaimable)}",
            f"无引用部件：{len(r.orphan_parts)} 个 · 可省 {slim.human_bytes(r.orphan_reclaimable)}",
            f"包内垃圾：{len(r.junk_parts)} 个 · 可省 {slim.human_bytes(r.junk_reclaimable)}",
        ]))
        self._body.addWidget(self._section("需要确认，不默认处理", [
            f"未使用版式：{len(r.unused_layouts)} 个",
            f"未使用母版：{len(r.unused_masters)} 个",
            "图片降采样、删除裁剪外区域、备注/隐藏页清理都需要单独确认",
        ]))
        if r.high_risk_notes:
            self._body.addWidget(self._section("高风险提示", list(r.high_risk_notes)))
        self._body.addStretch(1)

    def _make_slim_copy(self) -> None:
        if self._slim_inflight or self._scan_inflight or not self._ui_alive():
            return
        default_path = slim.default_output_path(self._path)
        out, _ = QFileDialog.getSaveFileName(self, "生成瘦身副本", default_path, "PowerPoint (*.pptx)")
        if not out:
            return
        if not out.lower().endswith(".pptx"):
            out += ".pptx"
        if os.path.normcase(os.path.abspath(out)) == os.path.normcase(self._path):
            QMessageBox.warning(self, "不能覆盖源文件", "不能覆盖源文件。请为瘦身副本选择一个新文件名。")
            return
        self._slim_inflight = True
        self._make_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._summary.setText("正在生成瘦身副本…")
        task = BackgroundTask(lambda out=out: self._run_slim_copy(out), "ppt-slim-create", self)
        self._track(task)
        task.done.connect(self._on_slim_done)
        task.finished.connect(lambda t=task: self._forget(t))
        task.start()

    def _run_slim_copy(self, out: str) -> slim.SlimResult:
        try:
            # 用户已在系统另存对话框选定（含覆盖确认）→ 允许覆盖，避免"已确认替换却报 output already exists"
            result = self._slim_fn(self._path, out, overwrite=True)
            if isinstance(result, slim.SlimResult):
                return result
            raise RuntimeError("slim function returned an invalid result")
        except Exception as exc:  # noqa: BLE001 - surface the real reason to the user
            try:
                original_size = os.path.getsize(self._path)
            except OSError:
                original_size = 0
            return slim.SlimResult(
                ok=False,
                source_path=self._path,
                output_path=os.path.abspath(out),
                original_size=original_size,
                slim_size=0,
                saved_bytes=0,
                removed_parts=(),
                deduped_media=0,
                actions=(),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _on_slim_done(self, result: object) -> None:
        if not self._ui_alive():
            return
        self._slim_inflight = False
        self._refresh_btn.setEnabled(True)
        self._make_btn.setEnabled(self._report is not None)
        if not isinstance(result, slim.SlimResult) or not result.ok:
            error = result.error if isinstance(result, slim.SlimResult) else ""
            QMessageBox.warning(self, "瘦身失败", error or "生成瘦身副本失败，请稍后重试。")
            return
        owner = self._closing_owner
        try:
            hook = getattr(owner, "_after_slim_created", None)
        except RuntimeError:
            hook = None
        if callable(hook):
            hook(result)
        QMessageBox.information(
            self,
            "瘦身完成",
            f"已生成瘦身副本：\n{result.output_path}\n\n"
            f"原始 {slim.human_bytes(result.original_size)} · "
            f"现在 {slim.human_bytes(result.slim_size)} · "
            f"节省 {slim.human_bytes(result.saved_bytes)}",
        )
        self.refresh()

    def _section(self, title: str, lines: list[str]) -> QFrame:
        w = QFrame()
        w.setObjectName("dashCard")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 15, 18, 16)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("dashCardT")
        lay.addWidget(t)
        for line in lines:
            lay.addWidget(self._line(line))
        return w

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

    @staticmethod
    def _clear(box: QVBoxLayout) -> None:
        while box.count():
            it = box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
