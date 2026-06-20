"""极光玻璃多主题 QSS（10 套）+ 自绘极光背景数据。

布局沿用主窗四栏 widget 结构（功能零改动），仅样式层玻璃化：
- 每套 token 提供半透明面板（panel/panel2/field 用 rgba）+ 极光光团 blobs（central 自绘用）。
- central 自绘极光（main_window.AuroraCentral 读 tok['blobs']/appbg），面板半透明透出。
- _QSS 保留全部原选择器（main_window 依赖），仅玻璃化：central 透明 / splitter 间隙 / pane 圆角高光。
- win 保持纯色（QMenu/弹出/report_overlay 依赖）。
接口 build_qss / tok / highlight_css 与旧版完全一致。
"""
from __future__ import annotations

from string import Template


def _rgb(hx: str):
    h = hx.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mk(bg, blobs, acc, acc2, ink, light=False):
    ar, ag, ab = _rgb(acc); A = f"{ar},{ag},{ab}"
    if light:
        d = dict(panel="rgba(255,255,255,0.82)", panel2="rgba(255,255,255,0.66)", field="rgba(255,255,255,0.72)",
                 hover="rgba(0,0,0,0.05)", sel=f"rgba({A},0.16)", selblur="rgba(0,0,0,0.06)",
                 bd="rgba(0,0,0,0.12)", bd2="rgba(0,0,0,0.06)", scroll="rgba(0,0,0,0.18)", scrollh="rgba(0,0,0,0.30)",
                 acctext="#FFFFFF", base=bg)
    else:
        d = dict(panel="rgba(255,255,255,0.07)", panel2="rgba(255,255,255,0.045)", field="rgba(255,255,255,0.06)",
                 hover="rgba(255,255,255,0.10)", sel=f"rgba({A},0.22)", selblur="rgba(255,255,255,0.07)",
                 bd="rgba(255,255,255,0.16)", bd2="rgba(255,255,255,0.09)", scroll="rgba(255,255,255,0.16)", scrollh="rgba(255,255,255,0.30)",
                 acctext="#0B1020", base="transparent")
    d.update(win=bg, appbg=bg, blobs=blobs, ink1=ink[0], ink2=ink[1], ink3=ink[2], ink4=ink[3],
             acc=acc, accd=acc2, grn="#34D399", hl_r=str(ar), hl_g=str(ag), hl_b=str(ab), hl_a="0.26", radius="13",
             canvas=("#F7F8FA" if light else "#17171F"))  # report_overlay 浮层卡片背景（不透明）依赖 canvas
    return d


# 10 套主题：(fx,fy,fr,(r,g,b,a)) 极光光团供 central 自绘
TOKENS: dict[str, dict] = {
    "aurora": _mk("#0A0817",
        [(0.16, 0.10, 0.62, (124, 58, 237, 170)), (0.86, 0.16, 0.66, (6, 182, 212, 150)),
         (0.60, 0.92, 0.72, (236, 72, 153, 110)), (0.30, 0.66, 0.58, (59, 130, 246, 95))],
        "#22D3EE", "#67E8F9", ("#F0EEFC", "#C8C2E8", "#948CC0", "#6A6298")),
    "cinema": _mk("#0A0A0D",
        [(0.30, 0.02, 0.74, (227, 181, 114, 155)), (0.86, 0.28, 0.60, (201, 120, 50, 120)),
         (0.12, 0.90, 0.62, (150, 60, 30, 95)), (0.60, 0.62, 0.50, (240, 205, 130, 72))],
        "#E3B572", "#F4D79A", ("#F4EFE6", "#CFC7B8", "#928876", "#5E574A")),
    "cyber": _mk("#070611",
        [(0.14, 0.12, 0.60, (255, 0, 128, 160)), (0.88, 0.16, 0.62, (0, 229, 255, 150)),
         (0.62, 0.92, 0.66, (138, 43, 226, 125)), (0.34, 0.60, 0.55, (94, 0, 214, 100))],
        "#00E5FF", "#7DF9FF", ("#F3EDFF", "#D4C7EE", "#9384C2", "#675896")),
    "ocean": _mk("#04131B",
        [(0.18, 0.12, 0.62, (0, 184, 184, 155)), (0.85, 0.20, 0.64, (20, 222, 165, 135)),
         (0.60, 0.92, 0.70, (8, 92, 162, 115)), (0.32, 0.62, 0.55, (0, 150, 205, 95))],
        "#2DD4BF", "#5EEAD4", ("#E4F6F3", "#BCE0DA", "#79A8A2", "#4E726D")),
    "magma": _mk("#150608",
        [(0.20, 0.08, 0.66, (255, 94, 40, 160)), (0.85, 0.22, 0.60, (222, 42, 92, 135)),
         (0.55, 0.92, 0.68, (150, 24, 40, 110)), (0.34, 0.60, 0.52, (255, 160, 46, 92))],
        "#FF7A3C", "#FFB454", ("#FBEDE6", "#E6C8BC", "#B88E7E", "#7E5C4E")),
    "forest": _mk("#07140D",
        [(0.18, 0.10, 0.64, (40, 184, 84, 155)), (0.84, 0.20, 0.60, (120, 222, 160, 120)),
         (0.60, 0.92, 0.68, (18, 96, 54, 115)), (0.32, 0.62, 0.54, (20, 160, 140, 95))],
        "#4ADE80", "#86EFAC", ("#E8F6EC", "#C2E0CB", "#82AE90", "#557A62")),
    "sakura": _mk("#170810",
        [(0.18, 0.10, 0.64, (255, 132, 180, 155)), (0.85, 0.20, 0.60, (240, 92, 142, 135)),
         (0.60, 0.92, 0.66, (196, 100, 200, 115)), (0.34, 0.60, 0.54, (255, 196, 206, 92))],
        "#F472B6", "#FBA8D0", ("#FBEEF4", "#E8C9DA", "#BC8FA6", "#826071")),
    "midnight": _mk("#060A18",
        [(0.16, 0.10, 0.62, (40, 92, 255, 160)), (0.86, 0.18, 0.64, (0, 150, 255, 140)),
         (0.60, 0.92, 0.70, (78, 60, 220, 115)), (0.32, 0.62, 0.55, (0, 182, 230, 95))],
        "#3B82F6", "#93C5FD", ("#EAF0FF", "#C5D2EC", "#8A98BE", "#586488")),
    "graphite": _mk("#0C0D11",
        [(0.18, 0.12, 0.58, (150, 160, 185, 90)), (0.85, 0.20, 0.60, (110, 125, 150, 80)),
         (0.60, 0.90, 0.64, (90, 100, 125, 70)), (0.34, 0.60, 0.50, (180, 190, 210, 58))],
        "#AEB6C6", "#E2E8F0", ("#F0F2F6", "#CDD2DC", "#8E95A4", "#5E6472")),
    "cloud": _mk("#EDF1F7",
        [(0.16, 0.10, 0.60, (120, 170, 255, 95)), (0.85, 0.18, 0.60, (255, 180, 210, 85)),
         (0.60, 0.92, 0.66, (150, 230, 210, 80)), (0.34, 0.60, 0.55, (200, 180, 255, 75))],
        "#0A84FF", "#0A66D6", ("#1D1D1F", "#494B50", "#6E6E73", "#A0A4AB"), light=True),
}

THEMES: list[tuple[str, str]] = [
    ("aurora", "极光玻璃"), ("cinema", "胶片放映厅"), ("cyber", "赛博霓虹"), ("ocean", "深海极光"),
    ("magma", "熔岩黄昏"), ("forest", "森林晨雾"), ("sakura", "樱花粉黛"), ("midnight", "午夜电蓝"),
    ("graphite", "石墨极简"), ("cloud", "云白晨光"),
]

_QSS = Template("""
* { font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif; }
QWidget { background: $base; color: $ink1; font-size: 13px; }
QMainWindow { background: $win; }
QWidget#central { background: transparent; }
QToolTip { background: $win; color: $ink1; border: 1px solid $bd; }

/* 玻璃标题栏（无边框窗口的自绘标题栏） */
QWidget#glassTitle { background: $panel; border-bottom: 1px solid $bd; }
QLabel#gtDot { color: $acc; font-size: 12px; background: transparent; }
QLabel#gtName { color: $ink1; font-size: 13px; font-weight: 700; background: transparent; }
QLabel#gtVer { color: $ink3; font-size: 11px; background: transparent; }
QLabel#gtTheme { color: $ink3; font-size: 12px; background: transparent; }
QPushButton#winMin, QPushButton#winMax, QPushButton#winClose { background: transparent; border: none; color: $ink2; font-size: 13px; }
QPushButton#winMin:hover, QPushButton#winMax:hover { background: rgba($hl_r,$hl_g,$hl_b,0.35); color: $ink1; }
QPushButton#winClose:hover { background: #E81123; color: #FFFFFF; }

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
QPushButton#primary:pressed { background: $accd; padding-top: 8px; padding-bottom: 6px; }
QPushButton#ghost { background: transparent; border: none; color: $ink3; padding: 5px 8px; }
QPushButton#ghost:hover { background: $hover; color: $ink1; }

/* 筛选 chip */
QPushButton#chip { background: $field; border: 1px solid $bd; border-radius: 980px; padding: 4px 12px; color: $ink3; font-size: 12px; }
QPushButton#chip:hover { border-color: $bd2; color: $ink2; }
QPushButton#chip:checked { background: rgba($hl_r,$hl_g,$hl_b,0.16); border-color: $acc; color: $acc; }

/* 结果列表（在 listPane 玻璃卡内，透明） */
QListWidget#resultList { background: transparent; border: none; outline: 0; padding: 6px 4px; }
QListWidget#resultList::item { border: none; margin: 0; padding: 0; background: transparent; }
QListWidget#resultList::item:selected { background: transparent; }

/* 预览区 — 玻璃卡 */
QWidget#previewPanel { background: $panel; border: 1px solid $bd; border-radius: ${radius}px; }
QWidget#previewHeadBar { background: transparent; }
QLabel#previewImage { background: $field; border: 1px solid $bd; border-radius: ${radius}px; }
QLabel#pathLabel { color: $ink2; font-size: 12px; }
QLabel#metaLabel { color: $ink3; font-size: 11.5px; }
QPushButton#linkBtn { background: transparent; border: 1px solid $bd2; border-radius: 7px; padding: 2px 10px; color: $acc; font-size: 11.5px; font-weight: 600; }
QPushButton#linkBtn:hover { border-color: $acc; }

/* 左侧列表 — 玻璃卡 */
QWidget#listPane { background: $panel2; border: 1px solid $bd; border-radius: ${radius}px; }
QWidget#listHeadBar { background: transparent; }
QLabel#sectionHead { color: $ink3; font-size: 11px; font-weight: 700; padding: 9px 12px 4px; background: transparent; }

/* facet 筛选抽屉 — 玻璃卡 */
QWidget#facetPanel { background: $panel2; border: 1px solid $bd; border-radius: ${radius}px; }
QWidget#facetHeadBar { background: transparent; border-bottom: 1px solid $bd; }
QLabel#facetHead { color: $ink2; font-size: 12px; font-weight: 600; background: transparent; }
QPushButton#facetClear { background: transparent; border: none; color: $acc; font-size: 11px; }
QLabel#facetDim { color: $ink4; font-size: 10.5px; font-weight: 700; padding: 8px 0 2px; background: transparent; }
QPushButton#facetChip { background: $field; border: 1px solid $bd2; border-radius: 7px; padding: 5px 11px; color: $ink2; font-size: 11.5px; text-align: left; }
QPushButton#facetChip:hover { border-color: $acc; }
QPushButton#facetChip:checked { background: rgba($hl_r,$hl_g,$hl_b,0.16); border-color: $acc; color: $acc; font-weight: 600; }

/* 详情抽屉 — 玻璃卡 */
QWidget#detailPanel { background: $panel2; border: 1px solid $bd; border-radius: ${radius}px; }
QLabel#detailHead { color: $ink2; font-size: 12px; font-weight: 600; padding: 9px 13px; border-bottom: 1px solid $bd; background: transparent; }
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
QWidget#emptyHint { background: transparent; }
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
QLabel#verShield { color: $grn; font-size: 11.5px; font-weight: 600; background: transparent; padding: 0 6px; }
QLabel#navDot { color: #ff453a; font-size: 13px; font-weight: 700; background: transparent; }

/* 索引进度条 + 百分比 + 就绪绿点 */
QProgressBar#indexBar { background: $bd2; border: none; border-radius: 4px; max-height: 7px; min-height: 7px; }
QProgressBar#indexBar::chunk { background: $acc; border-radius: 4px; }
QLabel#pctLabel { color: $acc; font-size: 12px; font-weight: 700; padding: 0 4px; background: transparent; }
QLabel#statusDot { color: $grn; font-size: 13px; padding: 0 2px 0 4px; background: transparent; }

/* 仪表盘首屏（零搜索默认视图） */
QWidget#dashView { background: transparent; }
QFrame#dashCard { background: $panel; border: 1px solid $bd; border-radius: ${radius}px; }
QLabel#dashTitle { color: $ink1; font-size: 21px; font-weight: 800; background: transparent; }
QLabel#dashSub { color: $ink3; font-size: 12.5px; background: transparent; }
QLabel#kpiNum { color: $acc; font-size: 27px; font-weight: 800; font-family: "Consolas"; background: transparent; }
QLabel#kpiLab { color: $ink2; font-size: 12.5px; font-weight: 600; background: transparent; }
QLabel#kpiSub { color: $ink4; font-size: 11px; background: transparent; }
QLabel#dashCardT { color: $ink2; font-size: 13px; font-weight: 700; background: transparent; }
QLabel#legName { color: $ink2; font-size: 12px; background: transparent; }
QLabel#legPc { color: $ink3; font-size: 12px; font-weight: 700; font-family: "Consolas"; background: transparent; }
QFrame#dashRec { background: $field; border: 1px solid $bd2; border-radius: 9px; }
QLabel#recName { color: $ink1; font-size: 12px; font-weight: 600; background: transparent; }
QLabel#recTime { color: $ink4; font-size: 11px; background: transparent; }
QLabel#recVer { color: $acc; font-size: 11px; font-weight: 700; background: transparent; }

/* 分隔条 — 加宽透明，玻璃卡之间透出极光当间隙 */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 12px; }
QSplitter::handle:hover { background: rgba($hl_r,$hl_g,$hl_b,0.20); border-radius: 3px; }

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

/* 次级窗口通用控件 */
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

/* 浮层 Toast */
QLabel#toast {
  background: rgba(28,28,32,0.96); color: #F2EFE8;
  border: 1px solid rgba(255,255,255,0.12); border-left: 3px solid $acc;
  border-radius: 9px; padding: 9px 18px; font-size: 13px; font-weight: 500;
}
""")

# substitute 只替换字符串 $var，TOKENS 里的 blobs(list) 会被忽略（不影响 QSS）
_QSS_KEYS = ("win", "base", "appbg", "panel", "panel2", "field", "hover", "sel", "selblur",
             "ink1", "ink2", "ink3", "ink4", "bd", "bd2", "acc", "accd", "acctext", "grn",
             "scroll", "scrollh", "hl_r", "hl_g", "hl_b", "radius")


def build_qss(theme: str) -> str:
    t = TOKENS.get(theme, TOKENS["aurora"])
    return _QSS.substitute({k: t[k] for k in _QSS_KEYS})


def tok(theme: str) -> dict:
    return TOKENS.get(theme, TOKENS["aurora"])


def highlight_css(theme: str) -> str:
    """结果片段命中词高亮：荧光底 + 加粗 + 强调色。"""
    t = tok(theme)
    return (f"background:rgba({t['hl_r']},{t['hl_g']},{t['hl_b']},{t['hl_a']});"
            f"border-radius:3px;font-weight:700;color:{t['acc']};padding:0 1px;")
