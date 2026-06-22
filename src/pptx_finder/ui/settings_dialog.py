"""Settings and diagnostics center."""
from __future__ import annotations

from collections.abc import Callable
import os
import platform
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

_MOD_NAMES = ("Ctrl", "Alt", "Shift", "Win")


class HotkeyEdit(QLineEdit):
    """录制全局热键：聚焦后按下「修饰键 + 主键」即捕获为 'Ctrl+Alt+P' 形式（#2）。"""

    def __init__(self, spec: str = "", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self._spec = spec
        self.setText(spec or "点此后按下组合键…")

    def spec(self) -> str:
        return self._spec

    def keyPressEvent(self, e):  # noqa: N802
        key = e.key()
        if key in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta):
            return  # 单独的修饰键不算，等主键
        mods = e.modifiers()
        parts = []
        if mods & Qt.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.AltModifier:
            parts.append("Alt")
        if mods & Qt.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.MetaModifier:
            parts.append("Win")
        main = QKeySequence(key).toString()
        if not main:
            return
        parts.append(main)
        self._spec = "+".join(parts)
        self.setText(self._spec)
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import db
from ..config import (
    APP_NAME,
    EXCLUDE_DIR_NAMES,
    cache_dir,
    data_dir,
    db_path,
    get_hotkey,
    set_hotkey,
)
from ..versioning import autostart
from .bg_task import BackgroundTask


class SettingsDialog(QDialog):
    def __init__(self, manager, parent=None, on_rescan: Callable[[], None] | None = None):
        super().__init__(parent)
        self._mgr = manager
        self._diagnostic_parent = parent
        self._closing_owner = parent
        self._on_rescan = on_rescan
        self._diag_tasks: list[BackgroundTask] = []
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self._diag_refresh_token = 0
        self._diag_inflight_token: int | None = None
        self._diag_extra_lines: list[str] = []
        self._powerpoint_inflight = False
        self._closing = False
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

    def _ui_alive(self) -> bool:
        if (
            self._closing
            or not _qt_is_valid(self)
            or not _qt_is_valid(getattr(self, "diagnostic_text", None))
        ):
            return False
        owner = getattr(self, "_closing_owner", None)
        try:
            return owner is None or not getattr(owner, "_closing", False)
        except RuntimeError:
            return False

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

        self.stat = QLabel("正在读取守护状态…")
        lay.addWidget(self.stat)

        self.auto = QCheckBox("开机自动启动")
        self.auto.setToolTip("建议开启，这样关机后重新登录也能继续守护保存事件。")
        self.auto.setChecked(autostart.is_enabled())
        self.auto.toggled.connect(self._toggle_auto)
        lay.addWidget(self.auto)

        # 全局唤起热键（#2 可改：解决状态栏「热键被占用」无修复路径）
        hk_title = QLabel("全局唤起热键")
        hk_title.setStyleSheet("font-weight:700;font-size:13px;margin-top:8px;")
        lay.addWidget(hk_title)
        hk_desc = QLabel("在下框中按下新组合键（需含 Ctrl / Alt，再加一个字母或数字），点「应用」即时生效。")
        hk_desc.setWordWrap(True)
        lay.addWidget(hk_desc)
        hk_row = QHBoxLayout()
        self._hotkey_edit = HotkeyEdit(get_hotkey())
        hk_row.addWidget(self._hotkey_edit, 1)
        hk_apply = QPushButton("应用")
        hk_apply.clicked.connect(self._apply_hotkey)
        hk_row.addWidget(hk_apply)
        lay.addLayout(hk_row)
        self._hotkey_result = QLabel("")
        self._hotkey_result.setWordWrap(True)
        self._hotkey_result.setStyleSheet("font-size:11.5px;")
        lay.addWidget(self._hotkey_result)

        lay.addStretch(1)
        return page

    def _apply_hotkey(self) -> None:
        spec = self._hotkey_edit.spec()
        parts = [p for p in spec.split("+") if p]
        # 必须含 Ctrl 或 Alt（光 Shift/Win 会劫持正常打字或撞系统快捷键）+ 恰一个单字符主键
        has_strong_mod = any(p in ("Ctrl", "Alt") for p in parts)
        main = [p for p in parts if p not in _MOD_NAMES]
        if not (has_strong_mod and len(main) == 1 and len(main[0]) == 1):
            self._hotkey_result.setText("请用 Ctrl / Alt（可加 Shift）+ 一个字母或数字")
            return
        set_hotkey(spec)
        apply_cb = getattr(self._diagnostic_parent, "_apply_hotkey", None)  # app.py 注入的热重绑回调
        ok = apply_cb(spec) if callable(apply_cb) else None
        if ok:
            self._hotkey_result.setText(f"✓ 已生效：{spec}")
        elif ok is False:
            self._hotkey_result.setText(f"⚠ {spec} 注册失败（可能被占用），换一个再试")
        else:
            self._hotkey_result.setText(f"已保存：{spec}（重启后生效）")

    def _build_health_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("健康诊断")
        title.setStyleSheet("font-weight:700;font-size:15px;")
        head.addWidget(title, 1)
        self.rescan_btn = QPushButton("重新扫描索引")
        self.rescan_btn.setToolTip("后台重新扫描 PPT 索引；扫描期间仍可继续搜索已索引内容。")
        self.rescan_btn.setEnabled(callable(self._on_rescan))
        self.rescan_btn.clicked.connect(self._request_rescan)
        head.addWidget(self.rescan_btn)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.schedule_diagnostics_refresh)
        head.addWidget(refresh)
        copy = QPushButton("复制")
        copy.clicked.connect(self._copy_diagnostics)
        head.addWidget(copy)
        lay.addLayout(head)

        self.diagnostic_text = QPlainTextEdit()
        self.diagnostic_text.setReadOnly(True)
        self.diagnostic_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.diagnostic_text.setPlainText("诊断加载中…")
        lay.addWidget(self.diagnostic_text, 1)
        self.schedule_diagnostics_refresh()
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

    def _version_stat_text(self, n: int | None = None) -> str:
        if n is None:
            return "当前守护状态暂不可用"
        return f"当前已在守护 {n} 个你改过的文件。"

    def _toggle_auto(self, on: bool) -> None:
        autostart.set_enabled(on)

    def schedule_diagnostics_refresh(self, *, delay_ms: int = 0) -> None:
        if not self._ui_alive():
            return
        loading = "诊断加载中…"
        if self._diag_extra_lines:
            loading += "\n" + "\n".join(self._diag_extra_lines)
        self.diagnostic_text.setPlainText(loading)
        if self._diag_inflight_token is not None:
            return
        self._diag_refresh_token += 1
        token = self._diag_refresh_token
        QTimer.singleShot(delay_ms, lambda token=token: self._run_scheduled_diagnostics(token))

    def _run_scheduled_diagnostics(self, token: int) -> None:
        if not self._ui_alive():
            return
        if token != self._diag_refresh_token:
            return
        if self._diag_inflight_token is not None:
            return
        ui_lines = self._collect_ui_diagnostic_lines()
        task = BackgroundTask(
            lambda ui_lines=ui_lines: self._build_diagnostics_payload(ui_lines),
            "settings-diagnostics",
            None,
        )
        self._track_diag_task(task)
        self._diag_inflight_token = token
        task.done.connect(lambda result, token=token: self._on_diagnostics_ready(token, result))
        task.finished.connect(lambda task=task, token=token: self._forget_diag_task(task, token))
        task.start()

    def refresh_diagnostics(self) -> None:
        self.schedule_diagnostics_refresh()

    def _collect_ui_diagnostic_lines(self) -> list[str]:
        lines = [
            f"app: {APP_NAME}",
            f"python: {sys.version.split()[0]} ({platform.platform()})",
            f"data_dir: {data_dir()}",
            f"db_path: {db_path()}",
            f"cache_dir: {cache_dir()}",
            f"global_hotkey: {get_hotkey()}",
            f"autostart: {'on' if autostart.is_enabled() else 'off'}",
            f"PPTX_FINDER_ROOTS: {os.environ.get('PPTX_FINDER_ROOTS', '') or '(auto fixed drives)'}",
            f"PPTX_FINDER_DATA_DIR: {os.environ.get('PPTX_FINDER_DATA_DIR', '') or '(default)'}",
            f"exclude_dirs: {len(EXCLUDE_DIR_NAMES)} rules",
        ]
        try:
            parent = self._diagnostic_parent
            if parent is not None and hasattr(parent, "diagnostic_lines"):
                lines.extend(parent.diagnostic_lines())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"ui: unavailable ({type(exc).__name__}: {exc})")
        try:
            search_worker = getattr(self._diagnostic_parent, "_search_worker", None)
            if search_worker is not None and hasattr(search_worker, "diagnostic_lines"):
                lines.extend(search_worker.diagnostic_lines())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"search: unavailable ({type(exc).__name__}: {exc})")
        try:
            update_controller = getattr(self._diagnostic_parent, "_updater", None)
            if update_controller is not None and hasattr(update_controller, "diagnostic_lines"):
                lines.extend(update_controller.diagnostic_lines())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"update: unavailable ({type(exc).__name__}: {exc})")
        return lines

    def _version_doc_count_off_ui(self) -> int:
        if hasattr(self._mgr, "list_docs_details"):
            return len(self._mgr.list_docs_details())
        return len(self._mgr.list_docs())

    def _build_diagnostics_payload(self, ui_lines: list[str]) -> dict:
        lines = list(ui_lines)
        guarded_docs: int | None = None
        try:
            own = db.connect(db_path())
            try:
                db.init_db(own)
                s = db.stats(own)
            finally:
                own.close()
            lines.append(f"index: {s['file_count']} files / {s['page_count']} pages")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"index: unavailable ({type(exc).__name__}: {exc})")
        try:
            guarded_docs = self._version_doc_count_off_ui()
            lines.append(f"versions: {guarded_docs} guarded docs")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"versions: unavailable ({type(exc).__name__}: {exc})")

        for p in (data_dir(), cache_dir(), db_path()):
            lines.append(f"exists {Path(p).name}: {Path(p).exists()}")
        return {"lines": lines, "guarded_docs": guarded_docs}

    def _on_diagnostics_ready(self, token: int, result: object) -> None:
        if not self._ui_alive():
            return
        if token != self._diag_refresh_token:
            return
        if isinstance(result, dict):
            lines = list(result.get("lines") or [])
            guarded_docs = result.get("guarded_docs")
        else:
            lines = ["diagnostics: unavailable"]
            guarded_docs = None
        if self._diag_extra_lines:
            lines.extend(self._diag_extra_lines)
            self._diag_extra_lines.clear()
        self.diagnostic_text.setPlainText("\n".join(lines))
        self.stat.setText(self._version_stat_text(guarded_docs if isinstance(guarded_docs, int) else None))

    def _track_diag_task(self, task) -> None:
        self._diag_tasks.append(task)
        if self._parent_bg_tasks is not None and task not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(task)

    def _forget_diag_task(self, task, token: int | None = None) -> None:
        parent_tasks = self._parent_bg_tasks
        try:
            if _qt_is_valid(self):
                tasks = getattr(self, "_diag_tasks", [])
                if task in tasks:
                    tasks.remove(task)
                if token is not None and self._diag_inflight_token == token:
                    self._diag_inflight_token = None
        except RuntimeError:
            pass
        if parent_tasks is not None and task in parent_tasks:
            parent_tasks.remove(task)

    def _copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(self.diagnostic_text.toPlainText())

    def _request_rescan(self) -> None:
        if not callable(self._on_rescan):
            return
        try:
            accepted = self._on_rescan()
        except Exception as exc:  # noqa: BLE001 诊断动作不能拖垮设置页
            line = f"rescan: failed ({type(exc).__name__}: {exc})"
            if self._diag_inflight_token is not None:
                self._diag_extra_lines.append(line)
            self.diagnostic_text.appendPlainText(f"\n{line}")
            return
        if accepted is False:
            self._diag_extra_lines.append("rescan: already running")
        else:
            self._diag_extra_lines.append("rescan: requested in background")
        self.schedule_diagnostics_refresh()

    def _check_powerpoint(self) -> None:
        if not self._ui_alive():
            return
        if self._powerpoint_inflight:
            self.powerpoint_status.setText("正在检测…")
            return
        self._powerpoint_inflight = True
        self.powerpoint_btn.setEnabled(False)
        self.powerpoint_status.setText("正在检测…")
        task = BackgroundTask(_probe_powerpoint, "powerpoint-diagnostic", None)
        self._track_diag_task(task)
        task.done.connect(self._on_powerpoint_checked)
        task.finished.connect(lambda task=task: self._forget_diag_task(task))
        task.start()

    def _on_powerpoint_checked(self, result: object) -> None:
        if (
            not self._ui_alive()
            or not _qt_is_valid(getattr(self, "powerpoint_status", None))
            or not _qt_is_valid(getattr(self, "powerpoint_btn", None))
        ):
            return
        self._powerpoint_inflight = False
        self.powerpoint_btn.setEnabled(True)
        self.powerpoint_status.setText(str(result or "检测失败，请查看日志。"))

    def closeEvent(self, event):  # noqa: N802
        # Closing the settings panel should never block on a COM diagnostic.
        # QDialog close hides the window; running tasks are kept alive by
        # self._diag_tasks and clean themselves up via their finished signal.
        self._closing = True
        self._diag_refresh_token += 1
        self._diag_inflight_token = None
        self._powerpoint_inflight = False
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
