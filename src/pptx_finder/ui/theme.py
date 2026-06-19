"""多风格 QSS：云白极简 / 深色经典 / 深空影院 / 莫兰迪奶油 / 极光玻璃。

每套风格用一份颜色 + 氛围 + 圆角 token；_QSS 模板参数化生成。
- base：QWidget 全局底色（深空/极光用 transparent，让 appbg 渐变透出）
- appbg：主窗背景（可为 qlineargradient / qradialgradient 渐变）
- panel/panel2：顶栏预览 / 列表状态栏背景（深空/极光用半透明 rgba 透出 appbg）
- radius：基础圆角（莫兰迪用大圆角）
win 保持纯色（report_overlay 卡片背景依赖它，不可为渐变/透明）。
"""
from __future__ import annotations

from string import Template

TOKENS: dict[str, dict[str, str]] = {
    "cloud": dict(
        win="#FFFFFF", canvas="#FBFCFD", field="#F4F6F9", hover="#EEF1F5",
        sel="#E7F0FF", selblur="#EDEFF2", ink1="#1D1D1F", ink2="#494B50",
        ink3="#6E6E73", ink4="#A0A4AB", bd="#E9EBEF", bd2="#E0E3E8",
        acc="#0A84FF", accd="#0A66D6", acctext="#FFFFFF", grn="#1A8F3C",
        scroll="#D2D6DD", scrollh="#BCC2CC", hl_r="10", hl_g="132", hl_b="255", hl_a="0.16",
        base="#FFFFFF", appbg="#FFFFFF", panel="#FFFFFF", panel2="#FBFCFD", radius="9",
    ),
    "raycast": dict(
        win="#161619", canvas="#1C1C21", field="#26262D", hover="#24242B",
        sel="#2E2E38", selblur="#222229", ink1="#F2F2F4", ink2="#C6C6CC",
        ink3="#8A8A92", ink4="#5C5C64", bd="#2A2A31", bd2="#33333B",
        acc="#6E9BF0", accd="#5B8DEF", acctext="#10131A", grn="#46C77E",
        scroll="#33333B", scrollh="#44444E", hl_r="110", hl_g="155", hl_b="240", hl_a="0.26",
        base="#161619", appbg="#161619", panel="#161619", panel2="#1C1C21", radius="9",
    ),
    # —— 深空影院：深黑 + 胶片暖金 + 电蓝；appbg 暖金角落微光，面板半透明 ——
    "cinema": dict(
        win="#0E0E12", canvas="#121217", field="#1A1A22", hover="#1E1E28",
        sel="#2A2520", selblur="#1C1C24", ink1="#F2EFE8", ink2="#C8C4BA",
        ink3="#8A8780", ink4="#5C5950", bd="#26262E", bd2="#33333C",
        acc="#E3B572", accd="#C8954A", acctext="#1A1206", grn="#5FD39A",
        scroll="#33333C", scrollh="#44444E", hl_r="227", hl_g="181", hl_b="114", hl_a="0.18",
        base="transparent",
        appbg=("qradialgradient(cx:0.16, cy:0.0, radius:1.05, fx:0.16, fy:0.0, "
               "stop:0 #1c1611, stop:0.45 #0E0E12, stop:1 #0a0a0d)"),
        panel="rgba(20,20,26,0.62)", panel2="rgba(14,14,18,0.6)", radius="10",
    ),
    # —— 莫兰迪奶油：低饱和暖色 + 鼠尾草绿；纯色 + 大圆角 ——
    "morandi": dict(
        win="#F0EBE2", canvas="#EAE4D8", field="#F7F2EA", hover="#EDE6D9",
        sel="#E3DDCC", selblur="#ECE6DA", ink1="#4A4138", ink2="#6B5D4F",
        ink3="#9B8E7D", ink4="#B3A896", bd="#E0D7C6", bd2="#D5CAB5",
        acc="#9CAF88", accd="#7E9268", acctext="#FFFFFF", grn="#9CAF88",
        scroll="#D5CAB5", scrollh="#C4B89F", hl_r="156", hl_g="175", hl_b="136", hl_a="0.22",
        base="#F0EBE2", appbg="#F0EBE2", panel="#F7F2EA", panel2="#EAE4D8", radius="16",
    ),
    # —— 极光玻璃：深紫 + 紫青粉；appbg 极光流光，面板半透明透出 ——
    "aurora": dict(
        win="#0F0C1D", canvas="#14112A", field="#1C1838", hover="#221E40",
        sel="#2A2450", selblur="#1E1A3A", ink1="#F0EEFC", ink2="#C8C2E8",
        ink3="#9890C0", ink4="#6A6298", bd="#2A2548", bd2="#383158",
        acc="#A855F7", accd="#9333EA", acctext="#FFFFFF", grn="#34D399",
        scroll="#383158", scrollh="#4A4170", hl_r="168", hl_g="85", hl_b="247", hl_a="0.24",
        base="transparent",
        appbg=("qlineargradient(x1:0, y1:0, x2:1, y2:1, "
               "stop:0 #2b1054, stop:0.38 #100c20, stop:0.66 #0b1a2e, stop:1 #281044)"),
        panel="rgba(24,20,46,0.6)", panel2="rgba(17,14,36,0.58)", radius="12",
    ),
}

# 风格顺序 + 中文标签（用于顶栏风格切换菜单）
THEMES: list[tuple[str, str]] = [
    ("cloud", "云白极简"),
    ("raycast", "深色经典"),
    ("cinema", "深空影院"),
    ("morandi", "莫兰迪奶油"),
    ("aurora", "极光玻璃"),
]

_QSS = Template("""
* { font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif; }
QWidget { background: $base; color: $ink1; font-size: 13px; }
QMainWindow, QWidget#central { background: $appbg; }
QToolTip { background: $field; color: $ink1; border: 1px solid $bd; }

/* 顶栏 */
QWidget#topBar { background: $panel; }

/* 搜索框 */
QLineEdit#searchBox {
  background: $field; border: 1.5px solid $bd; border-radius: ${radius}px;
  padding: 0 12px; font-size: 15px; color: $ink1; selection-background-color: $acc;
}
QLineEdit#searchBox:focus { border-color: $acc; }

/* 模式下拉 */
QComboBox {
  background: $field; border: 1px solid $bd; border-radius: 7px; padding: 4px 10px; color: $ink2;
}
QComboBox:hover { border-color: $bd2; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
  background: $win; border: 1px solid $bd; border-radius: 8px; padding: 4px;
  selection-background-color: $sel; selection-color: $ink1; outline: 0;
}

/* 按钮 */
QPushButton {
  background: $field; border: 1px solid $bd; border-radius: 7px; padding: 7px 14px; color: $ink1;
}
QPushButton:hover { background: $hover; }
QPushButton:disabled { color: $ink4; }
QPushButton#primary { background: $acc; border: 1px solid $acc; color: $acctext; font-weight: 600; }
QPushButton#primary:hover { background: $accd; }
QPushButton#ghost { background: transparent; border: none; color: $ink3; padding: 5px 8px; }
QPushButton#ghost:hover { background: $hover; color: $ink1; }

/* 筛选 chip */
QPushButton#chip { background: $field; border: 1px solid $bd; border-radius: 980px; padding: 4px 12px; color: $ink3; font-size: 12px; }
QPushButton#chip:hover { border-color: $bd2; color: $ink2; }
QPushButton#chip:checked { background: rgba($hl_r,$hl_g,$hl_b,0.16); border-color: $acc; color: $accd; }

/* 结果列表 */
QListWidget#resultList { background: $panel2; border: none; outline: 0; padding: 6px 4px; }
QListWidget#resultList::item { border: none; margin: 0; padding: 0; background: transparent; }
QListWidget#resultList::item:selected { background: transparent; }

/* 预览区 */
QWidget#previewPanel { background: $panel; }
QWidget#previewHeadBar { background: $panel; }
QLabel#previewImage { background: $field; border: 1px solid $bd; border-radius: ${radius}px; }
QLabel#pathLabel { color: $ink2; font-size: 12px; }
QLabel#metaLabel { color: $ink3; font-size: 11.5px; }
QPushButton#linkBtn { background: transparent; border: 1px solid $bd2; border-radius: 7px; padding: 2px 10px; color: $acc; font-size: 11.5px; font-weight: 600; }
QPushButton#linkBtn:hover { border-color: $acc; }

/* 左侧列表头（命中计数） */
QWidget#listPane { background: $panel2; }
QWidget#listHeadBar { background: $panel2; }
QLabel#sectionHead { color: $ink3; font-size: 11px; font-weight: 700; padding: 9px 12px 4px; background: transparent; }

/* 详情抽屉（07：版本时间线 / 大纲 / 文件信息） */
QWidget#detailPanel { background: $panel2; }
QLabel#detailHead { color: $ink2; font-size: 12px; font-weight: 600; padding: 9px 13px; border-bottom: 1px solid $bd; background: $panel; }
QLabel#detailSecT { color: $ink3; font-size: 11px; font-weight: 700; background: transparent; }
QLabel#detailMeta { color: $ink3; font-size: 11.5px; background: transparent; }
QLabel#detailMuted { color: $ink4; font-size: 11.5px; background: transparent; }
QWidget#verNode { border-left: 2px solid $bd2; }
QLabel#verLatest { color: $acc; font-size: 12px; font-weight: 700; background: transparent; }
QLabel#verTitle { color: $ink2; font-size: 12px; font-weight: 600; background: transparent; }
QLabel#verTs { color: $ink4; font-size: 10.5px; background: transparent; }
QLabel#verUp { color: $grn; font-size: 10px; font-weight: 700; background: transparent; }
QLabel#verDn { color: #ff7a6b; font-size: 10px; font-weight: 700; background: transparent; }
QPushButton#verBtn { background: transparent; border: 1px solid $bd2; border-radius: 5px; padding: 2px 9px; color: $ink2; font-size: 11px; }
QPushButton#verBtn:hover { border-color: $acc; color: $acc; }
QPushButton#verBtnPri { background: $acc; border: 1px solid $acc; border-radius: 5px; padding: 2px 9px; color: $acctext; font-size: 11px; font-weight: 600; }
QPushButton#outlineItem { background: transparent; border: none; text-align: left; padding: 4px 6px; color: $ink2; font-size: 11.5px; border-radius: 5px; }
QPushButton#outlineItem:hover { background: $hover; color: $acc; }
QLabel#listHead { color: $ink3; font-size: 11.5px; font-weight: 600; background: transparent; }
QComboBox#sortCombo { background: transparent; border: 1px solid $bd; border-radius: 6px; padding: 2px 8px; color: $ink3; font-size: 11.5px; }
QComboBox#sortCombo:hover { border-color: $bd2; color: $ink2; }
QComboBox#sortCombo QAbstractItemView { background: $win; border: 1px solid $bd; border-radius: 8px; selection-background-color: $sel; selection-color: $ink1; }

/* 搜索历史下拉 */
QListView#historyPopup { background: $win; border: 1px solid $bd2; border-radius: 8px; padding: 4px; outline: 0; color: $ink1; }
QListView#historyPopup::item { padding: 6px 12px; border-radius: 6px; }
QListView#historyPopup::item:selected { background: $sel; color: $ink1; }

/* 零结果引导面板 */
QWidget#emptyHint { background: $panel2; }
QLabel#emptyIcon { font-size: 38px; background: transparent; }
QLabel#emptyTitle { color: $ink2; font-size: 15px; font-weight: 600; background: transparent; }
QLabel#emptyTip { color: $ink3; font-size: 12px; background: transparent; }
QPushButton#suggBtn { background: $field; border: 1px solid $bd2; border-radius: 8px; padding: 7px 18px; color: $acc; font-size: 12.5px; font-weight: 600; }
QPushButton#suggBtn:hover { border-color: $acc; background: $hover; }

/* 结果卡片缩略图 */
QLabel#cardThumb { background: $field; border: 1px solid $bd; border-radius: 5px; }

/* 缩略图按钮 */
QToolButton#thumb { background: $field; border: 1px solid $bd; border-radius: 5px; padding: 0; }
QToolButton#thumb:hover { border-color: $bd2; }
QToolButton#thumb:checked { border: 2px solid $acc; }

/* 命中页导航 */
QPushButton#navBtn { background: $field; border: 1px solid $bd; border-radius: 6px; padding: 4px 12px; color: $ink2; font-size: 12px; }
QPushButton#navBtn:disabled { color: $ink4; }

/* 状态栏 */
QStatusBar#statusBar { background: $panel2; border-top: 1px solid $bd; color: $ink3; }
QStatusBar#statusBar QLabel { color: $ink3; font-size: 12px; background: transparent; }
QLabel#kbd { color: $ink1; background: $field; border: 1px solid $bd2; border-radius: 5px; padding: 2px 6px; font-size: 11px; font-weight: 600; }

/* 索引进度条 + 百分比 + 就绪绿点 */
QProgressBar#indexBar { background: $bd2; border: none; border-radius: 4px; max-height: 7px; min-height: 7px; }
QProgressBar#indexBar::chunk { background: $acc; border-radius: 4px; }
QLabel#pctLabel { color: $acc; font-size: 12px; font-weight: 700; padding: 0 4px; background: transparent; }
QLabel#statusDot { color: $grn; font-size: 13px; padding: 0 2px 0 4px; background: transparent; }

/* 分隔条 */
QSplitter::handle { background: $bd; } QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:hover { background: $acc; }

/* 滚动条 */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: $scrollh; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* 右键菜单 */
QMenu { background: $win; border: 1px solid $bd2; border-radius: 11px; padding: 6px; }
QMenu::item { padding: 7px 24px 7px 14px; border-radius: 7px; color: $ink1; font-size: 12.5px; }
QMenu::item:selected { background: $hover; }
QMenu::separator { height: 1px; background: $bd; margin: 4px 6px; }

/* —— 次级窗口（版本管理 / 设置）通用控件：复用主体配色语言，5 套主题自动适配 ——
   主窗的 #searchBox / #resultList 因 id 选择器优先级更高，外观不受这些裸规则影响 */
QLineEdit {
  background: $field; border: 1.5px solid $bd; border-radius: 7px;
  padding: 5px 10px; color: $ink1; selection-background-color: $acc;
}
QLineEdit:focus { border-color: $acc; }
QListWidget {
  background: $field; border: 1px solid $bd; border-radius: ${radius}px;
  outline: 0; padding: 4px;
}
QListWidget::item { color: $ink1; padding: 7px 9px; border-radius: 6px; }
QListWidget::item:hover { background: $hover; }
QListWidget::item:selected { background: $sel; color: $ink1; }
QCheckBox { color: $ink1; spacing: 7px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1.5px solid $bd2; border-radius: 5px; background: $field; }
QCheckBox::indicator:checked { background: $acc; border-color: $acc; }

/* 浮层 Toast（操作反馈，中下方淡入淡出；固定深色胶囊 + 主题色左缘，5 风格都醒目） */
QLabel#toast {
  background: rgba(28,28,32,0.96); color: #F2EFE8;
  border: 1px solid rgba(255,255,255,0.12); border-left: 3px solid $acc;
  border-radius: 9px; padding: 9px 18px; font-size: 13px; font-weight: 500;
}
""")


def build_qss(theme: str) -> str:
    return _QSS.substitute(TOKENS.get(theme, TOKENS["cloud"]))


def tok(theme: str) -> dict[str, str]:
    return TOKENS.get(theme, TOKENS["cloud"])


def highlight_css(theme: str) -> str:
    """结果片段命中词高亮：荧光底 + 加粗 + 强调色，搜索命中一眼可见。"""
    t = tok(theme)
    return (f"background:rgba({t['hl_r']},{t['hl_g']},{t['hl_b']},{t['hl_a']});"
            f"border-radius:3px;font-weight:700;color:{t['accd']};padding:0 1px;")
