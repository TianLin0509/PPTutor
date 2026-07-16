"""选中文件详情弹窗：两个 Tab —— 版本管理（文件信息 + 版本时间线，恢复/导出/改动简述）+
大纲（点击跳页）。无边框玻璃弹窗，可拖动。版本数据经 version_mgr.list_versions(path)（只读）。
"""
from __future__ import annotations

import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")  # 精确到秒
    except (OSError, OverflowError, ValueError):
        return ""


def _fmt_size(n: int) -> str:
    if not n or n <= 0:
        return ""
    f = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def _vget(v, key, default=""):
    """兼容 sqlite3.Row（无 .get）与 dict、字段可能缺失的安全取值。"""
    try:
        return v[key]
    except (KeyError, IndexError):
        return default


class DetailPanel(QWidget):
    restore_requested = Signal(str, str)  # path, version_id
    export_requested = Signal(str, str)
    preview_requested = Signal(str)
    page_requested = Signal(int)          # 大纲点击 → 跳到该页
    slim_requested = Signal(str)          # 单文件 PPT 瘦身体检

    def __init__(self, tok: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("detailPanel")
        self._tok = tok
        self._path = None
        self._drag_off = None
        self._file_actions_enabled = True
        self._version_nodes: list[QWidget] = []
        self._version_preview_labels: dict[str, QLabel] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_head())   # 玻璃标题栏（拖动 + 关闭）

        tabs = QTabWidget()
        tabs.setObjectName("detailTabs")
        tabs.addTab(self._build_version_tab(), "版本管理")
        tabs.addTab(self._build_outline_tab(), "大纲")
        root.addWidget(tabs, 1)

    # ---------- 结构 ----------
    def _build_head(self) -> QWidget:
        head = QWidget()
        head.setObjectName("detailHead")
        head.setFixedHeight(40)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(15, 0, 7, 0)
        hl.setSpacing(8)
        dot = QLabel("◆")
        dot.setObjectName("dtDot")
        title = QLabel("详情")
        title.setObjectName("dtTitle")
        hl.addWidget(dot)
        hl.addWidget(title)
        hl.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("dtClose")
        close_btn.setText("×")
        close_btn.setFixedSize(34, 32)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self.hide)
        hl.addWidget(close_btn)
        head.mousePressEvent = self._drag_press   # 标题栏拖动整窗
        head.mouseMoveEvent = self._drag_move
        return head

    def _scroll(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        c = QWidget()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(13, 12, 13, 16)
        scroll.setWidget(c)
        return scroll, lay

    def _build_version_tab(self) -> QWidget:
        scroll, lay = self._scroll()
        lay.setSpacing(8)
        self._meta_label = QLabel("← 选中左侧文件查看详情")  # 文件信息（紧凑，置顶）
        self._meta_label.setObjectName("detailMeta")
        self._meta_label.setWordWrap(True)
        lay.addWidget(self._meta_label)
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.addStretch(1)
        self._slim_btn = QPushButton("PPT 瘦身")
        self._slim_btn.setObjectName("detailAction")
        self._slim_btn.setToolTip("分析体积来源，并生成一个不覆盖源文件的瘦身副本")
        self._slim_btn.setEnabled(False)
        self._slim_btn.clicked.connect(lambda: self._path and self.slim_requested.emit(self._path))
        action_row.addWidget(self._slim_btn)
        lay.addLayout(action_row)
        lay.addSpacing(2)
        lay.addWidget(self._sec_title("版本时间线"))
        ver_c = QWidget()
        self._version_box = QVBoxLayout(ver_c)
        self._version_box.setContentsMargins(0, 0, 0, 0)
        self._version_box.setSpacing(7)
        lay.addWidget(ver_c)
        lay.addStretch(1)
        return scroll

    def _build_outline_tab(self) -> QWidget:
        scroll, lay = self._scroll()
        lay.setSpacing(2)
        ol_c = QWidget()
        self._outline_box = QVBoxLayout(ol_c)
        self._outline_box.setContentsMargins(0, 0, 0, 0)
        self._outline_box.setSpacing(0)
        lay.addWidget(ol_c)
        lay.addStretch(1)
        return scroll

    # ---------- 交互 ----------
    def _drag_press(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        else:
            self._drag_off = None

    def _drag_move(self, e):  # noqa: N802
        off = self._drag_off
        if off is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - off)

    def _sec_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("detailSecT")
        return lbl

    @staticmethod
    def _clear(box: QVBoxLayout) -> None:
        while box.count():
            it = box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    # ---------- 数据 ----------
    def clear_selection(self) -> None:
        self._path = None
        self._meta_label.setText("← 选中左侧文件查看详情")
        self._sync_slim_button()
        self._clear(self._version_box)
        self._version_nodes = []
        self._version_preview_labels = {}
        self._clear(self._outline_box)

    def set_file_actions_enabled(self, enabled: bool) -> None:
        self._file_actions_enabled = bool(enabled)
        for btn in self.findChildren(QPushButton):
            if btn.objectName() in {"verBtn", "verBtnPri", "verPreviewBtn", "outlineItem"}:
                btn.setEnabled(enabled)
        self._sync_slim_button()

    def _sync_slim_button(self) -> None:
        is_pptx = bool(self._path and str(self._path).lower().endswith(".pptx"))
        self._slim_btn.setEnabled(self._file_actions_enabled and is_pptx)

    def update_for(self, r, versions: list, *, versioning_enabled: bool = True) -> None:
        self._path = r.path
        self._sync_slim_button()
        parts = []
        sz = _fmt_size(r.size)
        if sz:
            parts.append(f"大小 {sz}")
        if r.page_count:
            parts.append(f"{r.page_count} 页")
        if versioning_enabled:
            parts.append(f"{len(versions)} 个版本" if versions else "暂无版本")
        else:
            parts.append("版本管理未开启")
        self._meta_label.setText("　·　".join(parts))

        self._clear(self._version_box)
        self._version_nodes = []
        self._version_preview_labels = {}
        if not versioning_enabled:
            tip = QLabel("版本管理当前关闭\n如需自动留存历史版本，可在设置 → 高阶功能中手动开启")
            tip.setObjectName("detailMuted")
            tip.setWordWrap(True)
            self._version_box.addWidget(tip)
            return
        if not versions:
            tip = QLabel("还没有历史版本\n用 PowerPoint 改一改、保存一下，就会自动留版本——全盘自动，无需任何设置")
            tip.setObjectName("detailMuted")
            tip.setWordWrap(True)
            self._version_box.addWidget(tip)
            return
        # versions 按 ts 降序（最新在前）。版本号有序：最老 v1.0 → 最新 v1.(N-1)
        total = len(versions)
        for i, v in enumerate(versions):
            seq = total - 1 - i
            is_latest = i == 0
            is_oldest = seq == 0
            label = ("最新版" if is_latest else "历史版") + f" v1.{seq}"
            node = self._version_node(v, label, is_latest, is_oldest)
            self._version_box.addWidget(node)
            self._version_nodes.append(node)

    def _version_node(self, v, label: str, is_latest: bool, is_oldest: bool) -> QWidget:
        w = QWidget()
        w.setObjectName("verNode")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(13, 7, 7, 7)
        lay.setSpacing(3)
        pc = _vget(v, "page_count", 0)
        vid = _vget(v, "version_id", "")
        # 第 1 行：版本标签 · 页数 [增减]  ……右侧 [恢复][导出]（与本行对齐，不独占一行）
        top = QHBoxLayout()
        top.setSpacing(6)
        title = QLabel(f"{label}　·　{pc} 页")
        title.setObjectName("verLatest" if is_latest else "verTitle")
        title.setToolTip("恢复前建议先看预览和改动摘要")
        top.addWidget(title)
        top.addStretch(1)
        br = QPushButton("恢复")
        br.setObjectName("verBtnPri" if is_latest else "verBtn")
        br.clicked.connect(lambda _=False, _v=vid: self.restore_requested.emit(self._path, _v))
        be = QPushButton("导出")
        be.setObjectName("verBtn")
        be.clicked.connect(lambda _=False, _v=vid: self.export_requested.emit(self._path, _v))
        top.addWidget(br)
        top.addWidget(be)
        lay.addLayout(top)
        # 第 2 行：时间（到秒）+ 改动简述
        sub = QHBoxLayout()
        sub.setSpacing(9)
        ts = QLabel(_fmt_ts(_vget(v, "ts", 0)))
        ts.setObjectName("verTs")
        sub.addWidget(ts)
        changed = (_vget(v, "changed", "") or "").strip() or ("首个版本" if is_oldest else "")
        sub.addStretch(1)
        lay.addLayout(sub)
        if changed:
            cl = QLabel("改动摘要：" + changed)
            cl.setObjectName("verChanged")
            cl.setWordWrap(True)
            cl.setToolTip(changed)
            lay.addWidget(cl)

        preview = QHBoxLayout()
        preview.setSpacing(8)
        thumb = QLabel("点击生成预览")
        thumb.setObjectName("verPreview")
        thumb.setFixedSize(150, 84)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setToolTip("历史版本第 1 页预览，用于判断是否恢复这一版")
        if vid:
            self._version_preview_labels[vid] = thumb
        preview.addWidget(thumb)
        bp = QPushButton("生成预览")
        bp.setObjectName("verPreviewBtn")
        bp.setEnabled(bool(vid))
        bp.setToolTip("渲染这一版的封面缩略图，不影响当前文件")
        bp.clicked.connect(lambda _=False, _v=vid: self.preview_requested.emit(_v))
        preview.addWidget(bp, 0, Qt.AlignTop)
        preview.addStretch(1)
        lay.addLayout(preview)

        thumb_path = str(_vget(v, "thumb_path", "") or "")
        if vid and thumb_path:
            self.set_version_preview(vid, thumb_path)
        return w

    def set_version_preview_loading(self, version_id: str) -> None:
        lbl = self._version_preview_labels.get(version_id)
        if lbl is None:
            return
        lbl.setPixmap(QPixmap())
        lbl.setText("预览生成中...")

    def set_version_preview(self, version_id: str, image_path: str | None) -> None:
        lbl = self._version_preview_labels.get(version_id)
        if lbl is None:
            return
        if not image_path:
            lbl.setPixmap(QPixmap())
            lbl.setText("预览失败")
            return
        pm = QPixmap(str(image_path))
        if pm.isNull():
            lbl.setPixmap(QPixmap())
            lbl.setText("预览失败")
            return
        lbl.setText("")
        lbl.setPixmap(pm.scaled(lbl.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lbl.setToolTip(str(image_path))

    def set_outline(self, titles: list) -> None:
        """titles: [(page_no, title)]，点击跳到该页预览。"""
        self._clear(self._outline_box)
        if not titles:
            tip = QLabel("（无可提取的页标题）")
            tip.setObjectName("detailMuted")
            self._outline_box.addWidget(tip)
            return
        for page_no, title in titles:
            b = QPushButton(f"{page_no}. {title}")
            b.setObjectName("outlineItem")
            b.clicked.connect(lambda _=False, p=page_no: self.page_requested.emit(p))
            self._outline_box.addWidget(b)
