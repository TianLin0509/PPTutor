"""版本管理窗口：受管文档 → 版本时间线 → 恢复 / 导出 / 找回 + 跨版本搜。

全部经 manager 访问（manager 内 RLock 串行，线程安全）；不直接碰 conn。
全局 QSS 自动套用主题。
"""
from __future__ import annotations

import datetime
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .bg_task import BackgroundTask
from .path_helpers import ensure_pptx_suffix


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _vget(v, key, default=""):
    try:
        return v[key]
    except (KeyError, IndexError, TypeError):
        return default


def _manager_supports(manager, name: str) -> bool:
    """Probe a lazy manager without triggering its ``__getattr__`` factory."""
    supports = getattr(type(manager), "supports", None)
    if callable(supports):
        try:
            return bool(supports(manager, name))
        except Exception:  # noqa: BLE001 capability hints are optional
            return False
    return callable(getattr(manager, name, None))


class VersionWindow(QWidget):
    _FILE_OP_BUSY_NOTICE = "已有文件操作正在进行，请稍候…"
    _DOC_POPULATE_BATCH = 160

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self._cur_doc = None  # (doc_id, path, status)
        self._docs_load_token = 0
        self._docs_inflight_token: int | None = None
        self._search_token = 0
        self._search_inflight_token: int | None = None
        self._search_inflight_query: str | None = None
        self._versions_load_token = 0
        self._versions_inflight_token: int | None = None
        self._versions_inflight_doc_id: str | None = None
        self._docs_tasks: list[BackgroundTask] = []
        self._search_tasks: list[BackgroundTask] = []
        self._version_tasks: list[BackgroundTask] = []
        self._version_preview_tasks: list[BackgroundTask] = []
        self._version_preview_inflight: set[str] = set()
        self._file_tasks: list[BackgroundTask] = []
        self._closing_owner = parent
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self._active_file_op = False
        self._reload_docs_after_file_op = False
        self._pending_versions_after_file_op: tuple[int, str, object] | None = None
        self._closing = False
        self._all_docs: list[dict] = []
        self._doc_filter_signature: tuple | None = None
        self._doc_filter_token = 0
        self._doc_population_token = 0
        self.setObjectName("versionWin")
        self.setWindowTitle("版本管理 · PPT 版 git")
        self.resize(940, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # 顶：跨版本内容搜索
        top = QHBoxLayout()
        top.addWidget(QLabel("跨版本搜内容："))
        self.search = QLineEdit()
        self.search.setPlaceholderText("找以前版本里出现过、现在可能已删的内容（某段话 / 某个词）…")
        self.search.returnPressed.connect(self._do_search)
        top.addWidget(self.search, 1)
        self.search_btn = QPushButton("搜索历史")
        self.search_btn.clicked.connect(self._do_search)
        top.addWidget(self.search_btn)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        # 左：受管文档
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("受管文档"))
        doc_filter_row = QHBoxLayout()
        self.doc_filter = QLineEdit()
        self.doc_filter.setPlaceholderText("按文件名或路径筛选…")
        self.doc_filter.textChanged.connect(self._schedule_doc_filter)
        doc_filter_row.addWidget(self.doc_filter, 1)
        self.doc_scope = QComboBox()
        self.doc_scope.addItem("全部", "all")
        self.doc_scope.addItem("现存", "active")
        self.doc_scope.addItem("已删除", "deleted")
        self.doc_scope.currentIndexChanged.connect(self._apply_doc_filter)
        doc_filter_row.addWidget(self.doc_scope)
        ll.addLayout(doc_filter_row)
        self.doc_list = QListWidget()
        self.doc_list.setUniformItemSizes(True)
        self.doc_list.currentItemChanged.connect(self._on_doc)
        ll.addWidget(self.doc_list, 1)
        split.addWidget(left)

        # 右：版本时间线 + 操作
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.right_title = QLabel("← 选择左侧文档查看版本历史")
        self.right_title.setStyleSheet("font-weight:700;")
        rl.addWidget(self.right_title)
        self.ver_list = QListWidget()
        self.ver_list.setUniformItemSizes(True)
        self.ver_list.currentItemChanged.connect(lambda *_args: self._on_version_selection_changed())
        rl.addWidget(self.ver_list, 1)
        preview_row = QHBoxLayout()
        self.version_preview = QLabel("选择版本后可生成预览")
        self.version_preview.setObjectName("versionPreview")
        self.version_preview.setFixedSize(180, 101)
        self.version_preview.setAlignment(Qt.AlignCenter)
        preview_row.addWidget(self.version_preview)
        preview_ops = QVBoxLayout()
        self.btn_preview = QPushButton("生成预览")
        self.btn_preview.clicked.connect(self._preview_selected_version)
        preview_ops.addWidget(self.btn_preview)
        preview_ops.addStretch(1)
        preview_row.addLayout(preview_ops, 1)
        rl.addLayout(preview_row)
        ops = QHBoxLayout()
        self.btn_restore = QPushButton("恢复此版本（覆盖当前）")
        self.btn_restore.setObjectName("primary")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_export = QPushButton("导出此版本…")
        self.btn_export.clicked.connect(self._export)
        self.btn_recover = QPushButton("找回文件")
        self.btn_recover.clicked.connect(self._recover)
        self.btn_recover.setVisible(False)
        for b in (self.btn_restore, self.btn_export, self.btn_recover):
            ops.addWidget(b)
        ops.addStretch(1)
        rl.addLayout(ops)
        split.addWidget(right)
        split.setSizes([300, 640])
        root.addWidget(split, 1)

        self._update_file_ops_state()
        self._apply_glass()
        self.schedule_reload_docs()

    def _apply_glass(self) -> None:
        """玻璃质感：给独立窗口套当前主题的纯色窗底（默认透明底在深色主题下显得「挫」）。"""
        try:
            from ..config import get_theme
            from . import theme as _th
            t = _th.tok(get_theme())
            self.setStyleSheet(f"QWidget#versionWin {{ background: {t['win']}; }}")
        except Exception:  # noqa: BLE001 样式失败不影响功能
            pass

    def _ui_alive(self) -> bool:
        if self._closing or not _qt_is_valid(self):
            return False
        owner = getattr(self, "_closing_owner", None)
        try:
            return owner is None or not getattr(owner, "_closing", False)
        except RuntimeError:
            return False

    def _track_bg_task(self, task, bucket: list) -> None:
        bucket.append(task)
        if self._parent_bg_tasks is not None and task not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(task)

    def _forget_bg_task(self, task, bucket: list) -> None:
        parent_tasks = self._parent_bg_tasks
        try:
            if task in bucket:
                bucket.remove(task)
        except RuntimeError:
            pass
        if parent_tasks is not None and task in parent_tasks:
            parent_tasks.remove(task)

    def closeEvent(self, event):  # noqa: N802
        self._closing = True
        self._docs_load_token += 1
        self._search_token += 1
        self._versions_load_token += 1
        self._doc_population_token += 1
        super().closeEvent(event)

    def _set_file_ops_enabled(self, enabled: bool) -> None:
        if not enabled:
            for btn in (self.btn_restore, self.btn_export, self.btn_recover, self.btn_preview):
                btn.setEnabled(False)
            return
        self._update_file_ops_state()

    def _set_navigation_enabled(self, enabled: bool) -> None:
        self.doc_list.setEnabled(enabled)
        self.ver_list.setEnabled(enabled)
        self.doc_filter.setEnabled(enabled)
        self.doc_scope.setEnabled(enabled)
        self.search.setEnabled(enabled)
        self.search_btn.setEnabled(enabled)

    def _update_file_ops_state(self) -> None:
        on = not self._active_file_op
        ctx = self._sel_version_context()
        has_version = ctx is not None
        version_id = ctx[0] if ctx else ""
        current_item = self.ver_list.currentItem()
        current_data = current_item.data(Qt.UserRole) if current_item else None
        healthy = not isinstance(current_data, dict) or str(
            current_data.get("health") or "ok"
        ) == "ok"
        recoverable = isinstance(self._cur_doc, tuple) and self._cur_doc[2] == "deleted"
        self.btn_restore.setEnabled(on and has_version and healthy)
        self.btn_export.setEnabled(on and has_version and healthy)
        self.btn_recover.setEnabled(on and recoverable)
        self.btn_preview.setEnabled(
            on
            and has_version
            and healthy
            and _manager_supports(self._mgr, "ensure_version_preview")
            and version_id not in self._version_preview_inflight
        )

    def _block_if_file_op_active(self) -> bool:
        if not self._active_file_op:
            return False
        self.right_title.setText(self._FILE_OP_BUSY_NOTICE)
        return True

    def _on_version_selection_changed(self) -> None:
        self._update_file_ops_state()
        self._show_selected_version_preview()

    def _clear_version_preview(self, text: str = "选择版本后可生成预览") -> None:
        self.version_preview.setPixmap(QPixmap())
        self.version_preview.setText(text)
        self.version_preview.setToolTip("")

    def _set_version_preview_image(self, image_path: str | None) -> bool:
        if not image_path:
            self._clear_version_preview("暂无预览")
            return False
        pm = QPixmap(str(image_path))
        if pm.isNull():
            self._clear_version_preview("预览不可用")
            return False
        self.version_preview.setText("")
        self.version_preview.setPixmap(
            pm.scaled(self.version_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self.version_preview.setToolTip(str(image_path))
        return True

    def _show_selected_version_preview(self) -> None:
        it = self.ver_list.currentItem()
        data = it.data(Qt.UserRole) if it is not None else None
        if not isinstance(data, dict) or not data.get("version_id"):
            self._clear_version_preview()
            return
        if str(data.get("health") or "ok") != "ok":
            reason = str(data.get("health_error") or "完整性检查未通过")
            self._clear_version_preview("恢复点已隔离")
            self.version_preview.setToolTip(reason)
            return
        thumb_path = str(data.get("thumb_path") or "")
        if not self._set_version_preview_image(thumb_path):
            self._clear_version_preview("点击生成预览")

    def _preview_selected_version(self) -> None:
        ctx = self._sel_version_context()
        if not ctx or not _manager_supports(self._mgr, "ensure_version_preview"):
            return
        if self._block_if_file_op_active():
            return
        version_id = ctx[0]
        if version_id in self._version_preview_inflight:
            return
        self._version_preview_inflight.add(version_id)
        self._clear_version_preview("预览生成中...")
        self._update_file_ops_state()
        task = BackgroundTask(lambda: self._mgr.ensure_version_preview(version_id), "version-preview", self)
        self._track_bg_task(task, self._version_preview_tasks)
        task.done.connect(lambda image_path, version_id=version_id: self._on_version_preview_ready(version_id, image_path))
        task.finished.connect(
            lambda task=task, version_id=version_id: self._finish_version_preview(task, version_id))
        task.start()

    def _finish_version_preview(self, task, version_id: str) -> None:
        self._version_preview_inflight.discard(version_id)
        self._forget_bg_task(task, self._version_preview_tasks)
        if self._ui_alive():
            self._update_file_ops_state()

    def _on_version_preview_ready(self, version_id: str, image_path: object) -> None:
        if not self._ui_alive():
            return
        image = str(image_path) if image_path else ""
        for i in range(self.ver_list.count()):
            item = self.ver_list.item(i)
            data = item.data(Qt.UserRole)
            if isinstance(data, dict) and data.get("version_id") == version_id:
                new_data = dict(data)
                new_data["thumb_path"] = image
                item.setData(Qt.UserRole, new_data)
                break
        if self._sel_version() == version_id:
            self._set_version_preview_image(image)

    def _defer_doc_reload_if_file_op_active(self) -> bool:
        if not self._active_file_op:
            return False
        self._reload_docs_after_file_op = True
        return True

    # ---------- 数据加载 ----------
    def _prepare_doc_reload(self) -> int:
        self._docs_load_token += 1
        if self._search_inflight_token is not None:
            self._search_token += 1
        if self._versions_inflight_token is not None:
            self._versions_load_token += 1
        token = self._docs_load_token
        self._cur_doc = None
        self._search_inflight_token = None
        self._search_inflight_query = None
        self._versions_inflight_token = None
        self._versions_inflight_doc_id = None
        self.doc_list.clear()
        self.ver_list.clear()
        self._clear_version_preview()
        self.right_title.setText("正在加载版本文档…")
        it = QListWidgetItem("正在加载版本文档…")
        it.setData(Qt.UserRole, None)
        self.doc_list.addItem(it)
        return token

    def schedule_reload_docs(self, *, delay_ms: int = 0) -> None:
        if self._defer_doc_reload_if_file_op_active():
            return
        token = self._prepare_doc_reload()
        QTimer.singleShot(delay_ms, lambda token=token: self._run_reload_docs(token))

    def _run_reload_docs(self, token: int) -> None:
        if not self._ui_alive() or token != self._docs_load_token:
            return
        if self._docs_inflight_token == token:
            return
        task = BackgroundTask(self._load_docs, "version-doc-list-load", self)
        self._docs_inflight_token = token
        self._track_bg_task(task, self._docs_tasks)
        task.done.connect(lambda result, token=token: self._on_docs_loaded(token, result))
        task.finished.connect(
            lambda task=task, token=token: self._finish_doc_list_load(task, token))
        task.start()

    def _finish_doc_list_load(self, task, token: int) -> None:
        self._forget_bg_task(task, self._docs_tasks)
        if self._docs_inflight_token == token:
            self._docs_inflight_token = None

    def _load_docs(self) -> list[dict]:
        if hasattr(self._mgr, "list_docs_details"):
            return list(self._mgr.list_docs_details())
        return list(self._mgr.list_docs())

    def reload_docs(self) -> None:
        if self._defer_doc_reload_if_file_op_active():
            return
        token = self._prepare_doc_reload()
        self._run_reload_docs(token)

    def _on_docs_loaded(self, token: int, result: object) -> None:
        if not self._ui_alive() or token != self._docs_load_token:
            return
        if self._active_file_op:
            self._reload_docs_after_file_op = True
            return
        docs = list(result or []) if isinstance(result, list) else []
        self._populate_docs(docs)

    def _populate_docs(self, docs: list[dict]) -> None:
        self._all_docs = list(docs)
        self._doc_filter_signature = None
        self._apply_doc_filter()

    def _schedule_doc_filter(self, *_args) -> None:
        """Coalesce rapid typing without keeping another always-live timer."""
        self._doc_filter_token += 1
        token = self._doc_filter_token
        QTimer.singleShot(180, lambda: self._apply_scheduled_doc_filter(token))

    def _apply_scheduled_doc_filter(self, token: int) -> None:
        if token != self._doc_filter_token or self._closing:
            return
        self._apply_doc_filter()

    def _apply_doc_filter(self, *_args) -> None:
        selected = None
        current = self.doc_list.currentItem()
        if current is not None:
            data = current.data(Qt.UserRole)
            if isinstance(data, tuple):
                selected = data[0]
        query = self.doc_filter.text().strip().casefold()
        scope = str(self.doc_scope.currentData() or "all")
        signature = (query, scope, len(self._all_docs), id(self._all_docs))
        if signature == self._doc_filter_signature:
            return
        self._doc_filter_signature = signature
        docs = [
            doc
            for doc in self._all_docs
            if (scope == "all" or str(_vget(doc, "status", "active") or "active") == scope)
            and (
                not query
                or query in str(_vget(doc, "path", "") or "").casefold()
                or query in os.path.basename(
                    str(_vget(doc, "path", "") or "")
                ).casefold()
            )
        ]
        self._doc_population_token += 1
        population_token = self._doc_population_token
        self.doc_list.clear()
        if not docs:
            self._cur_doc = None
            self.ver_list.clear()
            self._clear_version_preview("没有匹配的版本文档")
            it = QListWidgetItem("没有匹配的版本文档")
            it.setData(Qt.UserRole, None)
            self.doc_list.addItem(it)
            self.right_title.setText("还没有可恢复的版本历史")
            self._update_file_ops_state()
            return
        selected_row = next(
            (row for row, d in enumerate(docs) if selected and d["doc_id"] == selected),
            0,
        )
        self._append_doc_population_batch(population_token, docs, 0, selected_row)

    def _append_doc_population_batch(
        self,
        token: int,
        docs: list[dict],
        start: int,
        selected_row: int,
    ) -> None:
        """Build large document lists incrementally so scope changes never freeze Qt."""
        if token != self._doc_population_token or self._closing:
            return
        end = min(start + self._DOC_POPULATE_BATCH, len(docs))
        self.doc_list.setUpdatesEnabled(False)
        for d in docs[start:end]:
            name = os.path.basename(d["path"])
            label = ("🗑 " + name + "（已删·可找回）") if d["status"] == "deleted" else name
            it = QListWidgetItem(label)
            it.setToolTip(str(d["path"]))
            it.setData(Qt.UserRole, (d["doc_id"], d["path"], d["status"]))
            self.doc_list.addItem(it)
        self.doc_list.setUpdatesEnabled(True)
        if self.doc_list.currentRow() < 0 and selected_row < end:
            self.doc_list.setCurrentRow(selected_row)
        if end < len(docs):
            QTimer.singleShot(
                0,
                lambda token=token, docs=docs, start=end, selected_row=selected_row:
                    self._append_doc_population_batch(token, docs, start, selected_row),
            )

    def _on_doc(self, cur, prev=None) -> None:
        if not self._ui_alive():
            return
        if cur is None:
            return
        data = cur.data(Qt.UserRole)
        if not isinstance(data, tuple):
            return
        if self._block_if_file_op_active():
            return
        self._search_token += 1
        self._search_inflight_token = None
        self._search_inflight_query = None
        self._cur_doc = data
        did, path, status = data
        deleted = status == "deleted"
        self.right_title.setText(os.path.basename(path) + ("　[已删除，可找回]" if deleted else ""))
        self.btn_recover.setVisible(deleted)
        self._fill_versions(did)

    def _fill_versions(self, doc_id: str) -> None:
        if not self._ui_alive():
            return
        if (
            self._versions_inflight_token is not None
            and self._versions_inflight_token == self._versions_load_token
            and self._versions_inflight_doc_id == doc_id
        ):
            return
        self._versions_load_token += 1
        token = self._versions_load_token
        self.ver_list.clear()
        self._clear_version_preview("正在加载版本时间线...")
        it = QListWidgetItem("正在加载版本时间线…")
        it.setData(Qt.UserRole, None)
        self.ver_list.addItem(it)
        self._update_file_ops_state()

        def _work():
            if hasattr(self._mgr, "list_versions_by_doc_details"):
                return self._mgr.list_versions_by_doc_details(doc_id)
            return [
                {
                    "version_id": v["version_id"],
                    "ts": v["ts"],
                    "page_count": v["page_count"],
                    "changed": _vget(v, "changed", ""),
                    "thumb_path": _vget(v, "thumb_path", ""),
                }
                for v in self._mgr.list_versions_by_doc(doc_id)
            ]

        task = BackgroundTask(_work, "version-list-load", self)
        self._versions_inflight_token = token
        self._versions_inflight_doc_id = doc_id
        self._track_bg_task(task, self._version_tasks)
        task.done.connect(
            lambda result, token=token, doc_id=doc_id: self._on_versions_loaded(token, doc_id, result))
        task.finished.connect(
            lambda task=task, token=token, doc_id=doc_id: self._finish_version_list_load(task, token, doc_id))
        task.start()

    def _finish_version_list_load(self, task, token: int, doc_id: str) -> None:
        self._forget_bg_task(task, self._version_tasks)
        if self._versions_inflight_token == token and self._versions_inflight_doc_id == doc_id:
            self._versions_inflight_token = None
            self._versions_inflight_doc_id = None

    def _on_versions_loaded(self, token: int, doc_id: str, result: object) -> None:
        if not self._ui_alive() or token != self._versions_load_token:
            return
        if self._active_file_op:
            self._pending_versions_after_file_op = (token, doc_id, result)
            return
        self.ver_list.clear()
        rows = list(result or []) if isinstance(result, list) else []
        if not rows:
            it = QListWidgetItem("暂无版本记录")
            it.setData(Qt.UserRole, None)
            self.ver_list.addItem(it)
            self._clear_version_preview("暂无版本记录")
            self._update_file_ops_state()
            return
        # 按会话聚类（同 session_id 连续聚为一组；无 session_id 退化为按天），每组加分组头
        groups: list[tuple[str, list]] = []
        for v in rows:
            key = self._session_key(v)
            if groups and groups[-1][0] == key:
                groups[-1][1].append(v)
            else:
                groups.append((key, [v]))
        for _key, vs in groups:
            header = self._session_label(vs)
            for j, v in enumerate(vs):
                # 会话头作为该组首个版本项的前缀行：既呈现分组，又保持「每版一项、row 0 是版本」
                self.ver_list.addItem(self._make_version_item(v, doc_id, header if j == 0 else ""))
        self.ver_list.setCurrentRow(0)
        self._update_file_ops_state()

    def _session_key(self, v) -> str:
        sid = str(_vget(v, "session_id", "") or "").strip()
        if sid:
            return "s:" + sid
        ts = _vget(v, "ts", 0)
        try:
            return "d:" + datetime.datetime.fromtimestamp(float(ts or 0)).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return "d:?"

    def _session_label(self, vs: list) -> str:
        """会话分组头：日期 + 时间跨度 + 版本数（vs 为新→旧）。"""
        n = len(vs)
        try:
            d_new = datetime.datetime.fromtimestamp(float(_vget(vs[0], "ts", 0) or 0))
            d_old = datetime.datetime.fromtimestamp(float(_vget(vs[-1], "ts", 0) or 0))
        except (OSError, OverflowError, ValueError):
            return f"🗂 {n} 版"
        if d_new.date() != d_old.date():
            return f"🗂 {d_old.strftime('%m月%d日')} – {d_new.strftime('%m月%d日')}　·　{n} 版"
        if n > 1:
            return (f"🗂 {d_new.strftime('%m月%d日')}　"
                    f"{d_old.strftime('%H:%M')}–{d_new.strftime('%H:%M')}　·　{n} 版")
        return f"🗂 {d_new.strftime('%m月%d日 %H:%M')}　·　{n} 版"

    def _make_version_item(self, v, doc_id: str, header: str = "") -> QListWidgetItem:
        changed = str(_vget(v, "changed", "") or "").strip()
        health = str(_vget(v, "health", "ok") or "ok")
        body = f"{_fmt_ts(_vget(v, 'ts', 0))}　·　{_vget(v, 'page_count', 0)} 页"
        if changed:
            body = f"{body}\n  ✎ {changed}"
        if health != "ok":
            body = f"{body}\n  ⚠ 无效恢复点（已隔离）"
        label = f"{header}\n{body}" if header else body
        it = QListWidgetItem(label)
        if health != "ok":
            it.setToolTip(str(_vget(v, "health_error", "") or "完整性检查未通过"))
        doc_path = None
        if isinstance(self._cur_doc, tuple) and self._cur_doc[0] == doc_id:
            doc_path = self._cur_doc[1]
        it.setData(Qt.UserRole, {
            "version_id": _vget(v, "version_id", ""),
            "doc_path": doc_path,
            "changed": changed,
            "thumb_path": _vget(v, "thumb_path", ""),
            "health": health,
            "health_error": _vget(v, "health_error", ""),
        })
        return it

    def _sel_version(self) -> str | None:
        ctx = self._sel_version_context()
        return ctx[0] if ctx else None

    def _sel_version_context(self) -> tuple[str, str | None] | None:
        it = self.ver_list.currentItem()
        if not it:
            return None
        data = it.data(Qt.UserRole)
        if isinstance(data, dict):
            vid = data.get("version_id")
            if not vid:
                return None
            return str(vid), data.get("doc_path")
        return (data, None) if data else None

    # ---------- 操作 ----------
    def _doc_path_for_version(self, vid: str) -> str | None:
        """从版本 id 反查其所属文档的真实路径。

        跨版本搜索的命中可能属于**另一个**文档，绝不能用左侧当前选中文档(_cur_doc)的
        路径——否则会把 docB 的版本恢复/导出到 docA 的文件上（覆盖错文件，数据安全 bug）。
        """
        v = self._mgr.get_version(vid)
        if not v:
            return None
        doc = self._mgr.get_doc(v["doc_id"])
        return doc["path"] if doc else None

    def _restore(self) -> None:
        ctx = self._sel_version_context()
        if not ctx:
            return
        vid, path = ctx
        if self._block_if_file_op_active():
            return
        prev_title = self.right_title.text()
        self._active_file_op = True
        self._set_file_ops_enabled(False)
        self._set_navigation_enabled(False)
        self.right_title.setText("正在读取版本差异…")

        def prepare_restore():
            resolved_path = path or self._doc_path_for_version(vid)
            diff = None
            if resolved_path and _manager_supports(self._mgr, "describe_version_diff"):
                try:
                    diff = self._mgr.describe_version_diff(vid)
                except Exception:  # noqa: BLE001 diff is helpful but not required
                    diff = None
            return {"path": resolved_path, "diff": diff}

        task = BackgroundTask(prepare_restore, "version-restore-prepare", self)
        self._track_bg_task(task, self._file_tasks)
        task.done.connect(
            lambda payload, vid=vid, prev_title=prev_title:
            self._on_restore_prepared(vid, prev_title, payload)
        )
        task.finished.connect(
            lambda task=task: self._forget_bg_task(task, self._file_tasks)
        )
        task.start()

    def _on_restore_prepared(self, vid: str, prev_title: str, payload: object) -> None:
        if not self._ui_alive() or not self._active_file_op:
            return
        data = payload if isinstance(payload, dict) else {}
        path = str(data.get("path") or "")
        if not path:
            self._finish_restore_prepare(prev_title)
            QMessageBox.warning(self, "恢复", "找不到该版本对应的文档路径")
            return
        msg = f"用此版本恢复：\n{os.path.basename(path)}\n\n若当前文件存在，会先自动留一版，不会丢。"
        diff = data.get("diff")
        if isinstance(diff, dict):
            lines = [str(x).strip() for x in (diff.get("lines") or []) if str(x).strip()]
            if lines:
                msg = (
                    "这版的主要变化：\n"
                    + "\n".join(f"• {line}" for line in lines[:6])
                    + "\n\n"
                    + msg
                )
        if QMessageBox.question(self, "恢复", msg) != QMessageBox.Yes:
            self._finish_restore_prepare(prev_title)
            return

        # Hand the busy state directly to the actual restore task; pending
        # timeline refreshes remain queued across both phases.
        self._active_file_op = False
        self.right_title.setText(prev_title)
        self._run_file_op(
            "version-restore",
            lambda: self._mgr.restore_to(path, vid),
            "恢复",
            "正在恢复版本…",
            "已恢复到该版本",
            "恢复失败",
            lambda: self._on_doc(self.doc_list.currentItem()) if self.doc_list.currentItem() else None,
        )

    def _finish_restore_prepare(self, prev_title: str) -> None:
        reload_after = self._reload_docs_after_file_op
        self._reload_docs_after_file_op = False
        self._active_file_op = False
        self._set_navigation_enabled(True)
        if reload_after:
            self._pending_versions_after_file_op = None
            self.schedule_reload_docs()
        else:
            self._apply_pending_versions_after_file_op()
        self._set_file_ops_enabled(True)
        if self.right_title.text() in ("正在读取版本差异…", self._FILE_OP_BUSY_NOTICE):
            self.right_title.setText(prev_title)

    def _export(self) -> None:
        ctx = self._sel_version_context()
        if not ctx:
            return
        vid, path = ctx
        if self._block_if_file_op_active():
            return
        path = path or self._doc_path_for_version(vid)
        if not path:
            QMessageBox.warning(self, "导出", "找不到该版本对应的文档路径")
            return
        base = os.path.splitext(os.path.basename(path))[0]
        dest, _f = QFileDialog.getSaveFileName(self, "导出此版本", base + "_导出.pptx", "PowerPoint (*.pptx)")
        if dest:
            dest = ensure_pptx_suffix(dest)
            self._run_file_op(
                "version-export",
                lambda: self._mgr.export(path, vid, dest),
                "导出",
                "正在导出版本…",
                "已导出",
                "导出失败",
            )

    def _recover(self) -> None:
        if not self._cur_doc:
            return
        if self._block_if_file_op_active():
            return
        did, _path, _ = self._cur_doc
        self._run_file_op(
            "version-recover",
            lambda: self._mgr.recover(did),
            "找回",
            "正在从版本库重建文件…",
            "已从版本库重建出文件",
            "找回失败",
            self.schedule_reload_docs,
        )

    def _run_file_op(self, label: str, fn, title: str, busy_text: str,
                     ok_text: str, fail_text: str, on_ok=None) -> None:
        if self._block_if_file_op_active():
            return
        prev_title = self.right_title.text()
        self._active_file_op = True
        self._set_file_ops_enabled(False)
        self._set_navigation_enabled(False)
        self.right_title.setText(busy_text)
        task = BackgroundTask(fn, label, self)
        self._track_bg_task(task, self._file_tasks)

        def _done(result):
            if not self._ui_alive():
                return
            reload_after_file_op = self._reload_docs_after_file_op
            self._reload_docs_after_file_op = False
            self._active_file_op = False
            self._set_navigation_enabled(True)
            if reload_after_file_op:
                self._pending_versions_after_file_op = None
            else:
                self._apply_pending_versions_after_file_op()
            self._set_file_ops_enabled(True)
            ok = bool(result)
            if self.right_title.text() in (busy_text, self._FILE_OP_BUSY_NOTICE):
                self.right_title.setText(prev_title)
            QMessageBox.information(self, title, ok_text if ok else fail_text)
            if ok and on_ok is not None:
                on_ok()
            if reload_after_file_op and not self._callback_is_doc_reload(on_ok):
                self.schedule_reload_docs()

        task.done.connect(_done)
        task.finished.connect(
            lambda task=task: self._forget_bg_task(task, self._file_tasks))
        task.start()

    def _apply_pending_versions_after_file_op(self) -> None:
        pending = self._pending_versions_after_file_op
        self._pending_versions_after_file_op = None
        if pending is None:
            return
        token, doc_id, result = pending
        self._on_versions_loaded(token, doc_id, result)

    def _callback_is_doc_reload(self, callback) -> bool:
        return (
            getattr(callback, "__self__", None) is self
            and getattr(callback, "__name__", "") == "schedule_reload_docs"
        )

    def focus_doc(self, path: str) -> None:
        """外部跳转：选中匹配该路径的受管文档（列表若还在后台加载则稍后重试一次）。"""
        if not path:
            return
        target = os.path.abspath(str(path))

        def _try() -> bool:
            for i in range(self.doc_list.count()):
                it = self.doc_list.item(i)
                data = it.data(Qt.UserRole)
                if isinstance(data, tuple) and len(data) >= 2 and data[1]:
                    try:
                        if os.path.abspath(str(data[1])) == target:
                            self.doc_list.setCurrentRow(i)
                            return True
                    except (OSError, ValueError):
                        pass
            return False

        if not _try():
            QTimer.singleShot(450, _try)   # 文档列表可能还在后台加载
        self.raise_()
        self.activateWindow()

    def search_history(self, query: str) -> None:
        """外部跳转：在跨版本搜框里直接搜某词。"""
        if not query:
            return
        self.search.setText(str(query))
        self._do_search()
        self.raise_()
        self.activateWindow()

    def _do_search(self) -> None:
        if not self._ui_alive():
            return
        if self._block_if_file_op_active():
            return
        q = self.search.text().strip()
        if not q:
            self._search_token += 1
            self._search_inflight_token = None
            self._search_inflight_query = None
            self.ver_list.clear()
            self._update_file_ops_state()
            if isinstance(self._cur_doc, tuple):
                did, path, status = self._cur_doc
                deleted = status == "deleted"
                self.right_title.setText(os.path.basename(path) + ("　[已删除，可找回]" if deleted else ""))
                self.btn_recover.setVisible(deleted)
                self._fill_versions(did)
            else:
                self.right_title.setText("← 选择左侧文档查看版本历史")
            return
        if (
            self._search_inflight_token is not None
            and self._search_inflight_query == q
        ):
            self.right_title.setText(f"正在搜索历史「{q}」…")
            return
        self._search_token += 1
        token = self._search_token
        self.right_title.setText(f"正在搜索历史「{q}」…")
        self.ver_list.clear()
        it = QListWidgetItem("正在搜索历史版本…")
        it.setData(Qt.UserRole, None)
        self.ver_list.addItem(it)
        self._update_file_ops_state()

        def _work():
            if hasattr(self._mgr, "search_history_details"):
                return self._mgr.search_history_details(q, limit=200)
            hits = list(self._mgr.search_history(q))
            return {"query": q, "total": len(hits), "rows": [
                {
                    "doc_path": h["doc_id"],
                    "ts": 0,
                    "page_no": h["page_no"],
                    "version_id": h["version_id"],
                }
                for h in hits[:200]
            ]}

        task = BackgroundTask(_work, "version-history-search", self)
        self._search_inflight_token = token
        self._search_inflight_query = q
        self._track_bg_task(task, self._search_tasks)
        task.done.connect(
            lambda result, token=token, query=q: self._on_history_search_done(token, query, result))
        task.finished.connect(
            lambda task=task, token=token, query=q: self._finish_history_search(task, token, query))
        task.start()

    def _finish_history_search(self, task, token: int, query: str) -> None:
        self._forget_bg_task(task, self._search_tasks)
        if self._search_inflight_token == token and self._search_inflight_query == query:
            self._search_inflight_token = None
            self._search_inflight_query = None

    def _on_history_search_done(self, token: int, query: str, result: object) -> None:
        if not self._ui_alive() or token != self._search_token:
            return
        if self._active_file_op:
            return
        self.ver_list.clear()
        self._update_file_ops_state()
        if not isinstance(result, dict):
            self.right_title.setText(f"跨版本搜「{query}」失败，请稍后重试")
            return
        rows = list(result.get("rows") or [])
        total = int(result.get("total") or 0)
        self.right_title.setText(f"跨版本搜「{query}」：命中 {total} 处历史版本")
        if not rows:
            it = QListWidgetItem("没有命中历史版本")
            it.setData(Qt.UserRole, None)
            self.ver_list.addItem(it)
            self._update_file_ops_state()
            return
        for row in rows:
            ts = _fmt_ts(float(row.get("ts") or 0))
            name = row.get("name") or os.path.basename(str(row.get("doc_path") or "")) or str(row.get("doc_id") or "")
            health = str(row.get("health") or "ok")
            label = f"{name}　·　{ts}　·　第 {row.get('page_no', '')} 页"
            if health != "ok":
                label += "　⚠ 已隔离"
            it = QListWidgetItem(label)
            if health != "ok":
                it.setToolTip(str(row.get("health_error") or "完整性检查未通过"))
            it.setData(Qt.UserRole, {
                "version_id": row.get("version_id"),
                "doc_path": row.get("doc_path"),
                "health": health,
                "health_error": row.get("health_error") or "",
            })
            self.ver_list.addItem(it)
        self.ver_list.setCurrentRow(0)
        self._update_file_ops_state()
