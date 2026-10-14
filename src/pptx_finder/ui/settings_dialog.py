"""Settings and diagnostics center."""
from __future__ import annotations

from collections.abc import Callable
import logging
import os
import platform
import re
import sys
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QFrame,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QStackedWidget,
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
    FONT_SCALE_MAX,
    FONT_SCALE_MIN,
    FONT_SCALE_STEP,
    FONT_SCALES,
    cache_dir,
    clamp_font_scale,
    data_dir,
    db_path,
    enabled_index_exts,
    get_autostart,
    get_document_search_enabled,
    get_font_family,
    get_font_scale,
    get_hotkey,
    get_index_roots,
    get_smart_grouping_enabled,
    get_vault_max_mb,
    get_version_management_enabled,
    get_version_keep_per_doc,
    get_version_vault_dir,
    sanitize_font_family,
    set_autostart,
    set_document_search_enabled,
    set_font_family,
    set_font_scale,
    set_hotkey,
    set_index_roots,
    set_smart_grouping_enabled,
    set_vault_max_mb,
    set_version_management_enabled,
    set_version_keep_per_doc,
    validate_index_root,
    validate_version_vault_dir,
)
from ..versioning import autostart
from . import bg_task
from .bg_task import BackgroundTask

_MOD_NAMES = ("Ctrl", "Alt", "Shift", "Win")
FONT_SCALE_CUSTOM_LABEL = "自定义…"


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


class _AutoNote(QLabel):
    """结果提示：没有文字时不占位置（空着的提示会在卡片里留一大块空白）。"""

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setVisible(bool(text))


class _NavTabs(QWidget):
    """设置窗口左侧分类 + 右侧页面（v2.0.3 起替代顶部页签）。

    对外保留 QTabWidget 的常用接口（addTab / count / tabText / currentIndex /
    setCurrentIndex / currentChanged），调用方和测试不用跟着改。
    """

    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("setNav")
        self.nav.setFixedWidth(172)
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.stack = QStackedWidget()
        self.stack.setObjectName("setStack")
        row.addWidget(self.nav)
        row.addWidget(self.stack, 1)
        self.nav.currentRowChanged.connect(self._on_row)

    def addTab(self, page: QWidget, text: str) -> int:  # noqa: N802
        self.stack.addWidget(page)
        QListWidgetItem(text, self.nav)
        if self.nav.currentRow() < 0:
            self.nav.setCurrentRow(0)
        return self.stack.count() - 1

    def _on_row(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            self.currentChanged.emit(row)

    def count(self) -> int:
        return self.stack.count()

    def tabText(self, index: int) -> str:  # noqa: N802
        item = self.nav.item(index)
        return item.text() if item is not None else ""

    def currentIndex(self) -> int:  # noqa: N802
        return self.stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        self.nav.setCurrentRow(index)

    def currentWidget(self) -> QWidget:  # noqa: N802
        return self.stack.currentWidget()

    def widget(self, index: int) -> QWidget:
        return self.stack.widget(index)


def _settings_qss(t: dict) -> str:
    """设置窗口自己的样式：左侧分类、分组卡片、每项一行（名称 + 说明在左，控件在右）。"""
    return f"""
QDialog#settingsWin {{ background: {t['win']}; }}
QListWidget#setNav {{ background: {t['rail']}; border: none; border-right: 1px solid {t['bd2']};
    padding: 14px 10px; outline: none; font-size: 14px; }}
QListWidget#setNav::item {{ height: 38px; padding-left: 12px; border-radius: 8px; color: {t['ink2']}; margin: 1px 0; }}
QListWidget#setNav::item:hover {{ background: {t['hover']}; }}
QListWidget#setNav::item:selected {{ background: {t['sel']}; color: {t['acc']}; font-weight: 700; }}
QStackedWidget#setStack, QWidget#setPage {{ background: {t['win']}; }}
QScrollArea#setScroll {{ background: transparent; border: none; }}
QLabel#setPageTitle {{ font-size: 20px; font-weight: 700; color: {t['ink1']}; }}
QLabel#setPageDesc {{ font-size: 13px; color: {t['ink3']}; }}
QLabel#setSection {{ font-size: 13px; font-weight: 700; color: {t['ink2']}; padding-top: 6px; }}
QFrame#setCard {{ background: {t['panel']}; border: 1px solid {t['bd']}; border-radius: 10px; }}
QFrame#setDivider {{ background: {t['bd2']}; border: none; }}
QLabel#setRowTitle, QCheckBox#setRowTitle {{ font-size: 14px; font-weight: 600; color: {t['ink1']}; }}
QLabel#setRowDesc, QLabel#setNote {{ font-size: 12.5px; color: {t['ink3']}; }}
QLabel#setWarn {{ font-size: 12.5px; color: #9a6a00; }}
"""


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
        self._index_roots_inflight = False
        self._autostart_toggle_token = 0
        self._autostart_toggle_inflight = False
        self._retention_update_token = 0
        self._retention_update_inflight = False
        self._vault_size_inflight = False
        self._vault_size_generation = 0
        self._closing = False
        self.setObjectName("settingsWin")
        self.setWindowTitle("设置 · PPT Doctor")
        self.resize(900, 640)
        self.setMinimumSize(760, 520)
        try:
            from ..config import get_theme
            from . import theme as _th
            self.setStyleSheet(_settings_qss(_th.tok(get_theme())))
        except Exception:  # noqa: BLE001 样式失败不影响功能
            pass

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # v2.0.3：顶部页签 → 左侧分类；原「功能」一页塞了开关、版本库、热键、字体、索引范围，
        # 内容超宽出横向滚动条、分组也乱，拆成独立分类，每项一行（名称 + 说明在左，控件在右）。
        self.tabs = _NavTabs()
        self.tabs.addTab(self._build_general_page(), "常规")
        self.tabs.addTab(self._build_features_page(), "功能")
        self.tabs.addTab(self._build_vault_page(), "版本库")
        self.tabs.addTab(self._build_roots_page(), "索引范围")
        self.tabs.addTab(self._build_components_tab(), "组件")
        self.tabs.addTab(self._build_health_tab(), "健康诊断")
        self.tabs.addTab(self._build_powerpoint_tab(), "PowerPoint")
        lay.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._on_settings_tab_changed)
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

    # ---------- 版式小件：页头 / 分组卡片 / 一行设置 ----------
    def _page(self, title: str, desc: str = "", *, scroll: bool = True):
        """返回 (外层控件, 内容布局)。内容只纵向滚动，永远不出横向滚动条。"""
        page = QWidget()
        page.setObjectName("setPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(12)
        head = QLabel(title)
        head.setObjectName("setPageTitle")
        lay.addWidget(head)
        if desc:
            sub = QLabel(desc)
            sub.setObjectName("setPageDesc")
            sub.setWordWrap(True)
            lay.addWidget(sub)
        lay.addSpacing(4)
        if not scroll:
            return page, lay
        area = QScrollArea()
        area.setObjectName("setScroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area, lay

    @staticmethod
    def _card(lay) -> QVBoxLayout:
        card = QFrame()
        card.setObjectName("setCard")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 4, 18, 4)
        inner.setSpacing(0)
        lay.addWidget(card)
        return inner

    @staticmethod
    def _row(card: QVBoxLayout, title, desc="", control=None, *, below=None) -> None:
        """一行设置。title 可以是文字或 QCheckBox（勾选框本身当标题）；below 放整行宽的控件。"""
        if card.count():
            line = QFrame()
            line.setObjectName("setDivider")
            line.setFixedHeight(1)
            card.addWidget(line)
        box = QVBoxLayout()
        box.setContentsMargins(0, 12, 0, 12)
        box.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(3)
        if isinstance(title, QWidget):
            title.setObjectName("setRowTitle")
            text.addWidget(title)
        else:
            label = QLabel(title)
            label.setObjectName("setRowTitle")
            text.addWidget(label)
        if isinstance(desc, QLabel):
            if not desc.objectName():
                desc.setObjectName("setRowDesc")
            desc.setWordWrap(True)
            text.addWidget(desc)
        elif desc:
            d = QLabel(desc)
            d.setObjectName("setRowDesc")
            d.setWordWrap(True)
            text.addWidget(d)
        top.addLayout(text, 1)
        if isinstance(control, QWidget):
            top.addWidget(control, 0, Qt.AlignVCenter)
        elif control is not None:
            top.addLayout(control)
        box.addLayout(top)
        items = below if isinstance(below, (list, tuple)) else ([below] if below is not None else [])
        for item in items:
            if isinstance(item, QWidget):
                box.addWidget(item)
            else:
                box.addLayout(item)
        card.addLayout(box)

    @staticmethod
    def _note(text: str = "", name: str = "setNote") -> QLabel:
        label = _AutoNote()
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setText(text)
        return label

    # ---------- 常规：开机自启 / 唤起热键 / 界面字体 ----------
    def _build_general_page(self) -> QWidget:
        outer, lay = self._page("常规", "启动方式、全局热键和界面字体。")
        card = self._card(lay)

        self.auto = QCheckBox("开机自动启动")
        self.auto.setToolTip("建议开启，这样关机后重新登录也能继续守护保存事件。")
        self.auto.setChecked(get_autostart())
        self.auto.toggled.connect(self._toggle_auto)
        self._row(card, self.auto, "登录 Windows 后在后台启动，版本守护和全局热键随时可用。")

        # 全局唤起热键（#2 可改：解决状态栏「热键被占用」无修复路径）
        hk_row = QHBoxLayout()
        hk_row.setSpacing(8)
        self._hotkey_edit = HotkeyEdit(get_hotkey())
        self._hotkey_edit.setFixedWidth(180)
        hk_row.addWidget(self._hotkey_edit)
        hk_apply = QPushButton("应用")
        hk_apply.clicked.connect(self._apply_hotkey)
        hk_row.addWidget(hk_apply)
        self._hotkey_result = self._note()
        self._row(card, "全局唤起热键",
                  "点输入框后按下新组合键（需含 Ctrl / Alt，再加一个字母或数字），点「应用」即时生效。",
                  hk_row, below=self._hotkey_result)

        # 界面字体（字族 + 字号档位）：点「应用」即时全 App 生效并写 ui.json
        font_row = QHBoxLayout()
        font_row.setSpacing(8)
        font_row.addWidget(QLabel("字族："))
        self._font_family = QFontComboBox()
        # 字体下拉默认按最长的字体名定宽，会把整张卡片撑出窗口
        self._font_family.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._font_family.setMinimumContentsLength(14)
        self._font_family.insertItem(0, "默认（内置字体）", "")
        # QFontComboBox 的模型不回显 insertItem 的文本，DisplayRole 要补写一次
        self._font_family.setItemData(0, "默认（内置字体）", Qt.DisplayRole)
        saved_family = get_font_family()
        idx = self._font_family.findText(saved_family) if saved_family else -1
        # 已存字族在本机不存在（被卸载）时回落到默认项
        self._font_family.setCurrentIndex(idx if idx > 0 else 0)
        font_row.addWidget(self._font_family, 1)
        size_row = QHBoxLayout()
        size_row.setSpacing(8)
        size_row.addWidget(QLabel("字号："))
        # 三个预设一键即得；预设不合口味时选「自定义」，右边的倍率框接管。
        # 档位值只在 config.FONT_SCALES 定义一次，这里只负责起中文名。
        self._font_scale = QComboBox()
        for name, value in zip(("小", "标准", "大"), FONT_SCALES):
            self._font_scale.addItem(f"{name}（{value * 100:g}%）", value)
        self._font_scale.addItem(FONT_SCALE_CUSTOM_LABEL, None)
        self._font_scale_custom = QDoubleSpinBox()
        self._font_scale_custom.setDecimals(2)
        self._font_scale_custom.setRange(FONT_SCALE_MIN, FONT_SCALE_MAX)
        self._font_scale_custom.setSingleStep(FONT_SCALE_STEP)
        self._font_scale_custom.setSuffix(" ×")
        self._font_scale_custom.setToolTip(
            f"自定义倍率，{FONT_SCALE_MIN:g}~{FONT_SCALE_MAX:g}。"
            "1.00 = 标准字号；再往两端走界面会开始挤或糊，所以在这里夹死。"
        )
        saved_scale = get_font_scale()
        self._font_scale_custom.setValue(saved_scale)
        idx = self._font_scale.findData(saved_scale)
        # 存的是自定义值（不等于任何预设）时，直接落在「自定义」项上回显原值
        self._font_scale.setCurrentIndex(
            idx if idx >= 0 else self._font_scale.count() - 1)
        self._font_scale.currentIndexChanged.connect(self._sync_font_scale_custom)
        self._sync_font_scale_custom()
        size_row.addWidget(self._font_scale)
        size_row.addWidget(self._font_scale_custom)
        size_row.addStretch(1)
        font_apply = QPushButton("应用")
        font_apply.clicked.connect(self._apply_font)
        size_row.addWidget(font_apply)
        self._font_result = self._note("部分卡片的固定字号样式不受影响。")
        self._row(card, "界面字体", "字族与字号对整个程序生效，点「应用」立即看到效果。",
                  below=[font_row, size_row, self._font_result])
        lay.addStretch(1)
        return outer

    # ---------- 功能：三项高阶功能开关 ----------
    def _build_features_page(self) -> QWidget:
        outer, lay = self._page(
            "功能",
            "基础模式始终开启：全盘 PPT 检索 + PPT 统计。下面三项默认关闭，"
            "只在你确实需要时才占用额外 CPU、磁盘和后台线程。")
        card = self._card(lay)
        self.version_feature = QCheckBox("PPT 版本管理（自动留历史版本）")
        self.version_feature.setChecked(get_version_management_enabled())
        self.version_feature.setToolTip("开启后才启动全盘保存监听、离线补拍和版本库维护。")
        self.version_feature.toggled.connect(
            lambda on: self._toggle_feature("version_management", on)
        )
        self._row(card, self.version_feature, "改存即自动留一版，随时回到旧版本；会启动全盘保存监听与版本库维护。")

        self.document_feature = QCheckBox("Word / PDF 内容搜索")
        self.document_feature.setChecked(get_document_search_enabled())
        self.document_feature.setToolTip("开启后会在低优先级后台补建 Word/PDF 内容索引。")
        self.document_feature.toggled.connect(
            lambda on: self._toggle_feature("document_search", on)
        )
        self._row(card, self.document_feature, "在低优先级后台补建 Word / PDF 的内容索引，搜索时一并命中。")

        # 「索引所有文件」的开关已经撤掉（2026-08-28）：全盘文件名盘点现在是常开能力，
        # 用不用由搜索框右边的范围选择器当场决定，不必先来设置里找一个开关、再等一轮重扫。
        self.grouping_feature = QCheckBox("相似稿 / 重复稿智能归组")
        self.grouping_feature.setChecked(get_smart_grouping_enabled())
        self.grouping_feature.setToolTip(
            "开启后计算完整文件哈希和 MinHash；大型资料库首次补建会更久。"
        )
        self.grouping_feature.toggled.connect(
            lambda on: self._toggle_feature("smart_grouping", on)
        )
        self._row(card, self.grouping_feature, "计算文件哈希与相似度，把同一份稿的多个副本归成一组；大资料库首次补建会更久。")

        lay.addStretch(1)
        return outer

    # ---------- 版本库：保留数 / 容量 / 位置 / 清理 ----------
    def _build_vault_page(self) -> QWidget:
        outer, lay = self._page("版本库", "PPT 历史版本存放在哪里、留多少。需要先在「功能」里开启版本管理。")
        self.stat = self._note("正在读取守护状态…", "setPageDesc")
        lay.addWidget(self.stat)
        card = self._card(lay)
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
        self.retention.setMinimumWidth(170)
        self._row(card, "每份 PPT 最多保留", "超出后自动清理最旧的版本，受保护的恢复点除外。", self.retention)

        self.vault_max = QComboBox()
        for label, value in (
            ("2 GB", 2048),
            ("5 GB（推荐）", 5120),
            ("10 GB", 10240),
            ("20 GB", 20480),
            ("不限", 0),
        ):
            self.vault_max.addItem(label, value)
        current_max = get_vault_max_mb()
        index = self.vault_max.findData(current_max)
        if index < 0:
            self.vault_max.addItem(f"{current_max} MB", current_max)
            index = self.vault_max.count() - 1
        self.vault_max.setCurrentIndex(index)
        self.vault_max.setToolTip(
            "立即检查容量并清理可回收旧版；受保护的恢复点仍超限时暂停新增留版。"
        )
        self.vault_max.currentIndexChanged.connect(self._apply_vault_max)
        self.vault_max.setMinimumWidth(170)
        self._vault_size_label = self._note("当前版本库占用：打开对话框时测量…", "setRowDesc")
        self._row(card, "版本库容量上限", self._vault_size_label, self.vault_max)

        vault_dir_row = QHBoxLayout()
        vault_dir_row.setSpacing(8)
        self.vault_dir_edit = QLineEdit(get_version_vault_dir())
        self.vault_dir_edit.setPlaceholderText(f"默认：{data_dir() / 'vault'}")
        vault_dir_row.addWidget(self.vault_dir_edit, 1)
        vault_browse = QPushButton("浏览…")
        vault_browse.clicked.connect(self._browse_vault_dir)
        vault_dir_row.addWidget(vault_browse)
        vault_reset = QPushButton("还原默认")
        vault_reset.clicked.connect(lambda: self.vault_dir_edit.setText(""))
        vault_dir_row.addWidget(vault_reset)
        vault_apply = QPushButton("应用")
        vault_apply.clicked.connect(self._apply_vault_dir)
        vault_dir_row.addWidget(vault_apply)
        self._vault_dir_result = self._note()
        self._row(card, "存储位置", "可以放到空间更大的磁盘；修改时可选择把已有版本一起迁移过去。",
                  below=[vault_dir_row, self._vault_dir_result])

        self._ghost_btn = QPushButton("清理…")
        self._ghost_btn.setToolTip("清理失效版本（源文件已删除的留底）")
        self._ghost_btn.clicked.connect(self._reap_ghost_docs)
        self._row(card, "清理失效版本", "源文件已经删除、只剩版本留底的记录，确认后可一并清掉，释放空间。",
                  self._ghost_btn)
        lay.addStretch(1)
        return outer

    # ---------- 索引范围 ----------
    def _build_roots_page(self) -> QWidget:
        outer, lay = self._page("索引范围")
        roots_desc = QLabel(
            "默认索引所有本地固定磁盘；下面添加的目录会与固定盘一起被索引（不是替换）。"
            "网络路径（如 \\\\server\\share\\dir）当前不可达也可保存，可达时自动纳入；"
            "网络路径上的内容变化最坏约一周后才随完整扫描被感知，重启应用可加速。"
        )
        roots_desc.setObjectName("setPageDesc")
        roots_desc.setWordWrap(True)
        lay.addWidget(roots_desc)
        # 环境变量旧语义是「限定索引范围」；一旦保存自定义根，它降级为追加项且
        # 本地固定盘并入索引——老用户无感知会索引量暴涨，必须显式提示
        env_roots_note = QLabel(
            "检测到 PPTX_FINDER_ROOTS 环境变量限定；保存自定义根后本地固定盘将一并纳入索引。"
        )
        env_roots_note.setWordWrap(True)
        env_roots_note.setObjectName("setWarn")
        env_roots_note.setVisible(bool(os.environ.get("PPTX_FINDER_ROOTS", "").strip()))
        lay.addWidget(env_roots_note)
        self.index_roots_list = QListWidget()
        self.index_roots_list.setMinimumHeight(140)
        for root in get_index_roots():
            self._append_index_root_item(root)
        # 已保存的根：关窗时与列表比对，判断是否有没点「保存」的改动
        self._index_roots_saved = tuple(self._index_root_items())
        self._close_after_roots_save = False
        card = self._card(lay)
        list_box = QVBoxLayout()
        list_box.setContentsMargins(0, 12, 0, 12)
        list_box.setSpacing(8)
        list_box.addWidget(self.index_roots_list)
        net_row = QHBoxLayout()
        self.index_root_edit = QLineEdit()
        self.index_root_edit.setPlaceholderText("网络路径，如 \\\\server\\share\\dir")
        self.index_root_edit.returnPressed.connect(self._add_index_root_network)
        net_row.addWidget(self.index_root_edit, 1)
        self._index_root_net_add = QPushButton("添加网络路径")
        self._index_root_net_add.clicked.connect(self._add_index_root_network)
        net_row.addWidget(self._index_root_net_add)
        list_box.addLayout(net_row)
        roots_btn_row = QHBoxLayout()
        self._index_root_local_add = QPushButton("添加本地目录…")
        self._index_root_local_add.clicked.connect(self._add_index_root_local)
        roots_btn_row.addWidget(self._index_root_local_add)
        self._index_root_remove = QPushButton("删除选中")
        self._index_root_remove.clicked.connect(self._remove_index_root)
        roots_btn_row.addWidget(self._index_root_remove)
        roots_btn_row.addStretch(1)
        self._index_roots_save = QPushButton("保存")
        self._index_roots_save.clicked.connect(self._apply_index_roots)
        roots_btn_row.addWidget(self._index_roots_save)
        list_box.addLayout(roots_btn_row)
        self._index_roots_result = self._note()
        list_box.addWidget(self._index_roots_result)
        card.addLayout(list_box)
        lay.addStretch(1)
        return outer

    def _browse_vault_dir(self) -> None:
        start = self.vault_dir_edit.text().strip() or str(data_dir() / "vault")
        d = QFileDialog.getExistingDirectory(self, "选择版本库存储位置", start)
        if d:
            self.vault_dir_edit.setText(d)

    def _apply_vault_dir(self) -> None:
        from ..versioning import vault as vault_mod

        raw = self.vault_dir_edit.text().strip()
        if raw:
            err = validate_version_vault_dir(raw)
            if err:
                self._vault_dir_result.setText(f"✗ {err}")
                return
        current = vault_mod.vault_dir()
        new_eff = Path(os.path.abspath(raw)) if raw else (data_dir() / "vault")
        same = os.path.normcase(os.path.abspath(str(new_eff))) == os.path.normcase(
            os.path.abspath(str(current))
        )
        if same:
            try:
                self._mgr.switch_vault_dir(new_eff, config_value=raw)
            except Exception as e:  # noqa: BLE001
                self._vault_dir_result.setText(f"✗ 保存失败：{e}")
                return
            self._vault_dir_result.setText("已是该位置，设置已保存。")
            return
        has_content = current.is_dir() and (
            (current / "versions.db").exists()
            or any(p.is_dir() and p.name != "_tmp" for p in current.iterdir())
        )
        note = "✓ 已立即切换到新版本库位置。"
        if has_content:
            box = QMessageBox(self)
            box.setWindowTitle("迁移版本库")
            box.setText(
                f"当前版本库 {current} 已有数据。\n\n"
                f"「迁移」：复制到 {new_eff}，逐文件校验并重连成功后再释放旧库；"
                "保存事件会自动排队。\n"
                f"「仅切换」：旧库保留原位，新位置立即开始工作。"
            )
            mig = box.addButton("迁移", QMessageBox.AcceptRole)
            switch = box.addButton("仅切换", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked is box.button(QMessageBox.Cancel):
                return
            if clicked is mig:
                prog = QProgressDialog("正在迁移版本库…", None, 0, 0, self)
                prog.setCancelButton(None)
                prog.setWindowModality(Qt.WindowModal)
                prog.setMinimumDuration(0)
                prog.show()
                QApplication.processEvents()
                try:
                    def _cb(done: int, total: int) -> None:
                        prog.setLabelText(f"正在迁移版本库… {done}/{total} 个文件")
                        QApplication.processEvents()

                    result = self._mgr.migrate_vault_dir(
                        new_eff,
                        _cb,
                        config_value=raw,
                    )
                except Exception as e:  # noqa: BLE001
                    prog.close()
                    self._vault_dir_result.setText(f"✗ 迁移失败：{e}")
                    return
                prog.close()
                retained = str(result.get("source_backup") or "")
                note = (
                    f"✓ 已迁移 {result['files']} 个文件"
                    f"（{result['bytes'] / 1048576:.0f} MB）并已立即切换。"
                )
                if retained:
                    note += f" 旧库回滚副本因占用未删除：{retained}"
            elif clicked is switch:
                try:
                    self._mgr.switch_vault_dir(new_eff, config_value=raw)
                except Exception as e:  # noqa: BLE001
                    self._vault_dir_result.setText(f"✗ 切换失败：{e}")
                    return
                note = f"✓ 已立即切换；旧版本库仍保留在 {current}。"
        else:
            try:
                self._mgr.switch_vault_dir(new_eff, config_value=raw)
            except Exception as e:  # noqa: BLE001
                self._vault_dir_result.setText(f"✗ 切换失败：{e}")
                return
        self._vault_dir_result.setText(note)

    def _reap_ghost_docs(self) -> None:
        import sqlite3

        try:
            dry = self._mgr.preview_ghost_cleanup()
            if not dry["ghost_docs"]:
                self._vault_dir_result.setText("没有失效版本（所有登记路径仍存在）。")
                return
            ret = QMessageBox.question(
                self,
                "清理失效版本",
                f"发现 {dry['ghost_docs']} 份源文件已删除的文档，共 {dry['ghost_versions']} 个版本。\n"
                f"清理将删除这些留底并回收对象池空间，不可恢复。继续？",
            )
            if ret != QMessageBox.Yes:
                return
            # The source may have reappeared while the confirmation was open;
            # the manager re-probes under the same locks used by snapshots.
            res = self._mgr.reap_ghost_docs_now()
            gc = res.get("gc") or {}
            mb = int(gc.get("bytes_reclaimed", 0) or 0) / 1048576
            self._vault_dir_result.setText(
                f"✓ 已清理 {res['ghost_docs']} 份文档 / {res['ghost_versions']} 个版本；"
                f"回收对象 {gc.get('objects_removed', 0)} 个、约 {mb:.0f} MB。"
            )
            self._refresh_vault_size()
        except sqlite3.OperationalError:
            # 与周维护并发时，第二连接 busy_timeout 8s 后抛 OperationalError——
            # 友好提示而不是让异常直接抛出 slot。
            self._vault_dir_result.setText("版本库正忙，请稍后重试。")

    def _apply_vault_max(self, _index: int) -> None:
        limit = int(self.vault_max.currentData() or 0)
        from .vault_storage_dialog import supports_storage, storage_summary
        if not supports_storage(self._mgr):
            set_vault_max_mb(limit)
            return
        self._vault_limit_inflight = True
        self._vault_size_generation += 1
        self._sync_feature_controls()
        self._vault_size_label.setText('正在按新上限检查容量…')
        def apply_limit():
            try:
                return {'ok': True, 'state': self._mgr.set_storage_limit(limit)}
            except Exception as exc:
                logging.getLogger(__name__).error('vault capacity update failed', exc_info=True)
                return {'ok': False, 'error': str(exc)}
        task = BackgroundTask(apply_limit, 'vault-capacity-update', None)
        self._track_diag_task(task)
        def ready(result):
            if not self._ui_alive():
                return
            if not isinstance(result, dict) or not result.get('ok'):
                self._vault_size_label.setText('容量检查未完成：' + str((result or {}).get('error', '任务未完成')))
            else:
                self._vault_size_label.setText(storage_summary(result['state']))
        def finished():
            self._forget_diag_task(task)
            self._vault_limit_inflight = False
            if self._ui_alive():
                self._sync_feature_controls()
        task.done.connect(ready)
        task.finished.connect(finished)
        task.start()

    def _refresh_vault_size(self) -> None:
        if self._vault_size_inflight:
            return
        self._vault_size_inflight = True
        from .vault_storage_dialog import supports_storage
        token = self._vault_size_generation
        def read_size():
            if supports_storage(self._mgr):
                return self._mgr.storage_status()
            return _vault_size_bytes_off_ui()
        task = BackgroundTask(read_size, "vault-size-measure", None)
        self._track_diag_task(task)
        task.done.connect(lambda result: self._on_vault_size_ready(result, token))
        task.finished.connect(lambda task=task: self._finish_vault_size(task))
        task.start()

    def _on_vault_size_ready(self, result: object, token=None) -> None:
        if token is not None and token != self._vault_size_generation:
            return
        if not self._ui_alive() or not _qt_is_valid(getattr(self, "_vault_size_label", None)):
            return
        if isinstance(result, dict):
            from .vault_storage_dialog import storage_summary
            self._vault_size_label.setText(storage_summary(result))
            return
        if not isinstance(result, int):
            self._vault_size_label.setText("当前版本库占用：暂不可用")
            return
        self._vault_size_label.setText(f"当前版本库占用：{_format_size_text(result)}")

    def _finish_vault_size(self, task) -> None:
        self._forget_diag_task(task)
        self._vault_size_inflight = False

    # ---------- 索引范围（自定义根） ----------
    def _append_index_root_item(self, root: str, warning: str = "") -> None:
        item = QListWidgetItem(root + (f"　⚠ {warning}" if warning else ""))
        item.setData(Qt.UserRole, root)  # 干净路径存 UserRole，警示文字不进持久化
        if warning:
            item.setForeground(QColor("#9a6a00"))
            item.setToolTip(warning)
        self.index_roots_list.addItem(item)

    def _index_root_items(self) -> list[str]:
        return [
            str(self.index_roots_list.item(i).data(Qt.UserRole) or "")
            for i in range(self.index_roots_list.count())
        ]

    def _add_index_root_local(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要加入索引的目录", "")
        if d:
            self._append_index_root_item(d)
            self._index_roots_result.setText("")

    def _add_index_root_network(self) -> None:
        raw = self.index_root_edit.text().strip()
        if not raw:
            return
        # 列表只是暂存区：形态与可达性都在「保存」时统一校验（探测网络盘慢，放后台）
        self._append_index_root_item(raw)
        self.index_root_edit.clear()
        self._index_roots_result.setText("")

    def _remove_index_root(self) -> None:
        for item in self.index_roots_list.selectedItems():
            self.index_roots_list.takeItem(self.index_roots_list.row(item))

    def _set_index_root_editing_enabled(self, enabled: bool) -> None:
        """校验在途期间锁定根列表编辑：在途添加的根会被校验完成后的列表重建静默覆盖。"""
        for w in (
            self.index_root_edit,
            self._index_root_net_add,
            self._index_root_local_add,
            self._index_root_remove,
        ):
            w.setEnabled(enabled)

    def _apply_index_roots(self) -> None:
        if self._index_roots_inflight:
            return
        roots = tuple(self._index_root_items())
        self._index_roots_inflight = True
        self._index_roots_save.setEnabled(False)
        self._set_index_root_editing_enabled(False)
        self._index_roots_result.setText("正在检测各路径可达性…")
        task = BackgroundTask(
            lambda roots=roots: [(root,) + validate_index_root(root) for root in roots],
            "index-roots-validate",
            None,
        )
        self._track_diag_task(task)
        task.done.connect(self._on_index_roots_validated)
        task.finished.connect(lambda task=task: self._finish_index_roots_validate(task))
        task.start()

    def _on_index_roots_validated(self, result: object) -> None:
        if not self._ui_alive() or not isinstance(result, list):
            return
        malformed = [(root, msg) for root, ok, _reach, msg in result if not ok]
        if malformed:
            self._index_roots_result.setText(
                "✗ 未保存：" + "；".join(f"{root}（{msg}）" for root, msg in malformed)
            )
            return
        set_index_roots([root for root, _ok, _reach, _msg in result])
        self._index_roots_saved = tuple(root for root, _ok, _reach, _msg in result)
        # 回写列表：不可达的根标警告样式，但允许保存（网络盘时通时断）
        self.index_roots_list.clear()
        warnings = 0
        for root, _ok, reachable, msg in result:
            if reachable:
                self._append_index_root_item(root)
            else:
                warnings += 1
                self._append_index_root_item(root, msg)
        note = "✓ 已保存，重启后与本地固定盘一起被索引。"
        if warnings:
            note += f" {warnings} 个路径当前不可达，其可用时会自动纳入。"
        self._index_roots_result.setText(note)
        if getattr(self, "_close_after_roots_save", False):
            # 关窗时选了「保存」：保存成功后再关
            self._close_after_roots_save = False
            QTimer.singleShot(0, self.close)

    def _finish_index_roots_validate(self, task) -> None:
        self._forget_diag_task(task)
        self._index_roots_inflight = False
        try:
            if _qt_is_valid(getattr(self, "_index_roots_save", None)):
                self._index_roots_save.setEnabled(True)
        except RuntimeError:
            pass
        try:
            if _qt_is_valid(getattr(self, "index_root_edit", None)):
                self._set_index_root_editing_enabled(True)
        except RuntimeError:
            pass

    def _index_roots_dirty(self) -> bool:
        saved = getattr(self, "_index_roots_saved", None)
        return saved is not None and tuple(self._index_root_items()) != saved

    def _confirm_close_with_unsaved_roots(self) -> bool:
        """有没点「保存」的索引目录改动时先问一句。返回 True = 可以关窗。"""
        if self._closing or not self.isVisible() or not self._index_roots_dirty():
            return True
        choice = self._ask_unsaved_index_roots()
        if choice == "discard":
            # 视为已处理：closeEvent 之后 QDialog 还会走一次 reject，不能再问第二遍
            self._index_roots_saved = tuple(self._index_root_items())
            return True
        if choice == "save":
            self._close_after_roots_save = True
            self._apply_index_roots()
        return False

    def _ask_unsaved_index_roots(self) -> str:
        """返回 save / discard / back。"""
        box = QMessageBox(QMessageBox.Question, "索引目录未保存",
                          "你添加或删除了索引目录，但还没有点「保存」。\n现在关闭，这些改动不会生效。",
                          QMessageBox.NoButton, self)
        save_btn = box.addButton("保存", QMessageBox.AcceptRole)
        discard_btn = box.addButton("放弃改动", QMessageBox.DestructiveRole)
        back_btn = box.addButton("返回", QMessageBox.RejectRole)
        box.setDefaultButton(save_btn)
        box.setEscapeButton(back_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is discard_btn:
            return "discard"
        return "back"

    def reject(self):  # noqa: D401 - Esc / 关闭按钮都走这里
        if not self._confirm_close_with_unsaved_roots():
            return
        super().reject()

    def _toggle_feature(self, key: str, enabled: bool) -> None:
        if key == 'version_management' and enabled:
            from .vault_storage_dialog import VaultStorageDialog, supports_storage
            if supports_storage(self._mgr):
                if VaultStorageDialog(self._mgr, self).exec() != QDialog.Accepted:
                    self.apply_runtime_feature_state(key, False)
                    return
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
        if hasattr(self, "vault_max"):
            self.vault_max.setEnabled(version_on and not getattr(self, '_vault_limit_inflight', False))
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

    def _selected_font_scale(self) -> float:
        """当前选中的倍率：预设取档位值，「自定义」取右边倍率框。"""
        preset = self._font_scale.currentData()
        raw = self._font_scale_custom.value() if preset is None else preset
        return clamp_font_scale(raw)

    def _sync_font_scale_custom(self, _index: int = 0) -> None:
        """只有选了「自定义」倍率框才可编辑；切回预设时同步显示该档位的数值。"""
        preset = self._font_scale.currentData()
        self._font_scale_custom.setEnabled(preset is None)
        if preset is not None:
            with QSignalBlocker(self._font_scale_custom):
                self._font_scale_custom.setValue(float(preset))

    def _apply_font(self) -> None:
        # QFontComboBox 可编辑（用户能手输任意文本）：入库前过 config 的同一道清洗，
        # 剥掉可注入 QSS 的字符；不存在的字体名允许保存（QSS 回退链兜底）
        family = (
            "" if self._font_family.currentIndex() == 0
            else sanitize_font_family(self._font_family.currentFont().family())
        )
        scale = self._selected_font_scale()
        set_font_family(family)
        set_font_scale(scale)
        apply_cb = getattr(self._diagnostic_parent, "_apply_font", None)  # 主窗口注入的热应用回调
        if callable(apply_cb):
            apply_cb()
            self._font_result.setText("✓ 已生效；部分卡片的固定字号样式不受影响。")
        else:
            self._font_result.setText("已保存（重启后生效）；部分卡片的固定字号样式不受影响。")

    # ---------- 可选组件（v2.0.1：基础版按需下载） ----------
    def _build_components_tab(self) -> QWidget:
        from .. import components
        from .component_center import component_center

        page, lay = self._page(
            "组件",
            "基础版不预装识别组件，用到时下载一次，之后在本机离线识别，图片不上传。"
            "下载和软件更新走同一套：GitHub 优先，连不上自动换备用服务器。")

        self._component_rows: dict[str, dict] = {}
        for comp in components.ALL:
            card = QFrame()
            card.setObjectName("setCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 12, 14, 12)
            row.setSpacing(12)
            text = QVBoxLayout()
            text.setSpacing(2)
            name = QLabel(comp.name)
            name.setObjectName("setRowTitle")
            purpose = QLabel("用于" + comp.purpose)
            purpose.setObjectName("setRowDesc")
            status = QLabel("")
            status.setObjectName(f"componentStatus_{comp.key}")
            status.setWordWrap(True)
            status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            text.addWidget(name)
            text.addWidget(purpose)
            text.addWidget(status)
            row.addLayout(text, 1)
            download = QPushButton("")
            download.clicked.connect(lambda _=False, k=comp.key: self._component_download(k))
            remove = QPushButton("卸载")
            remove.setToolTip("删除本机解压出来的组件、释放磁盘空间；需要时可以再下载")
            remove.clicked.connect(lambda _=False, k=comp.key: self._component_uninstall(k))
            row.addWidget(download, 0, Qt.AlignVCenter)
            row.addWidget(remove, 0, Qt.AlignVCenter)
            lay.addWidget(card)
            self._component_rows[comp.key] = {"status": status, "download": download,
                                              "remove": remove, "size": None, "busy": False,
                                              "note": ""}
        hint = QLabel("公司网络连不上 GitHub 和备用服务器时，可以改装「完整版」安装包，它已内置全部组件。")
        hint.setWordWrap(True)
        hint.setObjectName("setNote")
        lay.addWidget(hint)
        lay.addStretch(1)
        component_center().state_changed.connect(self._on_component_state)
        self._components_measured = False
        self._refresh_components()
        return page

    def _on_component_state(self, key: str) -> None:
        if self._closing or not _qt_is_valid(self):
            return
        from .component_center import component_center
        done = component_center().snapshot(key)["phase"] == "done"
        self._refresh_components(measure=done and self._components_measured)

    def _refresh_components(self, *, measure: bool = False) -> None:
        from .. import components
        from .component_center import component_center

        center = component_center()
        for key, row in getattr(self, "_component_rows", {}).items():
            if not _qt_is_valid(row["status"]):
                return
            comp = components.info(key)
            snap = center.snapshot(key)
            phase = snap["phase"]
            moving = phase in ("checking", "downloading", "installing")
            installed = components.is_installed(key)
            is_bundled = components.bundled(key)
            version = components.installed_version(key)
            size = row["size"]
            if row["busy"]:
                text = "正在卸载…"
            elif moving:
                st = snap["status"] or {}
                total = st.get("total") or snap["bytes"] or 0
                pct = f" {int(st.get('received', 0) * 100 / total)}%" if total else ""
                text = {"checking": "正在查询下载源…", "installing": "正在校验并解压…"}.get(
                    phase, f"正在下载{pct}")
                row["download"].setText("查看进度")
            elif installed:
                text = "已安装" + (f" v{version}" if version else "")
                if size:
                    text += f" · 占用 {_format_size_text(size)}"
                if is_bundled:
                    text += " · 随安装包内置"
                row["download"].setText("查看")
            elif is_bundled:
                text = "安装包已内置，第一次使用时自动解压（不联网）"
                row["download"].setText("现在解压")
            else:
                text = f"未安装 · 下载约 {comp.approx_mb} MB"
                if phase == "error":
                    text += " · 上次下载失败，点「下载」查看原因并重试"
                row["download"].setText("下载")
            if row["note"] and not moving and not row["busy"]:
                text += " · " + row["note"]
            row["status"].setText(text)
            primary = not installed or moving
            if row["download"].objectName() != ("primary" if primary else ""):
                row["download"].setObjectName("primary" if primary else "")
                row["download"].style().unpolish(row["download"])
                row["download"].style().polish(row["download"])
            # 内置的文字识别组件跟着安装包走，卸了下次用又会自动解压，不给卸载按钮
            row["remove"].setVisible(installed and not (key == "ocr" and is_bundled))
            row["remove"].setEnabled(not (moving or row["busy"]))
            row["download"].setEnabled(not row["busy"])
        if measure:
            self._measure_components()

    def _measure_components(self) -> None:
        from .. import components

        def measure():
            return {c.key: components.disk_bytes(c.key) for c in components.ALL}

        task = BackgroundTask(measure, "component-size", None)
        self._track_diag_task(task)
        task.done.connect(self._on_component_sizes)
        task.finished.connect(lambda task=task: self._forget_diag_task(task))
        task.start()

    def _on_component_sizes(self, result: object) -> None:
        if not isinstance(result, dict) or self._closing or not _qt_is_valid(self):
            return
        for key, size in result.items():
            row = self._component_rows.get(key)
            if row is not None:
                row["size"] = size
        self._refresh_components()

    def _component_download(self, key: str) -> None:
        from .. import components
        from .component_center import component_center
        center = component_center()
        self._component_rows[key]["note"] = ""
        if components.is_installed(key) or center.busy(key):
            center.open_dialog(key, self)
        else:
            center.request(key, self)

    def _confirm_component_uninstall(self, key: str) -> bool:
        from .. import components
        comp = components.info(key)
        size = self._component_rows[key]["size"]
        tail = "（完整版会从安装包自动解压，不联网）。" if components.bundled(key) else "。"
        box = QMessageBox(QMessageBox.Question, f"卸载{comp.name}",
                          f"删除本机的{comp.name}" + (f"，释放约 {_format_size_text(size)}" if size else "")
                          + f"？\n之后再用{comp.purpose}时需要重新下载" + tail,
                          QMessageBox.NoButton, self)
        ok_btn = box.addButton("卸载", QMessageBox.DestructiveRole)
        back_btn = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(back_btn)
        box.setEscapeButton(back_btn)
        box.exec()
        return box.clickedButton() is ok_btn

    def _component_uninstall(self, key: str) -> None:
        from .. import components
        from .component_center import component_center
        if component_center().busy(key) or not self._confirm_component_uninstall(key):
            return
        row = self._component_rows[key]
        row["busy"] = True
        row["note"] = ""
        self._refresh_components()

        def work():
            try:
                return components.uninstall(key)
            except Exception as exc:  # noqa: BLE001 原因带回界面
                return exc

        task = BackgroundTask(work, "component-uninstall", None)
        self._track_diag_task(task)
        task.done.connect(lambda result, k=key: self._on_component_uninstalled(k, result))
        task.finished.connect(lambda task=task: self._forget_diag_task(task))
        task.start()

    def _on_component_uninstalled(self, key: str, result: object) -> None:
        if self._closing or not _qt_is_valid(self):
            return
        row = self._component_rows[key]
        row["busy"] = False
        row["size"] = None
        if isinstance(result, BaseException):
            row["note"] = f"卸载失败：{result}"
        elif isinstance(result, int) and result:
            row["note"] = f"已卸载，释放 {_format_size_text(result)}"
        self._refresh_components(measure=self._components_measured)

    def _on_settings_tab_changed(self, index: int) -> None:
        # 组件占用要遍历上千个文件：切到「组件」页才在后台量，不拖慢设置页打开
        if self.tabs.tabText(index) == "组件" and not self._components_measured:
            self._components_measured = True
            self._measure_components()

    def show_tab(self, name: str) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == name:
                self.tabs.setCurrentIndex(i)
                return

    def _build_health_tab(self) -> QWidget:
        page, lay = self._page("健康诊断", "索引、版本库和运行环境的体检结果；反馈问题时点「复制」把全文发过来。",
                               scroll=False)
        head = QHBoxLayout()
        head.setSpacing(8)
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
        # 检查更新：打开与主窗口同一个更新弹窗（多源、进度、各源结果）
        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.setObjectName("checkUpdateBtn")
        updater_ctl = getattr(self._diagnostic_parent, "_updater", None)
        if updater_ctl is not None and hasattr(updater_ctl, "open_dialog"):
            pending = updater_ctl.pending_version() if hasattr(updater_ctl, "pending_version") else ""
            if pending:
                self.check_update_btn.setText(f"⬆ 有新版本 v{pending}")
            self.check_update_btn.setToolTip("查询 GitHub 与备用服务器上的最新版本")
            self.check_update_btn.clicked.connect(lambda: updater_ctl.open_dialog())
        else:
            self.check_update_btn.setEnabled(False)
            self.check_update_btn.setToolTip("源码运行的开发版不检查更新；安装版可用")
        head.addWidget(self.check_update_btn)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.schedule_diagnostics_refresh)
        head.addWidget(refresh)
        copy = QPushButton("复制")
        copy.clicked.connect(self._copy_diagnostics)
        head.addWidget(copy)
        head.addStretch(1)
        lay.addLayout(head)

        self.diagnostic_text = QPlainTextEdit()
        self.diagnostic_text.setReadOnly(True)
        self.diagnostic_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.diagnostic_text.setPlainText("诊断加载中…")
        lay.addWidget(self.diagnostic_text, 1)
        self.schedule_diagnostics_refresh()
        return page

    def _build_powerpoint_tab(self) -> QWidget:
        page, lay = self._page(
            "PowerPoint", "检测预览和跳转所需的 PowerPoint COM 能力。检测在后台执行，不会关闭你已打开的演示文稿。")
        card = self._card(lay)
        self.powerpoint_status = QLabel("尚未检测")
        self.powerpoint_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.powerpoint_btn = QPushButton("开始检测")
        self.powerpoint_btn.clicked.connect(self._check_powerpoint)
        self._row(card, "PowerPoint 检测", self.powerpoint_status, self.powerpoint_btn)
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
            f"custom_index_roots: {' | '.join(get_index_roots()) or '(none)'}",
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

        try:
            from .. import imgtext_ocr
            lines.append(imgtext_ocr.self_test())
        except Exception as exc:  # noqa: BLE001 可选组件不该拖垮诊断
            lines.append(f"imgtext_ocr: unavailable ({type(exc).__name__}: {exc})")
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

    def showEvent(self, event):  # noqa: N802
        # 版本库体积测量（rglob 全目录）只在对话框真正打开时惰性触发，放后台线程
        super().showEvent(event)
        self._refresh_vault_size()

    def closeEvent(self, event):  # noqa: N802
        if not self._confirm_close_with_unsaved_roots():
            event.ignore()
            return
        # Closing the settings panel should never block on a COM diagnostic.
        # QDialog close hides the window; running tasks are kept alive by
        # self._diag_tasks and clean themselves up via their finished signal.
        self._closing = True
        self._diag_refresh_token += 1
        self._diag_inflight_token = None
        self._powerpoint_inflight = False
        self._vault_audit_inflight = False
        self._index_roots_inflight = False
        self._autostart_toggle_token += 1
        self._autostart_toggle_inflight = False
        self._retention_update_token += 1
        self._retention_update_inflight = False
        self._vault_size_inflight = False
        super().closeEvent(event)


def _vault_size_bytes_off_ui() -> int:
    """后台线程里测量版本库体积；目录不存在返回 0（不创建目录）。"""
    from ..versioning import vault as vault_mod

    return vault_mod.vault_size_bytes()


def _format_size_text(nbytes: int) -> str:
    mb = nbytes / 1048576
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


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
