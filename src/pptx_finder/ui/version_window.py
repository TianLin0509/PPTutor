"""版本管理窗口：受管文档 → 版本时间线 → 恢复 / 导出 / 找回 + 跨版本搜。

全部经 manager 访问（manager 内 RLock 串行，线程安全）；不直接碰 conn。
全局 QSS 自动套用主题。
"""
from __future__ import annotations

import datetime
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
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


class VersionWindow(QWidget):
    _FILE_OP_BUSY_NOTICE = "已有文件操作正在进行，请稍候…"

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
        self._file_tasks: list[BackgroundTask] = []
        self._closing_owner = parent
        self._parent_bg_tasks = getattr(parent, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self._active_file_op = False
        self._reload_docs_after_file_op = False
        self._pending_versions_after_file_op: tuple[int, str, object] | None = None
        self._closing = False
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
        self.doc_list = QListWidget()
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
        self.ver_list.currentItemChanged.connect(lambda *_args: self._update_file_ops_state())
        rl.addWidget(self.ver_list, 1)
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
        self.schedule_reload_docs()

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
        super().closeEvent(event)

    def _set_file_ops_enabled(self, enabled: bool) -> None:
        if not enabled:
            for btn in (self.btn_restore, self.btn_export, self.btn_recover):
                btn.setEnabled(False)
            return
        self._update_file_ops_state()

    def _set_navigation_enabled(self, enabled: bool) -> None:
        self.doc_list.setEnabled(enabled)
        self.ver_list.setEnabled(enabled)
        self.search.setEnabled(enabled)
        self.search_btn.setEnabled(enabled)

    def _update_file_ops_state(self) -> None:
        on = not self._active_file_op
        has_version = self._sel_version_context() is not None
        recoverable = isinstance(self._cur_doc, tuple) and self._cur_doc[2] == "deleted"
        self.btn_restore.setEnabled(on and has_version)
        self.btn_export.setEnabled(on and has_version)
        self.btn_recover.setEnabled(on and recoverable)

    def _block_if_file_op_active(self) -> bool:
        if not self._active_file_op:
            return False
        self.right_title.setText(self._FILE_OP_BUSY_NOTICE)
        return True

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
        self.doc_list.clear()
        if not docs:
            it = QListWidgetItem("暂无版本文档")
            it.setData(Qt.UserRole, None)
            self.doc_list.addItem(it)
            self.right_title.setText("还没有可恢复的版本历史")
            return
        for d in docs:
            name = os.path.basename(d["path"])
            label = ("🗑 " + name + "（已删·可找回）") if d["status"] == "deleted" else name
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, (d["doc_id"], d["path"], d["status"]))
            self.doc_list.addItem(it)
        self.doc_list.setCurrentRow(0)

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
            self._update_file_ops_state()
            return
        for v in rows:
            it = QListWidgetItem(f"{_fmt_ts(v['ts'])}　·　{v['page_count']} 页")
            doc_path = None
            if isinstance(self._cur_doc, tuple) and self._cur_doc[0] == doc_id:
                doc_path = self._cur_doc[1]
            it.setData(Qt.UserRole, {"version_id": v["version_id"], "doc_path": doc_path})
            self.ver_list.addItem(it)
        self.ver_list.setCurrentRow(0)
        self._update_file_ops_state()

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
        path = path or self._doc_path_for_version(vid)
        if not path:
            QMessageBox.warning(self, "恢复", "找不到该版本对应的文档路径")
            return
        if QMessageBox.question(self, "恢复", f"用此版本恢复：\n{os.path.basename(path)}\n（若当前文件存在，会先自动留一版，不会丢）") != QMessageBox.Yes:
            return
        self._run_file_op(
            "version-restore",
            lambda: self._mgr.restore_to(path, vid),
            "恢复",
            "正在恢复版本…",
            "已恢复到该版本",
            "恢复失败",
            lambda: self._on_doc(self.doc_list.currentItem()) if self.doc_list.currentItem() else None,
        )

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
            it = QListWidgetItem(f"{name}　·　{ts}　·　第 {row.get('page_no', '')} 页")
            it.setData(Qt.UserRole, {
                "version_id": row.get("version_id"),
                "doc_path": row.get("doc_path"),
            })
            self.ver_list.addItem(it)
        self.ver_list.setCurrentRow(0)
        self._update_file_ops_state()
