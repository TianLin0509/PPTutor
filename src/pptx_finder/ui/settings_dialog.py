"""Settings and diagnostics center."""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import db
from ..config import (
    APP_NAME,
    EXCLUDE_DIR_NAMES,
    GLOBAL_HOTKEY,
    cache_dir,
    data_dir,
    db_path,
)
from ..versioning import autostart
from .bg_task import BackgroundTask


class SettingsDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self._diag_tasks: list[BackgroundTask] = []
        self.setWindowTitle("设置 · PPTutor")
        self.resize(620, 430)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_guard_tab(), "守护")
        self.tabs.addTab(self._build_health_tab(), "健康诊断")
        self.tabs.addTab(self._build_powerpoint_tab(), "PowerPoint")
        lay.addWidget(self.tabs, 1)

    def _build_guard_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(14)

        title = QLabel("版本管理")
        title.setStyleSheet("font-weight:700;font-size:15px;")
        lay.addWidget(title)

        desc = QLabel(
            "全盘自动守护：你用 PowerPoint 改过、保存过的 PPT 会自动留版本。"
            "只有之后新建或之后改存过的 PPTX 会进入管理；历史存量不会自动追溯。"
        )
        desc.setWordWrap(True)
        lay.addWidget(desc)

        self.stat = QLabel(self._version_stat_text())
        lay.addWidget(self.stat)

        self.auto = QCheckBox("开机自动启动")
        self.auto.setToolTip("建议开启，这样关机后重新登录也能继续守护保存事件。")
        self.auto.setChecked(autostart.is_enabled())
        self.auto.toggled.connect(self._toggle_auto)
        lay.addWidget(self.auto)
        lay.addStretch(1)
        return page

    def _build_health_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("健康诊断")
        title.setStyleSheet("font-weight:700;font-size:15px;")
        head.addWidget(title, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_diagnostics)
        head.addWidget(refresh)
        copy = QPushButton("复制")
        copy.clicked.connect(self._copy_diagnostics)
        head.addWidget(copy)
        lay.addLayout(head)

        self.diagnostic_text = QPlainTextEdit()
        self.diagnostic_text.setReadOnly(True)
        self.diagnostic_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.diagnostic_text, 1)
        self.refresh_diagnostics()
        return page

    def _build_powerpoint_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        title = QLabel("PowerPoint 检测")
        title.setStyleSheet("font-weight:700;font-size:15px;")
        lay.addWidget(title)
        desc = QLabel("检测预览和跳转所需的 PowerPoint COM 能力。检测在后台执行，不会关闭用户已有的演示文稿。")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        self.powerpoint_status = QLabel("尚未检测")
        self.powerpoint_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.powerpoint_status.setWordWrap(True)
        lay.addWidget(self.powerpoint_status)

        self.powerpoint_btn = QPushButton("开始检测")
        self.powerpoint_btn.clicked.connect(self._check_powerpoint)
        lay.addWidget(self.powerpoint_btn, 0, Qt.AlignLeft)
        lay.addStretch(1)
        return page

    def _version_stat_text(self) -> str:
        try:
            n = len(self._mgr.list_docs())
        except Exception:  # noqa: BLE001
            n = 0
        return f"当前已在守护 {n} 个你改过的文件。"

    def _toggle_auto(self, on: bool) -> None:
        autostart.set_enabled(on)

    def refresh_diagnostics(self) -> None:
        lines = [
            f"app: {APP_NAME}",
            f"python: {sys.version.split()[0]} ({platform.platform()})",
            f"data_dir: {data_dir()}",
            f"db_path: {db_path()}",
            f"cache_dir: {cache_dir()}",
            f"global_hotkey: {GLOBAL_HOTKEY}",
            f"autostart: {'on' if autostart.is_enabled() else 'off'}",
            f"PPTX_FINDER_ROOTS: {os.environ.get('PPTX_FINDER_ROOTS', '') or '(auto fixed drives)'}",
            f"PPTX_FINDER_DATA_DIR: {os.environ.get('PPTX_FINDER_DATA_DIR', '') or '(default)'}",
            f"exclude_dirs: {len(EXCLUDE_DIR_NAMES)} rules",
        ]
        try:
            parent = self.parent()
            conn = getattr(parent, "_conn", None)
            if conn is None:
                own = db.connect(db_path())
                try:
                    db.init_db(own)
                    s = db.stats(own)
                finally:
                    own.close()
            else:
                s = db.stats(conn)
            lines.append(f"index: {s['file_count']} files / {s['page_count']} pages")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"index: unavailable ({type(exc).__name__}: {exc})")
        try:
            lines.append(f"versions: {len(self._mgr.list_docs())} guarded docs")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"versions: unavailable ({type(exc).__name__}: {exc})")

        for p in (data_dir(), cache_dir(), db_path()):
            lines.append(f"exists {Path(p).name}: {Path(p).exists()}")
        self.diagnostic_text.setPlainText("\n".join(lines))
        self.stat.setText(self._version_stat_text())

    def _copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(self.diagnostic_text.toPlainText())

    def _check_powerpoint(self) -> None:
        self.powerpoint_btn.setEnabled(False)
        self.powerpoint_status.setText("正在检测…")
        task = BackgroundTask(_probe_powerpoint, "powerpoint-diagnostic", self)
        self._diag_tasks.append(task)
        task.done.connect(self._on_powerpoint_checked)
        task.finished.connect(lambda: self._diag_tasks.remove(task) if task in self._diag_tasks else None)
        task.start()

    def _on_powerpoint_checked(self, result: object) -> None:
        self.powerpoint_btn.setEnabled(True)
        self.powerpoint_status.setText(str(result or "检测失败，请查看日志。"))

    def closeEvent(self, event):  # noqa: N802
        for task in list(self._diag_tasks):
            task.wait(1000)
        super().closeEvent(event)


def _probe_powerpoint() -> str:
    if os.name != "nt":
        return "当前不是 Windows，跳过 PowerPoint COM 检测。"
    app = None
    pythoncom = None
    initialized = False
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
        # DispatchEx creates an isolated automation instance. It must not attach
        # to, close, or quit the user's existing PowerPoint window.
        app = win32com.client.DispatchEx("PowerPoint.Application")
        version = getattr(app, "Version", "")
        return f"PowerPoint COM 可用，版本 {version or '未知'}。"
    except Exception as exc:  # noqa: BLE001
        return f"PowerPoint COM 不可用：{type(exc).__name__}: {exc}"
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
