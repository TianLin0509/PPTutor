"""Settings and diagnostics center."""
from __future__ import annotations

from collections.abc import Callable
import os
import platform
import re
import sys
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    enabled_index_exts,
    get_autostart,
    get_document_search_enabled,
    get_hotkey,
    get_smart_grouping_enabled,
    get_version_management_enabled,
    get_version_keep_per_doc,
    set_autostart,
    set_document_search_enabled,
    set_hotkey,
    set_smart_grouping_enabled,
    set_version_management_enabled,
    set_version_keep_per_doc,
)
from ..versioning import autostart
from . import bg_task
from .bg_task import BackgroundTask

_MOD_NAMES = ("Ctrl", "Alt", "Shift", "Win")


def _diag_number(line: str, key: str) -> float:
    m = re.search(rf"\b{re.escape(key)}=([0-9]+(?:\.[0-9]+)?)", line)
    return float(m.group(1)) if m else 0.0


def _diagnostic_summary_lines(lines: list[str]) -> list[str]:
    issues: list[str] = []
    for line in lines:
        if line.startswith("ui_loop:"):
            max_gap = _diag_number(line, "max_gap")
            slow_gaps = _diag_number(line, "slow_gaps")
            if max_gap >= 500 or slow_gaps > 0:
                issues.append(f"UI 主线程出现卡顿：max_gap={max_gap:.0f} ms，slow_gaps={slow_gaps:.0f}")
        elif line.startswith("background_tasks:"):
            waiting = _diag_number(line, "waiting")
            failed = _diag_number(line, "failed")
            if waiting > 0:
                issues.append(f"后台任务正在排队：waiting={waiting:.0f}")
            if failed > 0:
                issues.append(f"后台任务有失败记录：failed={failed:.0f}")
        elif line.startswith("renderer_ipc:"):
            crashes = _diag_number(line, "crashes")
            timeouts = _diag_number(line, "timeouts")
            if crashes > 0 or timeouts > 0:
                issues.append(f"渲染子进程异常：crashes={crashes:.0f}，timeouts={timeouts:.0f}")
        elif line.startswith("renderer_ipc_last_error:") and not line.rstrip().endswith("-"):
            issues.append("最近一次渲染错误：" + line.split(":", 1)[1].strip())
        elif line.startswith("version_reconcile:") and "error=-" not in line:
            issues.append("版本兜底巡检异常：" + line)
        elif (
            line.startswith("version_snapshots:")
            and _diag_number(line, "failures") > 0
            and "last_error=-" not in line
        ):
            issues.append("版本快照近期有失败记录：" + line)
        elif (
            line.startswith("vault_fsck:")
            and "versions=0" not in line
            and "ok=True" not in line
        ):
            issues.append("版本库完整性检查未通过：" + line)
        elif line.startswith("autostart:") and "preference=on actual=off" in line:
            issues.append("开机自启目标无效，需要重新写入")
        elif "unavailable" in line:
            issues.append("诊断项不可用：" + line)

    if not issues:
        return ["diagnostic_summary: 未发现明显异常"]
    return [f"diagnostic_summary: 发现 {len(issues)} 个需要关注的问题"] + [
        f"diagnostic_issue: {issue}" for issue in issues[:6]
    ]


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


class SettingsDialog(QDialog):
    def __init__(
        self,
        manager,
        parent=None,
        on_rescan: Callable[[], None] | None = None,
        on_feature_change: Callable[[str, bool], None] | None = None,
    ):
        super().__init__(parent)
        self._mgr = manager
        self._diagnostic_parent = parent
        self._closing_owner = parent
        self._on_rescan = on_rescan
        self._on_feature_change = on_feature_change
        self._diag_tasks: list[BackgroundTask] = []
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self._diag_refresh_token = 0
        self._diag_inflight_token: int | None = None
        self._diag_extra_lines: list[str] = []
        self._powerpoint_inflight = False
        self._vault_audit_inflight = False
        self._autostart_toggle_token = 0
        self._autostart_toggle_inflight = False
        self._retention_update_token = 0
        self._retention_update_inflight = False
        self._closing = False
        self.setObjectName("settingsWin")
        self.setWindowTitle("设置 · PPT Doctor")
        self.resize(700, 600)
        try:
            from ..config import get_theme
            from . import theme as _th
            _t = _th.tok(get_theme())
            self.setStyleSheet(f"QWidget#settingsWin {{ background: {_t['win']}; }}")
        except Exception:  # noqa: BLE001 样式失败不影响功能
            pass

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_guard_tab(), "功能")
        self.tabs.addTab(self._build_health_tab(), "健康诊断")
        self.tabs.addTab(self._build_powerpoint_tab(), "PowerPoint")
        lay.addWidget(self.tabs, 1)
        self._sync_feature_controls()

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

        title = QLabel("基础模式与高阶功能")
        title.setStyleSheet("font-weight:700;font-size:15px;")
        lay.addWidget(title)

        desc = QLabel(
            "基础模式始终开启：全盘 PPT 检索 + PPT 统计。下面三项默认关闭，"
            "只在你确实需要时才占用额外 CPU、磁盘和后台线程。"
        )
        desc.setWordWrap(True)
        lay.addWidget(desc)

        self.version_feature = QCheckBox("PPT 版本管理（自动留历史版本）")
        self.version_feature.setChecked(get_version_management_enabled())
        self.version_feature.setToolTip("开启后才启动全盘保存监听、离线补拍和版本库维护。")
        self.version_feature.toggled.connect(
            lambda on: self._toggle_feature("version_management", on)
        )
        lay.addWidget(self.version_feature)

        self.document_feature = QCheckBox("Word / PDF 内容搜索")
        self.document_feature.setChecked(get_document_search_enabled())
        self.document_feature.setToolTip("开启后会在低优先级后台补建 Word/PDF 内容索引。")
        self.document_feature.toggled.connect(
            lambda on: self._toggle_feature("document_search", on)
        )
        lay.addWidget(self.document_feature)

        self.grouping_feature = QCheckBox("相似稿 / 重复稿智能归组")
        self.grouping_feature.setChecked(get_smart_grouping_enabled())
        self.grouping_feature.setToolTip(
            "开启后计算完整文件哈希和 MinHash；大型资料库首次补建会更久。"
        )
        self.grouping_feature.toggled.connect(
            lambda on: self._toggle_feature("smart_grouping", on)
        )
        lay.addWidget(self.grouping_feature)

        version_title = QLabel("版本管理设置")
        version_title.setStyleSheet("font-weight:700;font-size:13px;margin-top:4px;")
        lay.addWidget(version_title)

        self.stat = QLabel("正在读取守护状态…")
        lay.addWidget(self.stat)

        self.auto = QCheckBox("开机自动启动")
        self.auto.setToolTip("建议开启，这样关机后重新登录也能继续守护保存事件。")
        self.auto.setChecked(get_autostart())
        self.auto.toggled.connect(self._toggle_auto)
        lay.addWidget(self.auto)

        retention_row = QHBoxLayout()
        retention_row.addWidget(QLabel("每份 PPT 最多保留版本："))
        self.retention = QComboBox()
        for label, value in (
            ("50 版", 50),
            ("100 版（推荐）", 100),
            ("200 版", 200),
            ("不限", 0),
        ):
            self.retention.addItem(label, value)
        current_limit = get_version_keep_per_doc()
        index = self.retention.findData(current_limit)
        if index < 0:
            self.retention.addItem(f"{current_limit} 版", current_limit)
            index = self.retention.count() - 1
        self.retention.setCurrentIndex(index)
        self.retention.currentIndexChanged.connect(self._apply_retention)
        retention_row.addWidget(self.retention, 1)
        lay.addLayout(retention_row)

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

    def _toggle_feature(self, key: str, enabled: bool) -> None:
        setters = {
            "version_management": set_version_management_enabled,
            "document_search": set_document_search_enabled,
            "smart_grouping": set_smart_grouping_enabled,
        }
        setters[key](bool(enabled))
        self._sync_feature_controls()
        callback = self._on_feature_change
        if callable(callback):
            callback(key, bool(enabled))

    def _sync_feature_controls(self) -> None:
        version_on = bool(getattr(self, "version_feature", None) and self.version_feature.isChecked())
        if hasattr(self, "retention"):
            self.retention.setEnabled(
                version_on and not self._retention_update_inflight
            )
        if hasattr(self, "stat") and not version_on:
            self.stat.setText("版本管理已关闭；已有历史数据不会删除。")

    def apply_runtime_feature_state(self, key: str, enabled: bool) -> None:
        """Reflect a backend rollback without emitting another settings change."""
        controls = {
            "version_management": getattr(self, "version_feature", None),
            "document_search": getattr(self, "document_feature", None),
            "smart_grouping": getattr(self, "grouping_feature", None),
        }
        control = controls.get(str(key))
        if control is None:
            return
        blocker = QSignalBlocker(control)
        try:
            control.setChecked(bool(enabled))
        finally:
            del blocker
        self._sync_feature_controls()

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
        self.vault_audit_btn = QPushButton("深度检查版本库")
        self.vault_audit_btn.setToolTip(
            "逐一校验版本清单与对象内容哈希；大版本库可能需要约一分钟。"
        )
        self.vault_audit_btn.clicked.connect(self._check_vault_repository)
        head.addWidget(self.vault_audit_btn)
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
        set_autostart(on)
        self._autostart_toggle_token += 1
        token = self._autostart_toggle_token
        self._autostart_toggle_inflight = True
        self.auto.setEnabled(False)
        task = BackgroundTask(
            lambda on=bool(on): _set_autostart_enabled_off_ui(on),
            "autostart-toggle",
            None,
        )
        self._track_diag_task(task)
        task.done.connect(
            lambda ok, token=token, on=bool(on):
                self._on_autostart_toggle_done(token, on, ok)
        )
        task.finished.connect(lambda task=task: self._forget_diag_task(task))
        task.start()

    def _on_autostart_toggle_done(self, token: int, on: bool, ok: object) -> None:
        if not self._ui_alive() or token != self._autostart_toggle_token:
            return
        self._autostart_toggle_inflight = False
        self.auto.setEnabled(True)
        state = "on" if on else "off"
        result = "applied" if ok else "failed; will retry at next startup"
        self._diag_extra_lines.append(f"autostart_toggle: {state} {result}")

    def _apply_retention(self, _index: int) -> None:
        limit = int(self.retention.currentData() or 0)
        set_version_keep_per_doc(limit)
        self._retention_update_token += 1
        token = self._retention_update_token
        self._retention_update_inflight = True
        self.retention.setEnabled(False)

        def apply_limit() -> bool:
            # ``self._mgr`` may be the lazy proxy used by the basic tier.
            # Resolving this attribute can open/migrate versions.db and must
            # happen inside the worker, not in the combo-box callback.
            fn = getattr(self._mgr, "set_retention_limit", None)
            if not callable(fn):
                return False
            fn(limit)
            return True

        task = BackgroundTask(
            apply_limit,
            "version-retention-update",
            None,
        )
        self._track_diag_task(task)
        task.done.connect(
            lambda _ok, token=token: self._on_retention_update_done(token)
        )
        task.finished.connect(lambda task=task: self._forget_diag_task(task))
        task.start()

    def _on_retention_update_done(self, token: int) -> None:
        if not self._ui_alive() or token != self._retention_update_token:
            return
        self._retention_update_inflight = False
        self._sync_feature_controls()

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
            "features: "
            f"version={'on' if get_version_management_enabled() else 'off'} "
            f"documents={'on' if get_document_search_enabled() else 'off'} "
            f"smart_grouping={'on' if get_smart_grouping_enabled() else 'off'}",
            f"autostart_preference: {'on' if get_autostart() else 'off'}",
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
        try:
            lines.extend(bg_task.diagnostic_lines())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"background_tasks: unavailable ({type(exc).__name__}: {exc})")
        return lines

    def _version_doc_count_off_ui(self) -> int:
        if hasattr(self._mgr, "list_docs_details"):
            return len(self._mgr.list_docs_details())
        return len(self._mgr.list_docs())

    def _build_diagnostics_payload(self, ui_lines: list[str]) -> dict:
        lines = list(ui_lines)
        guarded_docs: int | None = None
        try:
            actual, target = _read_autostart_status_off_ui()
            lines.append(
                "autostart: "
                f"preference={'on' if get_autostart() else 'off'} "
                f"actual={'on' if actual else 'off'}"
            )
            lines.append(f"autostart_target: {target or '(missing)'}")
        except Exception as exc:  # noqa: BLE001 diagnostics must stay available
            lines.append(f"autostart: unavailable ({type(exc).__name__}: {exc})")
        try:
            own = db.connect(db_path())
            try:
                db.init_db(own)
                s = db.stats(own, exts=enabled_index_exts())
            finally:
                own.close()
            lines.append(f"index: {s['file_count']} files / {s['page_count']} pages")
            dbp = Path(db_path())
            walp = Path(str(dbp) + "-wal")
            lines.append(
                "index_storage: "
                f"db={dbp.stat().st_size if dbp.exists() else 0} bytes "
                f"wal={walp.stat().st_size if walp.exists() else 0} bytes"
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"index: unavailable ({type(exc).__name__}: {exc})")
        lazy_state = getattr(self._mgr, "is_initialized", None)
        version_cold = (
            not get_version_management_enabled()
            and callable(lazy_state)
            and not lazy_state()
        )
        if version_cold:
            guarded_docs = 0
            lines.append("versions: disabled (database not opened)")
        else:
            try:
                guarded_docs = self._version_doc_count_off_ui()
                lines.append(f"versions: {guarded_docs} guarded docs")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"versions: unavailable ({type(exc).__name__}: {exc})")

        for p in (data_dir(), cache_dir(), db_path()):
            lines.append(f"exists {Path(p).name}: {Path(p).exists()}")
        lines = _diagnostic_summary_lines(lines) + lines
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
        if get_version_management_enabled():
            self.stat.setText(
                self._version_stat_text(guarded_docs if isinstance(guarded_docs, int) else None)
            )
        else:
            self.stat.setText("版本管理已关闭；已有历史数据不会删除。")

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

    def _check_vault_repository(self) -> None:
        if not self._ui_alive() or self._vault_audit_inflight:
            return
        self._vault_audit_inflight = True
        self.vault_audit_btn.setEnabled(False)
        self.diagnostic_text.appendPlainText(
            "\nvault_fsck_manual: running deep object verification..."
        )

        def audit_repository():
            # Attribute resolution initializes the lazy version backend. A
            # multi-gigabyte vault may need migration/locks, so even this
            # lookup belongs off the GUI thread.
            fn = getattr(self._mgr, "audit_repository", None)
            if not callable(fn):
                return {"unavailable": True}
            return fn(deep=True)

        task = BackgroundTask(audit_repository, "vault-fsck-deep", None)
        self._track_diag_task(task)
        task.done.connect(self._on_vault_audit_done)
        task.finished.connect(lambda task=task: self._finish_vault_audit(task))
        task.start()

    def _on_vault_audit_done(self, result: object) -> None:
        if not self._ui_alive() or not isinstance(result, dict):
            return
        if result.get("unavailable"):
            line = "vault_fsck_manual: unavailable"
            self._diag_extra_lines.append(line)
            self.diagnostic_text.appendPlainText("\n" + line)
            return
        line = (
            "vault_fsck_manual: "
            f"ok={bool(result.get('ok', False))} "
            f"versions={int(result.get('versions_checked', 0) or 0)} "
            f"objects={int(result.get('objects_hashed', 0) or 0)} "
            f"invalid={int(result.get('quarantined_versions', result.get('invalid_count', 0)) or 0)} "
            f"missing={int(result.get('missing_objects', 0) or 0)} "
            f"hash_errors={int(result.get('hash_errors', 0) or 0)}"
        )
        self._diag_extra_lines.append(line)
        self.diagnostic_text.appendPlainText("\n" + line)

    def _finish_vault_audit(self, task) -> None:
        self._forget_diag_task(task)
        self._vault_audit_inflight = False
        try:
            if _qt_is_valid(getattr(self, "vault_audit_btn", None)):
                self.vault_audit_btn.setEnabled(True)
        except RuntimeError:
            pass

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
        self._vault_audit_inflight = False
        self._autostart_toggle_token += 1
        self._autostart_toggle_inflight = False
        self._retention_update_token += 1
        self._retention_update_inflight = False
        super().closeEvent(event)


def _run_with_com_apartment(fn):
    """Run shell COM work safely from a QThread, never from the GUI thread."""
    pythoncom = None
    initialized = False
    try:
        import pythoncom as _pythoncom  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
    except Exception:  # noqa: BLE001 COM helper may still work in an existing apartment
        pass
    try:
        return fn()
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001 best-effort apartment cleanup
                pass


def _set_autostart_enabled_off_ui(on: bool) -> bool:
    return bool(_run_with_com_apartment(lambda: autostart.set_enabled(bool(on))))


def _read_autostart_status_off_ui() -> tuple[bool, str]:
    return _run_with_com_apartment(
        lambda: (bool(autostart.is_enabled()), str(autostart.link_target() or ""))
    )


def _probe_powerpoint() -> str:
    if os.name != "nt":
        return "当前不是 Windows，跳过 PowerPoint COM 检测。"
    pythoncom = None
    initialized = False
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        initialized = True
        # Resolve the registered COM class without starting PowerPoint.  The old
        # DispatchEx probe created a hidden automation process; that process could
        # later be reused by a normal file open and inherit preview-session display
        # characteristics.  GetActiveObject is read-only and never launches one.
        pythoncom.CLSIDFromProgID("PowerPoint.Application")
        version = ""
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            version = str(getattr(app, "Version", "") or "")
        except Exception:  # noqa: BLE001 PowerPoint simply is not running
            pass
        if version:
            return f"PowerPoint COM 已注册；当前运行版本 {version}。"
        return "PowerPoint COM 已注册；当前未运行。"
    except Exception as exc:  # noqa: BLE001
        return f"PowerPoint COM 不可用：{type(exc).__name__}: {exc}"
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
