"""选中文件详情抽屉：版本时间线（恢复/导出）+ 大纲（点击跳页）+ 文件信息。

全盘 lazy 版本管理：无「纳管」概念——有版本就展示，没版本则提示「改存即自动留版」。
版本数据经 version_mgr.list_versions(path)（只读）。
"""
from __future__ import annotations

import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
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


class DetailPanel(QWidget):
    restore_requested = Signal(str, str)  # path, version_id
    export_requested = Signal(str, str)
    page_requested = Signal(int)          # 大纲点击 → 跳到该页

    def __init__(self, tok: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("detailPanel")
        self._tok = tok
        self._path = None
        self._version_nodes: list[QWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QLabel("详情")
        head.setObjectName("detailHead")
        root.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        self._cl = QVBoxLayout(content)
        self._cl.setContentsMargins(13, 12, 13, 16)
        self._cl.setSpacing(8)

        self._cl.addWidget(self._sec_title("📍 版本时间线"))
        ver_c = QWidget()
        self._version_box = QVBoxLayout(ver_c)
        self._version_box.setContentsMargins(0, 0, 0, 0)
        self._version_box.setSpacing(0)
        self._cl.addWidget(ver_c)

        self._cl.addSpacing(8)
        self._cl.addWidget(self._sec_title("📑 大纲"))
        ol_c = QWidget()
        self._outline_box = QVBoxLayout(ol_c)
        self._outline_box.setContentsMargins(0, 0, 0, 0)
        self._outline_box.setSpacing(0)
        self._cl.addWidget(ol_c)

        self._cl.addSpacing(8)
        self._cl.addWidget(self._sec_title("ℹ️ 文件信息"))
        self._meta_label = QLabel("← 选中左侧文件查看详情")
        self._meta_label.setObjectName("detailMeta")
        self._meta_label.setWordWrap(True)
        self._cl.addWidget(self._meta_label)
        self._cl.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

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

    def update_for(self, r, versions: list) -> None:
        self._path = r.path
        # —— 文件信息 ——
        parts = []
        sz = _fmt_size(r.size)
        if sz:
            parts.append(f"大小　{sz}")
        if r.page_count:
            parts.append(f"页数　{r.page_count} 页")
        parts.append("版本　" + (f"{len(versions)} 版" if versions else "暂无"))
        self._meta_label.setText("\n".join(parts))
        # —— 版本时间线 ——
        self._clear(self._version_box)
        self._version_nodes = []
        if not versions:
            tip = QLabel("还没有历史版本\n用 PowerPoint 改一改、保存一下，就会自动留版本——全盘自动，无需任何设置")
            tip.setObjectName("detailMuted")
            tip.setWordWrap(True)
            self._version_box.addWidget(tip)
            return
        prev_pc = None  # versions 按 ts 降序（最新在前）
        for i, v in enumerate(versions):
            node = self._version_node(v, i == 0, prev_pc)
            self._version_box.addWidget(node)
            self._version_nodes.append(node)
            prev_pc = v["page_count"]

    def _version_node(self, v: dict, is_latest: bool, newer_pc: int | None) -> QWidget:
        w = QWidget()
        w.setObjectName("verNode")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 7, 6, 7)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        title = QLabel(("最新版" if is_latest else "历史版") + f"　·　{v['page_count']} 页")
        title.setObjectName("verLatest" if is_latest else "verTitle")
        top.addWidget(title)
        # 与更新版本相比的页数增减
        if newer_pc is not None:
            d = newer_pc - v["page_count"]
            if d:
                delta = QLabel((f"+{d}" if d > 0 else str(d)) + " 页")
                delta.setObjectName("verUp" if d > 0 else "verDn")
                top.addWidget(delta)
        top.addStretch(1)
        lay.addLayout(top)
        ts = QLabel(_fmt_ts(v["ts"]))
        ts.setObjectName("verTs")
        lay.addWidget(ts)
        ops = QHBoxLayout()
        ops.setSpacing(6)
        br = QPushButton("恢复")
        br.setObjectName("verBtnPri" if is_latest else "verBtn")
        br.clicked.connect(lambda _=False, vid=v["version_id"]: self.restore_requested.emit(self._path, vid))
        be = QPushButton("导出")
        be.setObjectName("verBtn")
        be.clicked.connect(lambda _=False, vid=v["version_id"]: self.export_requested.emit(self._path, vid))
        ops.addWidget(br)
        ops.addWidget(be)
        ops.addStretch(1)
        lay.addLayout(ops)
        return w

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
