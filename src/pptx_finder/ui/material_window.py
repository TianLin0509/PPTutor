"""Native shape collection, clipboard reuse, and PPTX sharing."""
from pathlib import Path
import logging

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..materials import MaterialLibrary
from .bg_task import BackgroundTask

log = logging.getLogger(__name__)


class MaterialWindow(QWidget):
    def __init__(self, tok=None, parent=None, *, library=None):
        super().__init__(parent)
        self.setObjectName("materialWin")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle("素材库 · PPT Doctor")
        self.resize(880, 640)
        self.setMinimumSize(620, 440)
        self.library = library or MaterialLibrary()
        self._tasks = []
        self._busy = False
        self._items = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        title = QLabel("素材库")
        title.setObjectName("dashTitle")
        root.addWidget(title)
        hint = QLabel("在 PPT 中选中形状或图片并 Ctrl+C，然后收藏。取用时复制素材，回 PPT 粘贴。")
        hint.setWordWrap(True)
        root.addWidget(hint)
        capture = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("素材名称（可留空，稍后整理）")
        self.category_edit = QLineEdit()
        self.category_edit.setPlaceholderText("分类，例如：箭头 / Logo / 框")
        self.name_edit.setMaxLength(120)
        self.category_edit.setMaxLength(80)
        self.capture_btn = QPushButton("从剪贴板收藏")
        self.capture_btn.setObjectName("primary")
        self.capture_btn.clicked.connect(self._capture)
        capture.addWidget(self.name_edit, 2)
        capture.addWidget(self.category_edit, 2)
        capture.addWidget(self.capture_btn)
        root.addLayout(capture)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索素材名称或分类…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render)
        self.categories = QComboBox()
        self.categories.addItem("全部分类", "")
        self.categories.currentIndexChanged.connect(self._render)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.categories)
        root.addLayout(filters)
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        self.list_widget.setIconSize(QSize(140, 96))
        self.list_widget.setGridSize(QSize(175, 146))
        self.list_widget.setSpacing(6)
        self.list_widget.setWordWrap(True)
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.itemSelectionChanged.connect(self._sync)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._copy())
        root.addWidget(self.list_widget, 1)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("复制素材")
        self.copy_btn.clicked.connect(self._copy)
        self.edit_btn = QPushButton("编辑名称 / 分类")
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn = QPushButton("移出素材库")
        self.delete_btn.clicked.connect(self._delete)
        self.import_btn = QPushButton("导入 PPTX 素材包…")
        self.import_btn.clicked.connect(self._import)
        self.export_btn = QPushButton("导出选中素材…")
        self.export_btn.clicked.connect(self._export)
        for btn in (self.copy_btn, self.edit_btn, self.delete_btn):
            row.addWidget(btn)
        row.addStretch()
        root.addLayout(row)
        share = QHBoxLayout()
        share.addWidget(self.import_btn)
        share.addWidget(self.export_btn)
        share.addStretch()
        root.addLayout(share)
        self.status = QLabel("正在读取素材…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        root.addWidget(self.status)
        self._run(self.library.list, self._loaded, "读取素材库")

    def _selected(self):
        return [x.data(Qt.UserRole) for x in self.list_widget.selectedItems()]

    def _sync(self):
        n = len(self._selected())
        for btn in (self.copy_btn, self.edit_btn):
            btn.setEnabled(not self._busy and n == 1)
        for btn in (self.delete_btn, self.export_btn):
            btn.setEnabled(not self._busy and n > 0)
        for w in (self.capture_btn, self.import_btn, self.list_widget,
                  self.name_edit, self.category_edit, self.search, self.categories):
            w.setEnabled(not self._busy)

    def _render(self, *_):
        selected = set(self._selected())
        query = self.search.text().casefold().strip()
        category = self.categories.currentData() or ""
        self.list_widget.clear()
        for item in self._items:
            if category and item.category != category:
                continue
            if query not in f"{item.name} {item.category}".casefold():
                continue
            row = QListWidgetItem(QIcon(str(self.library.folder(item.id) / "preview.png")),
                                  item.name + (f"\n{item.category}" if item.category else ""))
            row.setData(Qt.UserRole, item.id)
            row.setToolTip(f"{item.name}\n{item.category or '未分类'}\n双击复制素材")
            self.list_widget.addItem(row)
            row.setSelected(item.id in selected)
        self._sync()

    def _loaded(self, items):
        self._items = items
        previous = self.categories.currentData()
        self.categories.blockSignals(True)
        self.categories.clear()
        self.categories.addItem("全部分类", "")
        for category in sorted({x.category for x in items if x.category}):
            self.categories.addItem(category, category)
        self.categories.setCurrentIndex(max(0, self.categories.findData(previous)))
        self.categories.blockSignals(False)
        self._render()
        self.status.setText(f"共 {len(items)} 个素材 · Ctrl / Shift 可多选后导出" if items else
                            "还没有素材。从 PPT 复制一个喜欢的箭头、图标或图片，开始收藏。")

    def _run(self, fn, done, label):
        if self._busy:
            return
        self._busy = True
        self._sync()
        self.status.setText(f"正在{label}…")

        def work():
            try:
                return {"value": fn()}
            except Exception as exc:
                log.exception("material operation failed: %s", label)
                return {"error": str(exc)}

        def complete(result):
            self._busy = False
            self._sync()
            if not isinstance(result, dict) or "error" in result:
                error = result.get("error") if isinstance(result, dict) else "任务未完成，请重试。"
                self.status.setText(f"{label}失败：{error}")
            else:
                done(result["value"])

        task = BackgroundTask(work, "materials", self)
        self._tasks.append(task)
        task.done.connect(complete)
        task.finished.connect(lambda: self._tasks.remove(task))
        task.start()

    def _refresh_after(self, message):
        def finished(items):
            self._loaded(items)
            self.status.setText(message)
        self._run(self.library.list, finished, "刷新素材库")

    def _capture(self):
        name, category = self.name_edit.text(), self.category_edit.text()
        self._run(lambda: self.library.capture(name, category),
                  lambda item: self._refresh_after(f"已收藏：{item.name}。原始 PPT 移动或删除也不会影响素材。"),
                  "收藏")

    def _copy(self):
        ids = self._selected()
        if len(ids) == 1:
            self._run(lambda: self.library.copy(ids[0]),
                      lambda _: self.status.setText("素材已复制。回到 PPT 的页面编辑区，按 Ctrl+V 粘贴。"),
                      "复制素材")

    def _edit(self):
        ids = self._selected()
        if len(ids) != 1:
            return
        item = next(x for x in self._items if x.id == ids[0])
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑素材信息")
        layout = QFormLayout(dialog)
        name, category = QLineEdit(item.name), QLineEdit(item.category)
        name.setMaxLength(120)
        category.setMaxLength(80)
        layout.addRow("名称", name)
        layout.addRow("分类", category)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            new_name, new_category = name.text(), category.text()
            self._run(lambda: self.library.rename(item.id, new_name, new_category),
                      lambda _: self._refresh_after("素材信息已更新。"), "保存素材信息")

    def _delete(self):
        ids = self._selected()
        if ids and QMessageBox.question(self, "移出素材库", f"将选中的 {len(ids)} 个素材移出素材库？") == QMessageBox.Yes:
            self._run(lambda: self.library.delete(ids),
                      lambda _: self._refresh_after("已移出素材库；文件保留在素材库的 .trash 目录。"), "移出素材")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 PPT Doctor 素材包", "", "PPTX 素材包 (*.pptx)")
        if path:
            self._run(lambda: self.library.import_pack(Path(path)),
                      lambda items: self._refresh_after(f"已导入 {len(items)} 个素材。"), "导入素材包")

    def _export(self):
        ids = self._selected()
        if not ids:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出素材包", "我的素材包.pptx", "PPTX 素材包 (*.pptx)")
        if path:
            if not path.lower().endswith(".pptx"):
                path += ".pptx"
            self._run(lambda: self.library.export_pack(ids, Path(path)),
                      lambda _: self.status.setText(f"已导出 {len(ids)} 个素材：{path}\n同事可直接用 PowerPoint 打开，或导入素材库。"),
                      "导出素材包")

    def closeEvent(self, event):
        if self._tasks:
            self.status.setText("操作仍在进行，完成后即可关闭窗口。")
            event.ignore()
            return
        super().closeEvent(event)
