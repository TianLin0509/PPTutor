"""主题体系：静白工作室（方向 A，默认）+ 旧极光玻璃 10 套。

方向 A「静白工作室」核心纪律（2026-07-16 定稿）：
- 蓝色预算：同屏强调色 ≤3 处（选中指示条 / 主按钮 / 焦点环），其余一律灰阶。
- 命中词荧光笔：hl_bg/hl_fg token（静白=荧光黄底深字；旧玻璃主题=原半透明强调底）。
- 不透明面板（panel/panel2/field 实色）、无极光光团（blobs=[]，AuroraCentral 只铺纯色）。
- 控件语言三统一：圆角三档（卡 12 / 控件 8 / 小件 5）、hover 只变底色、焦点环同一根。

布局沿用主窗四栏 widget 结构；旧 10 套玻璃主题 token 不动，仅共享打磨后的 QSS 模板。
接口 build_qss / tok / highlight_css 与旧版完全一致。
"""
from __future__ import annotations

from string import Template

from ..config import resource_path


def _rgb(hx: str):
    h = hx.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mk(bg, blobs, acc, acc2, ink, light=False):
    ar, ag, ab = _rgb(acc)
    A = f"{ar},{ag},{ab}"
    if light:
        d = dict(panel="rgba(255,255,255,0.96)", panel2="rgba(255,255,255,0.88)", field="rgba(255,255,255,0.94)",
                 hover=f"rgba({A},0.08)", sel=f"rgba({A},0.14)", selblur="rgba(44,55,72,0.07)",
                 bd="rgba(43,55,72,0.16)", bd2="rgba(43,55,72,0.10)", scroll="rgba(67,78,94,0.34)", scrollh="rgba(43,54,70,0.56)",
                 acctext="#FFFFFF", base=bg)
    else:
        d = dict(panel="rgba(255,255,255,0.10)", panel2="rgba(255,255,255,0.07)", field="rgba(255,255,255,0.09)",
                 hover="rgba(255,255,255,0.13)", sel=f"rgba({A},0.24)", selblur="rgba(255,255,255,0.08)",
                 bd="rgba(255,255,255,0.18)", bd2="rgba(255,255,255,0.11)", scroll="rgba(255,255,255,0.27)", scrollh="rgba(255,255,255,0.48)",
                 acctext="#0B1020", base="transparent")
    d.update(win=bg, appbg=bg, blobs=blobs, ink1=ink[0], ink2=ink[1], ink3=ink[2], ink4=ink[3],
             acc=acc, accd=acc2, grn="#34D399", hl_r=str(ar), hl_g=str(ag), hl_b=str(ab), hl_a="0.26", radius="12",
             is_light=light,  # 主题明暗标志：系统标题栏深浅、对比度判定用（替代旧的硬编码主题名清单）
             canvas=("#F8FAFD" if light else "#17171F"),  # report_overlay 浮层卡片背景（不透明）依赖 canvas
             hl_bg=f"rgba({A},0.26)", hl_fg=acc)  # 命中词高亮：旧玻璃主题维持原视觉
    return d


def _mk_atelier(light: bool) -> dict:
    """静白 / 静黑：不透明层级、无光团、荧光黄命中、灰阶徽章。"""
    if light:
        acc, accd = "#0A6CFF", "#0857CC"
        d = dict(
            win="#F5F5F7", appbg="#F5F5F7", base="transparent", blobs=[],
            panel="#FFFFFF", panel2="#FFFFFF", field="#F0F0F2",
            hover="rgba(29,29,31,0.045)", sel="#EEF3FA", selblur="rgba(29,29,31,0.055)",
            bd="#E3E3E7", bd2="#ECECEE",
            ink1="#1D1D1F", ink2="#494B50", ink3="#6E6E73", ink4="#A0A0A5",
            acc=acc, accd=accd, acctext="#FFFFFF", grn="#1E9E4A",
            scroll="rgba(60,60,67,0.30)", scrollh="rgba(60,60,67,0.55)",
            canvas="#F0F0F2", is_light=True,
            hl_bg="rgba(255,224,110,0.85)", hl_fg="#3A3423",  # 荧光笔黄：命中词唯一的非灰非蓝
        )
    else:
        acc, accd = "#0A84FF", "#3D9BFF"
        d = dict(
            win="#1B1B1E", appbg="#1B1B1E", base="transparent", blobs=[],
            panel="#252529", panel2="#232327", field="#2E2E33",
            hover="rgba(255,255,255,0.06)", sel="rgba(10,132,255,0.20)", selblur="rgba(255,255,255,0.07)",
            bd="rgba(255,255,255,0.11)", bd2="rgba(255,255,255,0.065)",
            ink1="#F5F5F7", ink2="#D0D0D5", ink3="#98989F", ink4="#6A6A70",
            acc=acc, accd=accd, acctext="#FFFFFF", grn="#32D74B",
            scroll="rgba(255,255,255,0.24)", scrollh="rgba(255,255,255,0.44)",
            canvas="#161619", is_light=False,
            hl_bg="rgba(255,214,10,0.30)", hl_fg="#FFD60A",
        )
    ar, ag, ab = _rgb(acc)
    d.update(hl_r=str(ar), hl_g=str(ag), hl_b=str(ab), hl_a="0.22", radius="12")
    return d


# 静白/静黑（方向 A）在前；旧 10 套：(fx,fy,fr,(r,g,b,a)) 极光光团供 central 自绘
TOKENS: dict[str, dict] = {
    "atelier": _mk_atelier(True),
    "atelier_dark": _mk_atelier(False),
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
    "cloud": _mk("#F2F5FA",
        [(0.16, 0.10, 0.60, (120, 170, 255, 95)), (0.85, 0.18, 0.60, (255, 180, 210, 85)),
         (0.60, 0.92, 0.66, (150, 230, 210, 80)), (0.34, 0.60, 0.55, (200, 180, 255, 75))],
        "#0A84FF", "#0A66D6", ("#1D1D1F", "#494B50", "#6E6E73", "#A0A4AB"), light=True),
}

THEMES: list[tuple[str, str]] = [
    ("atelier", "静白"), ("atelier_dark", "静黑"),
    ("aurora", "极光玻璃"), ("cinema", "胶片放映厅"), ("cyber", "赛博霓虹"), ("ocean", "深海极光"),
    ("magma", "熔岩黄昏"), ("forest", "森林晨雾"), ("sakura", "樱花粉黛"), ("midnight", "午夜电蓝"),
    ("graphite", "石墨极简"), ("cloud", "云白晨光"),
]

# 控件语言（方向 A）：圆角三档 卡$radius/控件8/小件5；hover 只变底色；焦点环统一 1.5px 强调色。
_QSS = Template("""
* { font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif; }
QWidget { background: $base; color: $ink1; font-size: 13px; }
QMainWindow { background: $win; }
QWidget#central { background: transparent; }
QToolTip { background: $win; color: $ink1; border: 1px solid $bd; border-radius: 7px; padding: 6px 9px; }

/* 合一工具栏（无边框窗口自绘标题栏：品牌 + 搜索 + 筛选 + 图标按钮 + 窗口控制） */
QWidget#glassTitle { background: $panel; border-bottom: 1px solid $bd2; }
QLabel#gtDot { color: $acc; font-size: 12px; background: transparent; }
QLabel#gtName { color: $ink1; font-size: 13px; font-weight: 700; background: transparent; }
QLabel#gtVer { color: $ink4; font-size: 10.5px; font-family: "Consolas", "Segoe UI"; background: transparent; }
QLabel#gtTheme { color: $ink3; font-size: 12px; background: transparent; }
QPushButton#updateChip { background: $acc; color: #FFFFFF; border: none; border-radius: 9px; padding: 3px 11px; font-size: 11px; font-weight: 600; }
QPushButton#updateChip:hover { background: $accd; }
QPushButton#updateChip:disabled { background: rgba($hl_r,$hl_g,$hl_b,0.45); color: $ink3; }
QPushButton#winMin, QPushButton#winMax, QPushButton#winClose { background: transparent; border: none; border-radius: 0; color: $ink3; font-size: 13px; }
QPushButton#winMin:hover, QPushButton#winMax:hover { background: $hover; color: $ink1; }
QPushButton#winClose:hover { background: #E81123; color: #FFFFFF; }

/* 顶栏（合一后仅承载搜索语法提示行，默认隐藏） */
QWidget#topBar { background: $panel; border: none; }

/* 搜索框：灰槽内嵌，聚焦时抬为面板底 + 焦点环（macOS 搜索语言） */
QLineEdit#searchBox {
  background: $field; border: 1.5px solid transparent; border-radius: 9px;
  padding: 0 12px; font-size: 14.5px; color: $ink1; selection-background-color: $acc; selection-color: $acctext;
}
QLineEdit#searchBox:hover { background: $hover; }
QLineEdit#searchBox:focus { border-color: $acc; background: $panel; }
QLabel#queryHint { color: $ink3; font-size: 11.5px; background: transparent; padding: 4px 4px 2px 14px; }

/* 下拉：与按钮同形制（控件圆角 8） */
QComboBox {
  background: transparent; border: 1px solid $bd; border-radius: 8px;
  padding: 5px 28px 5px 11px; color: $ink2;
}
QComboBox:hover { background: $hover; color: $ink1; }
QComboBox:on { border-color: $acc; color: $ink1; }
QComboBox:focus { border-color: $acc; }
QComboBox:disabled { color: $ink4; background: $selblur; }
QComboBox::drop-down { border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: top right; }
QComboBox::down-arrow { image: url("$combo_arrow"); width: 11px; height: 7px; }
QComboBox QAbstractItemView {
  background: $win; border: 1px solid $bd; border-radius: 9px; padding: 5px;
  selection-background-color: $sel; selection-color: $ink1; outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 28px; padding: 4px 9px; border-radius: 6px; }
QComboBox QAbstractItemView::item:hover { background: $hover; color: $ink1; }
QComboBox QAbstractItemView::item:selected { background: $sel; color: $ink1; }

/* 按钮：默认 = 面板底描边；hover 只变底色；primary 是全界面唯一实心强调 */
QPushButton {
  background: $panel; border: 1px solid $bd; border-radius: 8px; padding: 7px 14px; color: $ink1;
}
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $sel; }
QPushButton:disabled { color: $ink4; background: $selblur; border-color: $bd2; }
QPushButton#primary { background: $acc; border: 1px solid $acc; color: $acctext; font-weight: 600; }
QPushButton#primary:hover { background: $accd; border-color: $accd; }
QPushButton#primary:pressed { background: $accd; border-color: $accd; }
QPushButton#primary:disabled { background: $selblur; border-color: $bd2; color: $ink4; }
QPushButton#ghost { background: transparent; border: none; color: $ink2; padding: 5px 8px; }
QPushButton#ghost:hover { background: $hover; color: $ink1; }
QPushButton#ghost:pressed, QPushButton#ghost:checked { background: $sel; color: $acc; }

/* 工具栏图标按钮：方形 32，静默灰，checked 抬色 */
QPushButton#iconBtn { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0; color: $ink3; }
QPushButton#iconBtn:hover { background: $hover; color: $ink1; }
QPushButton#iconBtn:pressed, QPushButton#iconBtn:checked { background: $sel; color: $acc; }

/* 键盘焦点环：任何控件同一根 1.5px 强调色 */
QPushButton:focus, QComboBox:focus, QToolButton:focus { border: 1.5px solid $acc; }
QPushButton#ghost:focus, QPushButton#iconBtn:focus { border: 1.5px solid $acc; }
QPushButton#primary:focus { border: 2px solid $acctext; }

/* 筛选 chip（Aa 大小写等）：胶囊，灰默认，选中淡强调 */
QPushButton#chip { background: transparent; border: 1px solid $bd; border-radius: 980px; padding: 4px 12px; color: $ink3; font-size: 12px; }
QPushButton#chip:hover { background: $hover; color: $ink2; }
QPushButton#chip:checked { background: rgba($hl_r,$hl_g,$hl_b,0.14); border-color: $acc; color: $acc; }

/* 结果列表（在 listPane 卡内，透明） */
QListWidget#resultList { background: transparent; border: none; outline: 0; padding: 6px 4px; }
QListWidget#resultList::item { border: none; margin: 0; padding: 0; background: transparent; }
QListWidget#resultList::item:selected { background: transparent; }

/* 预览卡 */
QWidget#previewPanel { background: $panel; border: 1px solid $bd; border-radius: ${radius}px; }
QWidget#previewHeadBar { background: transparent; }
QLabel#previewImage { background: $canvas; border: 1px solid $bd2; border-radius: 10px; }
QLabel#pathLabel { color: $ink3; font-size: 12px; }
QLabel#metaLabel { color: $ink4; font-size: 11.5px; font-family: "Consolas", "Microsoft YaHei UI"; }
QPushButton#linkBtn { background: transparent; border: none; border-radius: 6px; padding: 3px 8px; color: $ink3; font-size: 11.5px; font-weight: 600; }
QPushButton#linkBtn:hover { background: $hover; color: $acc; }
QPushButton#detailAction {
  background: transparent; border: 1px solid $bd; border-radius: 8px; padding: 4px 12px;
  color: $ink2; font-size: 12px; font-weight: 600;
}
QPushButton#detailAction:hover { background: $hover; color: $ink1; }
QPushButton#detailAction:checked { background: $sel; border-color: $acc; color: $acc; }

/* 左列表卡 */
QWidget#listPane { background: $panel2; border: 1px solid $bd; border-radius: ${radius}px; }
QWidget#listHeadBar { background: transparent; }
QLabel#sectionHead { color: $ink3; font-size: 11px; font-weight: 700; padding: 9px 12px 4px; background: transparent; }

/* facet 筛选抽屉 */
QWidget#facetPanel { background: $panel2; border: 1px solid $bd; border-radius: ${radius}px; }
QWidget#facetHeadBar { background: transparent; border-bottom: 1px solid $bd2; }
QLabel#facetHead { color: $ink2; font-size: 12px; font-weight: 600; background: transparent; }
QPushButton#facetClear { background: transparent; border: none; color: $acc; font-size: 11px; }
QLabel#facetDim { color: $ink4; font-size: 10.5px; font-weight: 700; padding: 8px 0 2px; background: transparent; }
QPushButton#facetChip { background: transparent; border: 1px solid $bd2; border-radius: 7px; padding: 5px 11px; color: $ink2; font-size: 11.5px; text-align: left; }
QPushButton#facetChip:hover { background: $hover; }
QPushButton#facetChip:checked { background: rgba($hl_r,$hl_g,$hl_b,0.14); border-color: $acc; color: $acc; font-weight: 600; }

/* 详情抽屉 */
QWidget#detailPanel { background: $panel2; border: 1px solid $bd; border-radius: ${radius}px; }
#detailHead { background: $panel; border-bottom: 1px solid $bd2; border-top-left-radius: ${radius}px; border-top-right-radius: ${radius}px; }
QLabel#dtDot { color: $acc; font-size: 11px; background: transparent; }
QLabel#dtTitle { color: $ink1; font-size: 13px; font-weight: 700; background: transparent; }
QPushButton#dtClose { background: transparent; border: none; color: $ink3; font-size: 21px; font-weight: 600; border-radius: 8px; padding: 0; }
QPushButton#dtClose:hover { background: rgba(255,69,58,0.9); color: #ffffff; }
/* 次级窗口统一卡片化 Tab；主界面 detailTabs 保留紧凑下划线特例 */
QTabWidget::pane { background: $panel2; border: 1px solid $bd; border-radius: 10px; top: -1px; }
QTabBar::tab {
  background: transparent; color: $ink3; padding: 8px 14px; margin: 0 3px 4px 0;
  font-size: 12.5px; font-weight: 600; border: 1px solid transparent; border-radius: 7px;
}
QTabBar::tab:selected { color: $acc; background: $sel; }
QTabBar::tab:hover:!selected { color: $ink1; background: $hover; }
QTabBar::tab:focus { border-color: $acc; }
QTabWidget#detailTabs::pane { border: none; border-top: 1px solid $bd2; border-radius: 0; background: transparent; }
QTabWidget#detailTabs QTabBar::tab {
  background: transparent; margin: 0; padding: 8px 18px; border: none;
  border-radius: 0; border-bottom: 2px solid transparent;
}
QTabWidget#detailTabs QTabBar::tab:selected { color: $acc; border-bottom: 2px solid $acc; }
QTabWidget#detailTabs QTabBar::tab:hover:!selected { color: $ink1; background: $hover; }
QLabel#verChanged { color: $ink3; font-size: 11px; background: transparent; }
QLabel#detailSecT { color: $ink3; font-size: 11px; font-weight: 700; background: transparent; }
QLabel#detailMeta { color: $ink3; font-size: 11.5px; background: transparent; }
QLabel#detailMuted { color: $ink4; font-size: 11.5px; background: transparent; }
QWidget#verNode { border-left: 2px solid $bd2; }
QLabel#verLatest { color: $acc; font-size: 12px; font-weight: 700; background: transparent; }
QLabel#verTitle { color: $ink2; font-size: 12px; font-weight: 600; background: transparent; }
QLabel#verTs { color: $ink4; font-size: 10.5px; background: transparent; }
QLabel#verUp { color: $grn; font-size: 10px; font-weight: 700; background: transparent; }
QLabel#verDn { color: #ff7a6b; font-size: 10px; font-weight: 700; background: transparent; }
QLabel#verPreview { background: $field; border: 1px solid $bd2; border-radius: 6px; color: $ink4; font-size: 10.5px; }
QLabel#versionPreview { background: $field; border: 1px solid $bd2; border-radius: 6px; color: $ink4; font-size: 11px; }
QPushButton#verBtn { background: transparent; border: 1px solid $bd2; border-radius: 5px; padding: 2px 9px; color: $ink2; font-size: 11px; }
QPushButton#verBtn:hover { background: $hover; color: $ink1; }
QPushButton#verBtnPri { background: $acc; border: 1px solid $acc; border-radius: 5px; padding: 2px 9px; color: $acctext; font-size: 11px; font-weight: 600; }
QPushButton#verPreviewBtn { background: transparent; border: 1px solid $bd2; border-radius: 5px; padding: 3px 10px; color: $ink2; font-size: 11px; }
QPushButton#verPreviewBtn:hover { background: $hover; color: $ink1; }
QPushButton#outlineItem { background: transparent; border: none; text-align: left; padding: 4px 6px; color: $ink2; font-size: 11.5px; border-radius: 5px; }
QPushButton#outlineItem:hover { background: $hover; color: $acc; }
QLabel#listHead { color: $ink3; font-size: 11.5px; font-weight: 600; background: transparent; }
QComboBox#sortCombo, QComboBox#sortSecondary { background: transparent; border: 1px solid $bd2; border-radius: 7px; padding: 3px 24px 3px 9px; color: $ink3; font-size: 11.5px; }
QComboBox#sortCombo:hover, QComboBox#sortSecondary:hover { background: $hover; color: $ink2; }
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
QLabel#emptyMeta { color: $ink4; font-size: 11.5px; background: transparent; }
QPushButton#suggBtn { background: transparent; border: 1px solid $bd; border-radius: 8px; padding: 7px 18px; color: $ink2; font-size: 12.5px; font-weight: 600; }
QPushButton#suggBtn:hover { background: $hover; color: $acc; }

/* 结果卡片缩略图 */
QLabel#cardThumb { background: $field; border: 1px solid $bd; border-radius: 5px; }

/* 版本组展开器（「N 个历史版本 ▾」）：灰静默，hover 才提示可点 */
QToolButton#verExpand { background: transparent; border: 1px solid $bd2; border-radius: 980px; padding: 1px 9px; color: $ink3; font-size: 11px; font-weight: 600; }
QToolButton#verExpand:hover { background: $hover; border-color: $bd; color: $acc; }

/* 命中页缩略图 / 页码分段：选中白底浮起（浅）或强调描边 */
QToolButton#thumb { background: $field; border: 1px solid transparent; border-radius: 6px; padding: 0; color: $ink3; }
QToolButton#thumb:hover { background: $hover; color: $ink1; }
QToolButton#thumb:checked { background: $panel; border: 1.5px solid $acc; color: $acc; font-weight: 600; }

/* 命中页导航：轻量文字按钮 */
QPushButton#navBtn { background: transparent; border: none; border-radius: 6px; padding: 4px 10px; color: $ink3; font-size: 12px; }
QPushButton#navBtn:hover { background: $hover; color: $ink1; }
QPushButton#navBtn:disabled { color: $ink4; background: transparent; }
QLabel#pageLabel { color: $ink3; font-size: 12px; font-family: "Consolas", "Microsoft YaHei UI"; background: transparent; }

/* 状态栏 */
QStatusBar#statusBar { background: $panel2; border-top: 1px solid $bd2; color: $ink3; }
QStatusBar#statusBar QLabel { color: $ink3; font-size: 12px; background: transparent; }
QLabel#kbd { color: $ink2; background: $field; border: 1px solid $bd2; border-bottom: 2px solid $bd2; border-radius: 5px; padding: 1px 7px; font-size: 10.5px; font-weight: 600; font-family: "Consolas", "Microsoft YaHei UI"; }
QLabel#verShield { color: $grn; font-size: 11.5px; font-weight: 600; background: transparent; padding: 0 6px; }
QLabel#hotkeyLabel { color: $ink3; background: $field; border: 1px solid $bd2; border-radius: 6px; padding: 2px 9px; font-size: 11px; font-family: "Consolas", "Microsoft YaHei UI"; }
QLabel#hotkeyLabel:hover { color: $acc; border-color: $bd; }
QLabel#navDot { color: #ff453a; font-size: 13px; font-weight: 700; background: transparent; }

/* 索引状态：单色阶段胶囊 + 克制的自绘进度轨 */
QLabel#indexPhase {
  color: $acc; background: rgba($hl_r,$hl_g,$hl_b,0.12);
  border: 1px solid rgba($hl_r,$hl_g,$hl_b,0.24); border-radius: 7px;
  padding: 2px 7px; font-size: 10.5px; font-weight: 700;
}
QProgressBar#indexBar { background: transparent; border: none; min-height: 8px; max-height: 8px; }
QLabel#indexCount { color: $ink2; font-size: 11.5px; font-weight: 600; padding: 0 3px; background: transparent; font-family: "Consolas", "Microsoft YaHei UI"; }
QLabel#pctLabel {
  color: $acc; background: rgba($hl_r,$hl_g,$hl_b,0.10);
  border-radius: 6px; font-size: 11px; font-weight: 700; padding: 1px 5px; font-family: "Consolas", "Microsoft YaHei UI";
}
QLabel#statusDot { color: $grn; font-size: 13px; padding: 0 2px 0 4px; background: transparent; }
QFrame#typeRail { background: transparent; }

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

/* 分隔条 */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }
QSplitter::handle:hover { background: rgba($hl_r,$hl_g,$hl_b,0.12); border-radius: 3px; }

/* 滚动条：细轨道，无系统箭头，hover/拖动清晰反馈 */
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px 1px; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 1px 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 4px; min-height: 32px; }
QScrollBar::handle:horizontal { background: $scroll; border-radius: 4px; min-width: 32px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: $scrollh; }
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed { background: $acc; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: transparent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; background: transparent; }
QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal { width: 0; height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }

/* 右键菜单 */
QMenu { background: $win; border: 1px solid $bd; border-radius: 11px; padding: 6px; }
QMenu::item { padding: 7px 24px 7px 14px; border-radius: 7px; color: $ink1; font-size: 12.5px; }
QMenu::item:selected { background: $hover; }
QMenu::item:disabled { color: $ink4; }
QMenu::indicator { width: 14px; height: 14px; }
QMenu::separator { height: 1px; background: $bd2; margin: 4px 6px; }

/* 次级窗口通用控件 */
QLineEdit {
  background: $field; border: 1.5px solid transparent; border-radius: 7px;
  padding: 5px 10px; color: $ink1; selection-background-color: $acc; selection-color: $acctext;
}
QLineEdit:hover { background: $hover; }
QLineEdit:focus { border-color: $acc; background: $panel; }
QLineEdit:disabled, QLineEdit:read-only { color: $ink3; background: $selblur; }
QPlainTextEdit, QTextEdit {
  background: $field; color: $ink1; border: 1px solid $bd2; border-radius: 9px;
  padding: 8px; selection-background-color: $acc; selection-color: $acctext;
}
QPlainTextEdit:focus, QTextEdit:focus { border-color: $acc; }
QPlainTextEdit:read-only, QTextEdit:read-only { background: $panel2; color: $ink2; }
QListWidget {
  background: $field; border: 1px solid $bd2; border-radius: 10px;
  outline: 0; padding: 4px;
}
QListWidget::item { color: $ink1; padding: 7px 9px; border-radius: 6px; }
QListWidget::item:hover { background: $hover; }
QListWidget::item:selected { background: $sel; color: $ink1; }
QCheckBox { color: $ink1; spacing: 7px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1.5px solid $bd; border-radius: 5px; background: $field; }
QCheckBox::indicator:hover { border-color: rgba($hl_r,$hl_g,$hl_b,0.58); }
QCheckBox::indicator:checked { background: $acc; border-color: $acc; image: url("$check_icon"); }
QCheckBox::indicator:disabled { background: $selblur; border-color: $bd2; }
QCheckBox:focus { color: $acc; }

QToolButton { background: transparent; color: $ink2; border: 1px solid transparent; border-radius: 7px; padding: 5px; }
QToolButton:hover { background: $hover; color: $ink1; }
QToolButton:pressed, QToolButton:checked { background: $sel; color: $acc; }

QProgressBar { background: $selblur; border: none; border-radius: 4px; text-align: center; color: $ink2; }
QProgressBar::chunk { background: $acc; border-radius: 4px; }

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
             "scroll", "scrollh", "hl_r", "hl_g", "hl_b", "radius", "canvas")


def _qss_asset(name: str) -> str:
    """Return a Qt-style URL that works from source and a PyInstaller bundle."""
    return resource_path("assets", name).resolve().as_posix()


def build_qss(theme: str) -> str:
    t = TOKENS.get(theme, TOKENS["atelier"])
    values = {k: t[k] for k in _QSS_KEYS}
    values.update(
        combo_arrow=_qss_asset(
            "ui-chevron-dark.svg" if t.get("is_light", False) else "ui-chevron-light.svg"
        ),
        check_icon=_qss_asset("ui-check.svg"),
    )
    return _QSS.substitute(values)


def tok(theme: str) -> dict:
    return TOKENS.get(theme, TOKENS["atelier"])


def highlight_css(theme: str) -> str:
    """结果片段命中词高亮：静白=荧光笔黄底深字；旧玻璃主题=半透明强调底。"""
    t = tok(theme)
    return (f"background:{t['hl_bg']};"
            f"border-radius:3px;font-weight:700;color:{t['hl_fg']};padding:0 1px;")
