"""版本管理窗口：受管文档 → 版本时间线 → 恢复 / 导出 / 找回 + 跨版本搜。

全部经 manager 访问（manager 内 RLock 串行，线程安全）；不直接碰 conn。
全局 QSS 自动套用主题。
"""
from __future__ import annotations

import datetime
import os

from PySide6.QtCore import Qt
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


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


class VersionWindow(QWidget):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self._cur_doc = None  # (doc_id, path, status)
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
        sb = QPushButton("搜索历史")
        sb.clicked.connect(self._do_search)
        top.addWidget(sb)
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

        self.reload_docs()

    # ---------- 数据加载 ----------
    def reload_docs(self) -> None:
        self.doc_list.clear()
        for d in self._mgr.list_docs():
            name = os.path.basename(d["path"])
            label = ("🗑 " + name + "（已删·可找回）") if d["status"] == "deleted" else name
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, (d["doc_id"], d["path"], d["status"]))
            self.doc_list.addItem(it)

    def _on_doc(self, cur, prev=None) -> None:
        if cur is None:
            return
        data = cur.data(Qt.UserRole)
        if not isinstance(data, tuple):
            return
        self._cur_doc = data
        did, path, status = data
        deleted = status == "deleted"
        self.right_title.setText(os.path.basename(path) + ("　[已删除，可找回]" if deleted else ""))
        self.btn_recover.setVisible(deleted)
        self._fill_versions(did)

    def _fill_versions(self, doc_id: str) -> None:
        self.ver_list.clear()
        for v in self._mgr.list_versions_by_doc(doc_id):
            it = QListWidgetItem(f"{_fmt_ts(v['ts'])}　·　{v['page_count']} 页")
            it.setData(Qt.UserRole, v["version_id"])
            self.ver_list.addItem(it)

    def _sel_version(self) -> str | None:
        it = self.ver_list.currentItem()
        return it.data(Qt.UserRole) if it else None

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
        vid = self._sel_version()
        if not vid:
            return
        path = self._doc_path_for_version(vid)
        if not path:
            QMessageBox.warning(self, "恢复", "找不到该版本对应的文档路径")
            return
        if os.path.exists(path):
            if QMessageBox.question(self, "恢复", f"用此版本覆盖：\n{os.path.basename(path)}\n（当前内容会先自动留一版，不会丢）") != QMessageBox.Yes:
                return
        ok = self._mgr.restore_to(path, vid)
        QMessageBox.information(self, "恢复", "已恢复到该版本" if ok else "恢复失败")
        self._on_doc(self.doc_list.currentItem())

    def _export(self) -> None:
        vid = self._sel_version()
        if not vid:
            return
        path = self._doc_path_for_version(vid)
        if not path:
            QMessageBox.warning(self, "导出", "找不到该版本对应的文档路径")
            return
        base = os.path.splitext(os.path.basename(path))[0]
        dest, _f = QFileDialog.getSaveFileName(self, "导出此版本", base + "_导出.pptx", "PowerPoint (*.pptx)")
        if dest:
            ok = self._mgr.export(path, vid, dest)
            QMessageBox.information(self, "导出", "已导出" if ok else "导出失败")

    def _recover(self) -> None:
        if not self._cur_doc:
            return
        did, _path, _ = self._cur_doc
        ok = self._mgr.recover(did)
        QMessageBox.information(self, "找回", "已从版本库重建出文件" if ok else "找回失败")
        self.reload_docs()

    def _do_search(self) -> None:
        q = self.search.text().strip()
        if not q:
            return
        hits = self._mgr.search_history(q)
        self.right_title.setText(f"跨版本搜「{q}」：命中 {len(hits)} 处历史版本")
        self.ver_list.clear()
        for h in hits[:200]:
            doc = self._mgr.get_doc(h["doc_id"])
            v = self._mgr.get_version(h["version_id"])
            name = os.path.basename(doc["path"]) if doc else h["doc_id"]
            ts = _fmt_ts(v["ts"]) if v else ""
            it = QListWidgetItem(f"{name}　·　{ts}　·　第 {h['page_no']} 页")
            it.setData(Qt.UserRole, h["version_id"])
            self.ver_list.addItem(it)
