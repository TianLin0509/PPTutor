"""facet 筛选抽屉：时间 / 类型 / 页数 / 文件夹 多选叠加，每 chip 带数量。

不靠关键词也能缩小范围——选几个 chip（AND），主窗按当前结果过滤。
新搜索时 update_counts(keep=False) 重置选中。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

_DIMS = [("time", "修改时间"), ("type", "类型"), ("page", "页数"), ("folder", "文件夹")]


_MAX_BUCKETS = {"folder": 16}


class FacetPanel(QWidget):
    filters_changed = Signal(dict)  # {dim: set(buckets)}

    def __init__(self, tok: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("facetPanel")
        self._tok = tok
        self._filters: dict[str, set] = {}
        self._chip_btns: dict[tuple, QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("facetHeadBar")
        hr = QHBoxLayout(bar)
        hr.setContentsMargins(13, 9, 10, 9)
        head = QLabel("筛选")
        head.setObjectName("facetHead")
        hr.addWidget(head)
        hr.addStretch(1)
        self._clear_btn = QPushButton("清除")
        self._clear_btn.setObjectName("facetClear")
        self._clear_btn.clicked.connect(self._clear_all)
        hr.addWidget(self._clear_btn)
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self._cl = QVBoxLayout(body)
        self._cl.setContentsMargins(13, 8, 13, 14)
        self._cl.setSpacing(5)
        self._cl.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def update_counts(self, counts: dict, keep: bool = False) -> None:
        if not keep:
            self._filters = {}
        while self._cl.count() > 1:  # 保留末尾 stretch
            it = self._cl.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._chip_btns = {}
        for dim, label in _DIMS:
            buckets = counts.get(dim, [])
            limit = _MAX_BUCKETS.get(dim)
            if limit is not None and len(buckets) > limit:
                buckets = sorted(buckets, key=lambda item: (-item[1], str(item[0]).lower()))[:limit]
            if not buckets:
                continue
            t = QLabel(label)
            t.setObjectName("facetDim")
            self._cl.insertWidget(self._cl.count() - 1, t)
            for bucket, cnt in buckets:
                chip = QPushButton(f"{bucket}　{cnt}")
                chip.setObjectName("facetChip")
                chip.setCheckable(True)
                chip.setChecked(bucket in self._filters.get(dim, set()))
                chip.clicked.connect(
                    lambda checked, d=dim, b=bucket: self._toggle(d, b, checked))
                self._chip_btns[(dim, bucket)] = chip
                self._cl.insertWidget(self._cl.count() - 1, chip)

    def _toggle(self, dim: str, bucket: str, checked: bool) -> None:
        s = self._filters.setdefault(dim, set())
        if checked:
            s.add(bucket)
        else:
            s.discard(bucket)
        if not s:
            self._filters.pop(dim, None)
        self.filters_changed.emit({k: set(v) for k, v in self._filters.items()})

    def active_filters(self) -> dict:
        """当前选中条件的只读副本 {dim: set(buckets)}，供结果顶栏条件行渲染。"""
        return {k: set(v) for k, v in self._filters.items()}

    def remove_filter(self, dim: str, bucket: str) -> None:
        """取消选中某个桶并重发 filters_changed——与在面板里点掉 chip 走同一条状态机。"""
        s = self._filters.get(dim)
        if not s or bucket not in s:
            return
        s.discard(bucket)
        if not s:
            self._filters.pop(dim, None)
        btn = self._chip_btns.get((dim, bucket))
        if btn is not None:
            btn.setChecked(False)
        self.filters_changed.emit({k: set(v) for k, v in self._filters.items()})

    def _clear_all(self) -> None:
        self._filters = {}
        for b in self._chip_btns.values():
            b.setChecked(False)
        self.filters_changed.emit({})
