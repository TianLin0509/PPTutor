"""主题体系：方案 D 通透玻璃（晨雾 / 暗夜，默认晨雾）+ 静白工作室 + 旧极光玻璃 10 套。

方案 D「通透玻璃」（2026-11-25 田哥拍板落地）：AuroraCentral 按 appbg + blobs 画静态光团，
面板是半透明 rgba，不用任何实时模糊；浮卡留缝只对新晨雾 / 暗夜与静白 / 静黑开启，
旧 10 套玻璃保持 14c78a1 的贴边。暗夜实心按钮与选中轨用独立深蓝 accfill（白字 ≥5.5:1）。

方向 A「静白工作室」核心纪律（2026-07-16 定稿）：
- 蓝色预算：同屏强调色 ≤3 处（选中指示条 / 主按钮 / 焦点环），其余一律灰阶。
- 命中词荧光笔：hl_bg/hl_fg token（静白=荧光黄底深字；旧玻璃主题=原半透明强调底）。
- 不透明面板（panel/panel2/field 实色）、无极光光团（blobs=[]，AuroraCentral 只铺纯色）。
- 控件语言三统一：圆角三档（卡 12 / 控件 8 / 小件 5）、hover 只变底色、焦点环同一根。

布局沿用主窗四栏 widget 结构；旧 10 套玻璃主题 token 不动，仅共享打磨后的 QSS 模板。
接口 build_qss / tok / highlight_css 与旧版完全一致。
"""
from __future__ import annotations

import re
from string import Template

from ..config import resource_path, sanitize_font_family


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
    d["rail"] = d["panel2"]  # v2.0 侧栏底色：玻璃主题沿用次级面板
    return d


def _mk_atelier(light: bool) -> dict:
    """静白 / 静黑：不透明层级、无光团、荧光黄命中、灰阶徽章。"""
    if light:
        # v2.2 对齐 AI HUB-H「冷白工作室」（2026-11-21，renderer/styles/hw-tokens.css）：
        # 页底冷灰 #F1F3F5、白卡、鸿蒙蓝 #0A59F7、输入槽 #EDEFF2、选中淡蓝 #E7EFFE
        acc, accd = "#0A59F7", "#0047D9"
        d = dict(
            win="#F1F3F5", appbg="#F1F3F5", base="transparent", blobs=[],
            panel="#FFFFFF", panel2="#FFFFFF", field="#EDEFF2", rail="#FFFFFF",
            hover="rgba(24,36,49,0.05)", sel="#E7EFFE", selblur="rgba(24,36,49,0.06)",
            bd="#E2E6EA", bd2="#ECEEF1",
            ink1="#182431", ink2="#3A4652", ink3="#6E7781", ink4="#A2A8AF",
            acc=acc, accd=accd, acctext="#FFFFFF", grn="#3A9E42",
            scroll="rgba(24,36,49,0.26)", scrollh="rgba(24,36,49,0.46)",
            canvas="#F7F8FA", is_light=True,
            hl_bg="#FFF1B8", hl_fg="#182431",  # 命中词：HUB-H 搜索高亮同色
        )
    else:
        acc, accd = "#0A84FF", "#3D9BFF"
        d = dict(
            win="#1B1B1E", appbg="#1B1B1E", base="transparent", blobs=[],
            panel="#252529", panel2="#232327", field="#2E2E33", rail="#202024",
            hover="rgba(255,255,255,0.06)", sel="rgba(10,132,255,0.20)", selblur="rgba(255,255,255,0.07)",
            bd="rgba(255,255,255,0.11)", bd2="rgba(255,255,255,0.065)",
            ink1="#F5F5F7", ink2="#D0D0D5", ink3="#98989F", ink4="#A0A0AA",
            acc=acc, accd=accd, acctext="#FFFFFF", grn="#32D74B",
            scroll="rgba(255,255,255,0.24)", scrollh="rgba(255,255,255,0.44)",
            canvas="#161619", is_light=False,
            hl_bg="rgba(255,214,10,0.22)", hl_fg="#FFE047",
        )
    ar, ag, ab = _rgb(acc)
    d.update(hl_r=str(ar), hl_g=str(ag), hl_b=str(ab), hl_a="0.22", radius="12")
    return d


def _mk_glass(light: bool) -> dict:
    """方案 D 通透玻璃：晨雾（浅）/ 暗夜（深）。

    对比度按「面板叠在最浓光团上」的合成色核算（晨雾约 #F0F5FF，暗夜约 #182344）：
    晨雾 ink3 5.7:1、ink4 4.6:1；暗夜 ink3 6.1:1、ink4 5.2:1。
    实心按钮 / 选中轨用 accfill：晨雾 #0A59F7 白字 5.55:1，暗夜 #2464CD 白字 5.56:1
    （hover #0047D9 7.23:1 / #1F57B5 6.80:1）；亮蓝 acc 只给链接、描边、选中文字。
    """
    if light:
        d = dict(
            win="#EEF3FA", appbg="#EEF3FA", base="transparent",
            blobs=[(0.08, 0.04, 0.66, (138, 182, 255, 175)), (0.92, 0.08, 0.60, (206, 180, 255, 150)),
                   (0.58, 1.02, 0.76, (140, 224, 212, 135)), (0.36, 0.55, 0.50, (190, 210, 255, 80))],
            panel="rgba(255,255,255,0.82)", panel2="rgba(255,255,255,0.96)", field="rgba(226,232,243,0.72)",
            rail="rgba(255,255,255,0.82)",
            hover="rgba(30,60,120,0.06)", sel="rgba(10,89,247,0.11)", selblur="rgba(30,50,90,0.07)",
            bd="rgba(40,60,100,0.18)", bd2="rgba(40,60,100,0.09)", edge="rgba(36,56,100,0.08)",
            ink1="#121A2A", ink2="#2F3A4E", ink3="#566176", ink4="#666F80",
            acc="#0A59F7", accd="#0047D9", accfill="#0A59F7", accfillh="#0047D9", acctext="#FFFFFF",
            grn="#3A9E42", scroll="rgba(24,36,49,0.26)", scrollh="rgba(24,36,49,0.46)",
            canvas="#F7F9FD", is_light=True, hl_bg="#FFF1B8", hl_fg="#121A2A",
        )
    else:
        d = dict(
            win="#0B1020", appbg="#0B1020", base="transparent",
            blobs=[(0.10, 0.04, 0.66, (40, 92, 255, 120)), (0.92, 0.10, 0.60, (124, 58, 237, 95)),
                   (0.60, 1.02, 0.76, (0, 150, 170, 85)), (0.34, 0.58, 0.50, (59, 130, 246, 45))],
            panel="rgba(24,30,48,0.78)", panel2="rgba(28,35,56,0.96)", field="rgba(255,255,255,0.08)",
            rail="rgba(24,30,48,0.78)",
            hover="rgba(255,255,255,0.06)", sel="rgba(61,139,255,0.20)", selblur="rgba(255,255,255,0.07)",
            bd="rgba(255,255,255,0.16)", bd2="rgba(255,255,255,0.08)", edge="rgba(255,255,255,0.08)",
            ink1="#F2F5FB", ink2="#CDD4E2", ink3="#9AA3B6", ink4="#8E97AA",
            acc="#3D8BFF", accd="#5C9DFF", accfill="#2464CD", accfillh="#1F57B5", acctext="#FFFFFF",
            grn="#32D74B", scroll="rgba(255,255,255,0.24)", scrollh="rgba(255,255,255,0.44)",
            canvas="#161B2C", is_light=False, hl_bg="rgba(255,214,10,0.22)", hl_fg="#FFE047",
        )
    ar, ag, ab = _rgb(d["acc"])
    d.update(hl_r=str(ar), hl_g=str(ag), hl_b=str(ab), hl_a="0.22", radius="12")
    return d


# 方案 D 两套在前，静白/静黑（方向 A）其次；旧 10 套：(fx,fy,fr,(r,g,b,a)) 极光光团供 central 自绘
TOKENS: dict[str, dict] = {
    "mist_glass": _mk_glass(True),
    "night_glass": _mk_glass(False),
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

for _t in TOKENS.values():  # 旧主题补齐方案 D 新键（= 原值，QSS 字节不变）
    _t.setdefault("accfill", _t["acc"])
    _t.setdefault("accfillh", _t["accd"])
    _t.setdefault("edge", _t["bd2"])
del _t

THEMES: list[tuple[str, str]] = [
    ("mist_glass", "晨雾玻璃"), ("night_glass", "暗夜玻璃"),
    ("atelier", "静白"), ("atelier_dark", "静黑"),
    ("aurora", "极光玻璃"), ("cinema", "胶片放映厅"), ("cyber", "赛博霓虹"), ("ocean", "深海极光"),
    ("magma", "熔岩黄昏"), ("forest", "森林晨雾"), ("sakura", "樱花粉黛"), ("midnight", "午夜电蓝"),
    ("graphite", "石墨极简"), ("cloud", "云白晨光"),
]

#: 方案 D 默认首选主题（新装用户；已存过主题的用户保持原选择）
DEFAULT_THEME = "mist_glass"
#: 设置页 / 欢迎页主推的四套；其余收进「更多主题」
MAIN_THEMES: tuple[str, ...] = ("mist_glass", "night_glass", "atelier", "atelier_dark")
MORE_THEMES: tuple[str, ...] = tuple(n for n, _ in THEMES if n not in MAIN_THEMES)
#: 深浅快捷切换的配对（旧 10 套切到晨雾 / 暗夜）
DARK_LIGHT_PAIR = {"mist_glass": "night_glass", "night_glass": "mist_glass",
                   "atelier": "atelier_dark", "atelier_dark": "atelier"}

#: 方案 D 浮卡玻璃：页头胶囊、列表 / 预览分岛、药丸轨（只这两套；旧 10 套保持贴边）
FLOAT_GLASS_THEMES = frozenset({"mist_glass", "night_glass"})

# 控件语言（方向 A）：圆角三档 卡$radius/控件8/小件5；hover 只变底色；焦点环统一 1.5px 强调色。
_QSS = Template("""
* { font-family: $font_family; }
QWidget { background: $base; color: $ink1; font-size: 13px; }
QMainWindow { background: $win; }
QWidget#central { background: transparent; }
QToolTip { background: $panel; color: $ink1; border: 1px solid $bd; border-radius: 8px; padding: 6px 10px; }

/* 合一工具栏（无边框窗口自绘标题栏：品牌 + 搜索 + 筛选 + 图标按钮 + 窗口控制） */
QWidget#glassTitle { background: $panel; border-bottom: 1px solid $bd2; }
QWidget#mainColumn { background: $panel; border-radius: $col_radius; }
QLabel#gtDot { color: $acc; font-size: 12px; background: transparent; }
QLabel#gtName { color: $ink1; font-size: 15px; font-weight: 700; background: transparent; }
QLabel#gtVer { color: $ink4; font-size: 11px; font-family: $mono_family; background: transparent; }
QLabel#gtTheme { color: $ink3; font-size: 12px; background: transparent; }
QPushButton#updateChip { background: $accfill; color: #FFFFFF; border: none; border-radius: 8px; padding: 6px 10px; font-size: 12px; font-weight: 600; }
QPushButton#updateChip:hover { background: $accfillh; }
QPushButton#updateChip:disabled { background: rgba($hl_r,$hl_g,$hl_b,0.45); color: $ink3; }
QPushButton#winMin, QPushButton#winMax, QPushButton#winClose { background: transparent; border: none; border-radius: 0; color: $ink3; font-size: 13px; padding: 0; }
QPushButton#winMin:hover, QPushButton#winMax:hover { background: $hover; color: $ink1; }
QPushButton#winClose:hover { background: #E81123; color: #FFFFFF; }

/* 左侧导航栏：品牌 + 页面切换（搜索/概览/版本/健康）+ 工具（截图/转字/素材/报告/设置）+ 主题 */
QWidget#navRail { background: $rail; $rail_frame }
QWidget#railBrand { background: transparent; }
QLabel#railLogo { background: transparent; }
QToolButton#railBtn {
  background: transparent; border: none; border-radius: 16px;
  padding: 5px 0 4px 0; color: $ink2; font-size: 11.5px;
}
QToolButton#railBtn:hover { background: $hover; color: $ink1; }
QToolButton#railBtn:pressed { background: $sel; }
QToolButton#railBtn:checked { background: $rail_on_bg; color: $rail_on_fg; font-weight: 600; }
QToolButton#railBtn:focus { border: none; }
QLabel#railBadge { background: transparent; border: none; }
QLabel#railLogoMark { background: transparent; }
QWidget#pageHead { background: transparent; }
QLabel#pageHeadTitle { color: $ink1; font-size: 19px; font-weight: 700; background: transparent; }
QLabel#pageHeadSub { color: $ink3; font-size: 12.5px; background: transparent; }
QFrame#railSep { background: $bd2; border: none; }
QLabel#railDot { background: #F04438; border-radius: 4px; }

/* 顶栏（合一后仅承载搜索语法提示行，默认隐藏） */
QWidget#topBar { background: $panel; border: none; }

/* 搜索框：灰槽内嵌，聚焦时抬为面板底 + 焦点环（macOS 搜索语言） */
QLineEdit#searchBox {
  background: $field; border: 1.5px solid transparent; border-radius: 15px;
  padding: 0 10px; font-size: 14.5px; color: $ink1; selection-background-color: $acc; selection-color: $acctext;
}
QLineEdit#searchBox:hover { border-color: $bd; }
QLineEdit#searchBox:focus { border: 1.5px solid $acc; background: $panel; }
QLabel#queryHint { color: $ink3; font-size: 11.5px; background: transparent; padding: 4px 4px 2px 14px; }

/* 下拉：与按钮同形制（控件圆角 8） */
QComboBox {
  background: $field; border: 1px solid transparent; border-radius: 10px;
  padding: 5px 28px 5px 12px; color: $ink2;
}
QComboBox:hover { background: $bd; color: $ink1; }
QComboBox:on { border-color: $acc; color: $ink1; }
QComboBox:focus { border-color: $acc; }
QComboBox:disabled { color: $ink4; background: $selblur; }
QComboBox::drop-down { border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: top right; }
QComboBox::down-arrow { image: url("$combo_arrow"); width: 11px; height: 7px; }
QComboBox QAbstractItemView {
  background: $panel; border: 1px solid $bd; border-radius: 12px; padding: 5px;
  selection-background-color: $sel; selection-color: $ink1; outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 28px; padding: 4px 10px; border-radius: 8px; }
QComboBox QAbstractItemView::item:hover { background: $hover; color: $ink1; }
QComboBox QAbstractItemView::item:selected { background: $sel; color: $ink1; }

/* 按钮：默认 = 面板底描边；hover 只变底色；primary 是全界面唯一实心强调 */
QPushButton {
  background: $field; border: 1px solid transparent; border-radius: 14px; padding: 7px 16px; color: $ink1;
}
QPushButton:hover { background: $bd; }
QPushButton:pressed { background: $sel; color: $acc; }
QPushButton:disabled { color: $ink4; background: transparent; border-color: $bd2; }
QPushButton#primary { background: $accfill; border: 1px solid $accfill; color: $acctext; font-weight: 600; }
QPushButton#primary:hover { background: $accfillh; border-color: $accfillh; }
QPushButton#primary:pressed { background: $accfillh; border-color: $accfillh; }
QPushButton#primary:disabled { background: $selblur; border-color: transparent; color: $ink4; }
QPushButton#ghost { background: transparent; border: none; border-radius: 14px; color: $ink2; padding: 5px 10px; }
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
QPushButton#chip { background: $field; border: 1px solid transparent; border-radius: 14px; padding: 0 12px; color: $ink2; font-size: 12px; }
QPushButton#chip:hover { background: $hover; color: $ink2; }
QPushButton#chip:checked { background: rgba($hl_r,$hl_g,$hl_b,0.14); border-color: $acc; color: $acc; }

/* 结果顶栏条件行：已选条件 chip（点 ✕ 移除，淡蓝选中态）+ 虚线「+ 筛选」入口 */
QWidget#facetBar { background: transparent; }
QPushButton#facetActiveChip { background: rgba($hl_r,$hl_g,$hl_b,0.14); border: 1px solid $acc; border-radius: 12px; padding: 4px 12px; color: $acc; font-size: 12px; }
QPushButton#facetActiveChip:hover { background: rgba($hl_r,$hl_g,$hl_b,0.22); }
QPushButton#facetAdd { background: $field; border: 1px solid transparent; border-radius: 14px; padding: 0 12px; color: $ink2; font-size: 12px; }
QPushButton#facetAdd:hover { background: $hover; color: $ink2; }

/* 结果列表（在 listPane 卡内，透明） */
QListWidget#resultList { background: transparent; border: none; outline: 0; padding: 6px 4px; }
QListWidget#resultList::item { border: none; margin: 0; padding: 0; background: transparent; }
QListWidget#resultList::item:selected { background: transparent; }

/* 预览卡 */
QWidget#previewPanel { background: $panel; border: none; border-radius: 0; }
QWidget#previewHeadBar { background: transparent; }
QLabel#previewImage { background: $canvas; border: 1px solid $bd2; border-radius: 8px; color: $ink3; }
QFrame#card { background: $panel; border: 1px solid $bd2; border-radius: 16px; }
QLabel#pathLabel { color: $ink1; font-size: 18px; font-weight: 700; background: transparent; }
QLabel#metaLabel { color: $ink3; font-size: 12px; background: transparent; }
QPushButton#headerBtn { background: $field; border: 1px solid transparent; border-radius: 14px; padding: 5px 12px; color: $ink2; font-size: 12px; }
QPushButton#headerBtn:hover { background: $bd; color: $ink1; }
QPushButton#linkBtn { background: transparent; border: none; border-radius: 6px; padding: 3px 8px; color: $ink3; font-size: 11.5px; font-weight: 600; }
QPushButton#linkBtn:hover { background: $hover; color: $acc; }
QPushButton#detailAction {
  background: $field; border: 1px solid transparent; border-radius: 12px; padding: 4px 12px;
  color: $ink2; font-size: 12px; font-weight: 600;
}
QPushButton#detailAction:hover { background: $bd; color: $ink1; }
QPushButton#detailAction:checked { background: $sel; border-color: $acc; color: $acc; }

/* 左列表卡 */
QWidget#listPane { background: $panel; border: none; border-radius: 0; }
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

/* 详情区（内嵌预览卡的四 Tab 容器：透明底，卡片样式由外层 previewPanel 提供） */
QWidget#detailPanel { background: transparent; border: none; }
/* 次级窗口统一卡片化 Tab；主界面 detailTabs 保留紧凑下划线特例 */
QTabWidget::pane { background: $panel2; border: 1px solid $bd2; border-radius: 16px; top: -1px; }
QTabBar::tab {
  background: transparent; color: $ink3; padding: 8px 14px; margin: 0 3px 4px 0;
  font-size: 12.5px; font-weight: 600; border: 1px solid transparent; border-radius: 14px;
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
QLabel#detailPath { color: $ink3; font-size: 11.5px; background: transparent; }
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
QPushButton#verBtnPri { background: $accfill; border: 1px solid $accfill; border-radius: 5px; padding: 2px 9px; color: $acctext; font-size: 11px; font-weight: 600; }
QPushButton#verPreviewBtn { background: transparent; border: 1px solid $bd2; border-radius: 5px; padding: 3px 10px; color: $ink2; font-size: 11px; }
QPushButton#verPreviewBtn:hover { background: $hover; color: $ink1; }
QPushButton#outlineItem { background: transparent; border: none; text-align: left; padding: 4px 6px; color: $ink2; font-size: 11.5px; border-radius: 5px; }
QPushButton#outlineItem:hover { background: $hover; color: $acc; }
QLabel#listHead { color: $ink3; font-size: 11.5px; font-weight: 600; background: transparent; }
QComboBox#sortCombo, QComboBox#sortSecondary { background: $field; border: 1px solid transparent; border-radius: 14px; padding: 0 22px 0 10px; color: $ink2; font-size: 12px; }
QComboBox#sortCombo:hover, QComboBox#sortSecondary:hover { background: $bd; color: $ink1; }
QComboBox#sortCombo QAbstractItemView { background: $win; border: 1px solid $bd; border-radius: 8px; selection-background-color: $sel; selection-color: $ink1; }

/* 搜索历史下拉 */
QListView#historyPopup { background: $win; border: 1px solid $bd2; border-radius: 8px; padding: 4px; outline: 0; color: $ink1; }
QListView#historyPopup::item { padding: 6px 12px; border-radius: 6px; }
QListView#historyPopup::item:selected { background: $sel; color: $ink1; }

/* 零结果引导面板 */
QWidget#emptyHint { background: transparent; }
QLabel#emptyIcon { font-size: 38px; background: transparent; }
QLabel#emptyTitle { color: $ink2; font-size: 15px; font-weight: 600; background: transparent; }
QWidget#materialEmpty { background: $panel; border: 1px solid $bd2; border-radius: 16px; }
QLabel#emptyTip { color: $ink3; font-size: 12px; background: transparent; }
QLabel#emptyMeta { color: $ink4; font-size: 11.5px; background: transparent; }
QPushButton#suggBtn { background: $field; border: 1px solid transparent; border-radius: 15px; padding: 7px 18px; color: $ink2; font-size: 12.5px; font-weight: 500; }
QPushButton#suggBtn:hover { background: $sel; color: $acc; }

/* 结果卡片缩略图 */
QLabel#cardThumb { background: $field; border: 1px solid $bd; border-radius: 5px; }

/* 版本组展开器（「N 个历史版本 ▾」）：灰静默，hover 才提示可点 */
QToolButton#verExpand { background: transparent; border: 1px solid $bd2; border-radius: 12px; padding: 1px 9px; color: $ink3; font-size: 11px; font-weight: 600; }
QToolButton#verExpand:hover { background: $hover; border-color: $bd; color: $acc; }

/* 命中页缩略图 / 页码分段：选中白底浮起（浅）或强调描边 */
QToolButton#thumb { background: $field; border: 1px solid transparent; border-radius: 14px; padding: 0; color: $ink2; font-size: 12px; }
QToolButton#thumb:hover { background: $hover; color: $ink1; }
QToolButton#thumb:checked { background: $sel; border: 1px solid $acc; color: $acc; font-weight: 600; }

/* 命中页导航：轻量文字按钮 */
QPushButton#navBtn { background: $field; border: 1px solid transparent; border-radius: 14px; padding: 4px 14px; color: $ink2; font-size: 12px; }
QPushButton#navBtn:hover { background: $bd; color: $ink1; }
QPushButton#navBtn:disabled { color: $ink4; background: $field; border-color: transparent; }
QLabel#pageLabel { color: $ink3; font-size: 12px; font-family: $mono_family; background: transparent; }

/* 状态栏 */
QStatusBar#statusBar { background: transparent; border: none; color: $ink3; }
QStatusBar#statusBar::item { border: none; }
QStatusBar#statusBar QLabel { color: $ink3; font-size: 12px; background: transparent; }
QStatusBar#statusBar QLabel#lastIndexLabel { padding-left: 10px; }
QLabel#kbd { color: $ink2; background: $field; border: 1px solid $bd2; border-bottom: 2px solid $bd2; border-radius: 5px; padding: 1px 7px; font-size: 10.5px; font-weight: 600; font-family: $mono_family; }
QStatusBar#statusBar QLabel#verShield { color: $ink3; font-size: 12px; font-weight: 400; background: transparent; padding: 0 10px; }
QStatusBar#statusBar QLabel#hotkeyLabel { color: $ink3; background: transparent; border: none; border-radius: 6px; padding: 3px 12px 3px 8px; font-size: 12px; }
QStatusBar#statusBar QLabel#hotkeyLabel:hover { color: $acc; background: $hover; }
QLabel#navDot { color: #ff453a; font-size: 13px; font-weight: 700; background: transparent; }

/* 索引状态：单色阶段胶囊 + 克制的自绘进度轨 */
QStatusBar#statusBar QLabel#indexPhase {
  color: $acc; background: rgba($hl_r,$hl_g,$hl_b,0.10);
  border: none; border-radius: 9px;
  padding: 2px 9px; margin-left: 10px; font-size: 11.5px; font-weight: 600;
}
QProgressBar#indexBar { background: transparent; border: none; min-height: 8px; max-height: 8px; }
QStatusBar#statusBar QLabel#indexCount { color: $ink2; font-size: 12px; font-weight: 400; padding: 0 2px; background: transparent; }
QStatusBar#statusBar QLabel#pctLabel {
  color: $acc; background: transparent; font-size: 12px; font-weight: 600; padding: 0 2px;
}
QStatusBar#statusBar QLabel#statusDot { color: $grn; font-size: 9px; padding: 0 0 0 12px; background: transparent; }
QStatusBar#statusBar QLabel#lastIndexLabel { color: $ink4; font-size: 12px; background: transparent; }
QFrame#typeRail { background: transparent; }

/* 仪表盘首屏（零搜索默认视图） */
QWidget#dashView { background: transparent; }
QFrame#dashCard, QFrame#dashCardLink { background: $panel; border: 1px solid $bd2; border-radius: 16px; }
QFrame#dashCardLink:hover { border-color: rgba($hl_r,$hl_g,$hl_b,0.55); background: $sel; }
QLabel#dashTitle { color: $ink1; font-size: 22px; font-weight: 700; background: transparent; }
QLabel#dashSub { color: $ink3; font-size: 12.5px; background: transparent; }
QLabel#kpiNum { color: $ink1; font-size: 28px; font-weight: 700; font-family: $mono_family; background: transparent; }
QLabel#kpiNumAcc { color: $acc; font-size: 28px; font-weight: 700; font-family: $mono_family; background: transparent; }
QLabel#kpiAction { color: $acc; font-size: 17px; font-weight: 700; background: transparent; padding: 6px 0 5px 0; }
QLabel#kpiLab { color: $ink2; font-size: 12.5px; font-weight: 600; background: transparent; }
QLabel#kpiSub { color: $ink4; font-size: 11px; background: transparent; }
QLabel#dashCardT { color: $ink1; font-size: 14px; font-weight: 700; background: transparent; }
QLabel#legName { color: $ink2; font-size: 12px; background: transparent; }
QLabel#legPc { color: $ink3; font-size: 12px; font-weight: 700; font-family: $mono_family; background: transparent; }
QFrame#dashRec { background: $canvas; border: 1px solid transparent; border-radius: 12px; }
QFrame#dashRec:hover { background: $sel; border-color: rgba($hl_r,$hl_g,$hl_b,0.35); }
QLabel#recName { color: $ink1; font-size: 12px; font-weight: 600; background: transparent; }
QLabel#recTime { color: $ink4; font-size: 11px; background: transparent; }
QLabel#recVer { color: $acc; font-size: 11px; font-weight: 700; background: transparent; }
QLabel#healthLink { background: transparent; }  /* 库体检可点病灶条目：颜色由内联 acc token 控制，hover 下划线走 eventFilter */

/* 分隔条 */
QSplitter::handle { background: transparent; }
QSplitter#mainSplit::handle:horizontal { width: 5px; background: $panel; border-left: 1px solid $bd2; }
QSplitter#mainSplit::handle:horizontal:hover { background: rgba($hl_r,$hl_g,$hl_b,0.10); }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }
QSplitter::handle:hover { background: rgba($hl_r,$hl_g,$hl_b,0.12); border-radius: 3px; }

/* 滚动条：细轨道，无系统箭头，hover/拖动清晰反馈 */
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 1px; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 1px 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 3px; min-height: 32px; }
QScrollBar::handle:horizontal { background: $scroll; border-radius: 3px; min-width: 32px; }
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
QMenu { background: $panel; border: 1px solid $bd; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 7px 24px 7px 14px; border-radius: 8px; color: $ink1; font-size: 12.5px; }
QMenu::item:selected { background: $hover; }
QMenu::item:disabled { color: $ink4; }
QMenu::indicator { width: 14px; height: 14px; }
QMenu::separator { height: 1px; background: $bd2; margin: 4px 6px; }

/* 次级窗口通用控件 */
QLineEdit {
  background: $field; border: 1.5px solid transparent; border-radius: 10px;
  padding: 5px 10px; color: $ink1; selection-background-color: $acc; selection-color: $acctext;
}
QLineEdit:hover { border-color: $bd; }
QLineEdit:focus { border-color: $acc; background: $panel; }
QLineEdit:read-only { color: $ink2; }
QLineEdit:disabled { color: $ink4; background: $field; border-color: transparent; }

/* 数字框：与下拉同形制，箭头用同一枚 chevron */
QAbstractSpinBox {
  background: $field; border: 1.5px solid transparent; border-radius: 10px;
  padding: 4px 24px 4px 10px; color: $ink1; selection-background-color: $acc; selection-color: $acctext;
}
QAbstractSpinBox:hover { border-color: rgba($hl_r,$hl_g,$hl_b,0.45); }
QAbstractSpinBox:focus { border-color: $acc; background: $panel; }
QAbstractSpinBox:disabled { color: $ink4; background: $field; border-color: $bd2; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { border: none; background: transparent; width: 20px; subcontrol-origin: border; }
QAbstractSpinBox::up-button { subcontrol-position: top right; margin: 2px 2px 0 0; border-top-right-radius: 6px; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; margin: 0 2px 2px 0; border-bottom-right-radius: 6px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: $hover; }
QAbstractSpinBox::up-arrow { image: url("$spin_up"); width: 9px; height: 6px; }
QAbstractSpinBox::down-arrow { image: url("$combo_arrow"); width: 9px; height: 6px; }
QPlainTextEdit, QTextEdit {
  background: $panel; color: $ink1; border: 1px solid $bd; border-radius: 10px;
  padding: 8px; selection-background-color: $acc; selection-color: $acctext;
}
QPlainTextEdit:focus, QTextEdit:focus { border-color: $acc; }
QPlainTextEdit:read-only, QTextEdit:read-only { background: $panel; color: $ink2; }
QListWidget {
  background: $panel; border: 1px solid $bd2; border-radius: 16px;
  outline: 0; padding: 4px;
}
QListWidget::item { color: $ink1; padding: 7px 10px; border-radius: 10px; }
QListWidget::item:hover { background: $hover; }
QListWidget::item:selected { background: $sel; color: $acc; }
QCheckBox { color: $ink1; spacing: 7px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1.5px solid $ink4; border-radius: 5px; background: $panel; }
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
  background: $panel; color: $ink1;
  border: 1px solid $bd;
  border-radius: 16px; padding: 9px 20px; font-size: 13px; font-weight: 500;
}

/* 素材库顶部的团队素材包条：白卡，不再用系统灰粗边 */
QFrame#teamPackBar { background: $panel; border: 1px solid $bd2; border-radius: 16px; }
$float_glass_qss""")

# 方案 D 浮卡玻璃的追加样式（只拼给 FLOAT_GLASS_THEMES）：页头胶囊、列表 / 预览分岛、药丸轨、
# 卡片 22 圆角。全部是 rgba 底 + 1px 描边，不用 backdrop-filter / 阴影效果。
_FLOAT_GLASS_QSS = Template("""
/* —— 方案 D 通透玻璃：浮卡 —— */
QWidget#mainColumn { background: transparent; border-radius: 0px; }
QWidget#glassTitle { background: $panel; border: 1px solid $edge; border-radius: 26px; }
QWidget#topBar { background: transparent; }
QWidget#listPane, QWidget#previewPanel { background: $panel; border: 1px solid $edge; border-radius: 22px; }
QSplitter#mainSplit::handle:horizontal { width: 12px; background: transparent; border: none; }
QSplitter#mainSplit::handle:horizontal:hover { background: transparent; }
QFrame#dashCard, QFrame#dashCardLink { background: $panel; border: 1px solid $edge; border-radius: 22px; }
QFrame#card { background: $panel; border: 1px solid $edge; border-radius: 22px; }
QWidget#materialEmpty, QFrame#teamPackBar { background: $panel; border: 1px solid $edge; border-radius: 22px; }
QWidget#pageIsland { background: $panel; border: 1px solid $edge; border-radius: 22px; }
QLabel#toast { background: $panel2; border: 1px solid $bd; border-radius: 18px; }
QLineEdit#searchBox { border-radius: 18px; }
QLineEdit#searchBox:focus { background: $panel2; }
""")

# substitute 只替换字符串 $var，TOKENS 里的 blobs(list) 会被忽略（不影响 QSS）
_QSS_KEYS = ("win", "base", "appbg", "panel", "panel2", "field", "rail", "hover", "sel", "selblur",
             "ink1", "ink2", "ink3", "ink4", "bd", "bd2", "acc", "accd", "acctext", "grn",
             "scroll", "scrollh", "hl_r", "hl_g", "hl_b", "radius", "canvas", "accfill", "accfillh", "edge")

# 界面字体默认字族：与 2026-08 前硬编码值一致；用户覆盖走 ui.json 的 font_family
DEFAULT_FONT_FAMILY = '"Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif'
DEFAULT_MONO_FAMILY = '"Consolas", "Microsoft YaHei UI"'

# build_qss 末尾按倍率统一缩放模板里硬编码的 px 字号（参数化几十个值不现实）
# 数值段只认 1.2 这类合法小数：旧的 [0-9.]+ 会吞下注入进来的 1.2.3px，
# float("1.2.3") 直接 ValueError → 配置持久化后每次启动都在这崩（崩溃循环）
_FONT_SIZE_RE = re.compile(r"font-size:\s*([0-9]+(?:\.[0-9]+)?)px")


def _qss_asset(name: str) -> str:
    """Return a Qt-style URL that works from source and a PyInstaller bundle."""
    return resource_path("assets", name).resolve().as_posix()


def _scale_font_size(m: "re.Match[str]", scale: float) -> str:
    # 吸附到 0.5px 网格，避免 12.075px 这类毛边值；注意 Python round 是银行家舍入，
    # 恰落网格中点的值向偶数取整（如 12.5×0.9=11.25→11 而非 11.5）；整数值不带小数点
    px = round(float(m.group(1)) * scale * 2) / 2
    return f"font-size: {px:g}px"


def _font_family_qss(family: str) -> str:
    """用户字族加引号并回退到内置字族；空串/清洗后为空 = 内置字族（与旧硬编码一致）。

    字族名原样拼进 QSS 的 * 规则：不清洗的话引号/花括号/换行可突围注入任意样式，
    甚至让 Qt 拒收整表（配置已持久化 → 每次重启裸奔）。清洗与 config.set_font_family
    同源（sanitize_font_family），这里再过一道是双保险。
    """
    name = sanitize_font_family(family)
    return f'"{name}", {DEFAULT_FONT_FAMILY}' if name else DEFAULT_FONT_FAMILY


def build_qss(theme: str, font_family: str = "", font_scale: float = 1.0) -> str:
    t = TOKENS.get(theme, TOKENS["atelier"])
    values = {k: t[k] for k in _QSS_KEYS}
    values.update(
        combo_arrow=_qss_asset(
            "ui-chevron-dark.svg" if t.get("is_light", False) else "ui-chevron-light.svg"
        ),
        check_icon=_qss_asset("ui-check.svg"),
        spin_up=_qss_asset(
            "ui-chevron-up-dark.svg" if t.get("is_light", False) else "ui-chevron-up-light.svg"
        ),
        font_family=_font_family_qss(font_family),
        mono_family=DEFAULT_MONO_FAMILY,
        col_radius="16px" if islands(theme) else "0px",
        rail_frame=(f"border: 1px solid {t['edge']}; border-radius: 30px;" if float_glass(theme)
                    else "border: none; border-radius: 20px;" if islands(theme)
                    else f"border: none; border-right: 1px solid {t['bd2']}; border-radius: 0px;"),
        rail_on_bg=t["accfill"] if float_glass(theme) else t["sel"],
        rail_on_fg="#FFFFFF" if float_glass(theme) else t["acc"],
        float_glass_qss=(_FLOAT_GLASS_QSS.substitute({k: t[k] for k in _QSS_KEYS})
                         if float_glass(theme) else ""),
    )
    qss = _QSS.substitute(values)
    if abs(font_scale - 1.0) > 1e-9:
        qss = _FONT_SIZE_RE.sub(lambda m: _scale_font_size(m, font_scale), qss)
    return qss


#: 「灰画布上浮白卡」（导航岛与内容面板留缝、圆角）只给实色主题；玻璃主题的面板半透明，
#: 留缝会在玻璃上再叠一圈深色底（2026-11-24 同事反馈「多加了层黑底」），保持贴边。
ISLAND_THEMES = frozenset({"atelier", "atelier_dark", "mist_glass", "night_glass"})


def islands(theme: str) -> bool:
    return theme in ISLAND_THEMES


def float_glass(theme: str) -> bool:
    """方案 D 浮卡玻璃（晨雾 / 暗夜）：页头胶囊、列表与预览分岛、药丸轨。"""
    return theme in FLOAT_GLASS_THEMES


def tok(theme: str) -> dict:
    return TOKENS.get(theme, TOKENS["atelier"])


def _qt_color_str(v):
    """把 CSS 的 rgb()/rgba() 写法转成 QColor 和 QSS 都认的 #RRGGBB / #AARRGGBB。

    QColor("rgba(…)") 不认，会变成无效色（自绘时画成黑块），外部控件直接拿 token 画图时必须是这种格式。
    """
    if not isinstance(v, str):
        return v
    t = v.strip()
    low = t.lower()
    if not (low.startswith("rgb(") or low.startswith("rgba(")):
        return v
    try:
        parts = [x.strip() for x in t[t.index("(") + 1:t.rindex(")")].split(",")]
        r, g, b = (max(0, min(255, int(round(float(x))))) for x in parts[:3])
        a = 255
        if len(parts) > 3:
            av = float(parts[3])
            a = max(0, min(255, int(round(av * 255 if av <= 1 else av))))
    except (ValueError, IndexError):
        return v
    return f"#{r:02X}{g:02X}{b:02X}" if a == 255 else f"#{a:02X}{r:02X}{g:02X}{b:02X}"


def contract_tokens(theme: str) -> dict:
    """第二阶段合同 §3.2：给素材页（MaterialTabWidget.apply_theme）等外部控件的标准 token。

    返回新 dict，改它不影响主题本身；未知主题回退静白。
    """
    name = theme if theme in TOKENS else "atelier"
    t = TOKENS[name]
    dark = not t.get("is_light", False)
    out = {
        "name": name,
        "label": dict(THEMES).get(name, name),
        "is_dark": dark,
        "is_glass": name not in ("atelier", "atelier_dark"),
        "floating_cards": islands(name),
        "appbg": t["appbg"],
        "blobs": list(t.get("blobs") or []),
        "surface": t["panel"],
        "surface_hover": t["hover"],
        "surface_border": t["edge"],
        "text": t["ink1"],
        "text_secondary": t["ink2"],
        "text_muted": t["ink3"],
        "text_faint": t["ink4"],
        "field": t["field"],
        "selected": t["sel"],
        "accent": t["accfill"],
        "accent_hover": t["accfillh"],
        "accent_text": t["acctext"],
        "accent_soft": t["sel"],
        "link": t["acc"],
        "danger": "#FF453A" if dark else "#CF0A2C",
        "warn": "#FF9F0A" if dark else "#E8780C",
        "ok": t["grn"],
        "hl_bg": t["hl_bg"],
        "hl_fg": t["hl_fg"],
    }
    # 颜色一律给 QColor 能直接解析的十六进制（含透明度用 #AARRGGBB）
    return {k: (_qt_color_str(v) if k not in ("name", "label", "blobs") else v) for k, v in out.items()}


def highlight_css(theme: str) -> str:
    """结果片段命中词高亮：静白=荧光笔黄底深字；旧玻璃主题=半透明强调底。"""
    t = tok(theme)
    return (f"background:{t['hl_bg']};"
            f"border-radius:3px;font-weight:700;color:{t['hl_fg']};padding:0 1px;")


# ---------- 与操作系统深浅模式脱钩（2026-07-24） ----------
def _solid_qcolor(color: str, bg_hex: str):
    """把 token 颜色（#hex 或 CSS rgba(float alpha)）折算成不透明 QColor。

    半透明值按 alpha 合成到 bg_hex 上——QPalette 角色本身不需要 alpha，
    且 QColor 不接受 CSS 浮点 alpha 写法。
    """
    from PySide6.QtGui import QColor

    c = (color or "").strip()
    if c.startswith("#"):
        return QColor(c)
    if c.startswith("rgba(") and c.endswith(")"):
        r, g, b, a = [p.strip() for p in c[5:-1].split(",")]
        r, g, b, a = int(r), int(g), int(b), float(a)
        br, bg_, bb = _rgb(bg_hex)
        return QColor(round(r * a + br * (1 - a)), round(g * a + bg_ * (1 - a)), round(b * a + bb * (1 - a)))
    if c.startswith("rgb(") and c.endswith(")"):
        r, g, b = [p.strip() for p in c[4:-1].split(",")]
        return QColor(int(r), int(g), int(b))
    return QColor(c)


def build_palette(theme: str):
    """由主题 token 构造全局 QPalette：QSS 覆盖不到的控件（对话框、通用输入框、
    系统弹窗等）回退到应用主题色，而不是操作系统深浅模式的调色板。"""
    from PySide6.QtGui import QPalette

    t = tok(theme)
    win = t["win"]
    window = _solid_qcolor(win, win)
    panel = _solid_qcolor(t["panel"], win)
    field = _solid_qcolor(t["field"], win)
    ink1 = _solid_qcolor(t["ink1"], win)
    ink4 = _solid_qcolor(t["ink4"], win)
    acc = _solid_qcolor(t["acc"], win)
    acctext = _solid_qcolor(t["acctext"], win)

    pal = QPalette()
    pal.setColor(QPalette.Window, window)
    pal.setColor(QPalette.WindowText, ink1)
    pal.setColor(QPalette.Base, field)
    pal.setColor(QPalette.AlternateBase, panel)
    pal.setColor(QPalette.Text, ink1)
    pal.setColor(QPalette.Button, panel)
    pal.setColor(QPalette.ButtonText, ink1)
    pal.setColor(QPalette.ToolTipBase, window)
    pal.setColor(QPalette.ToolTipText, ink1)
    pal.setColor(QPalette.PlaceholderText, ink4)
    pal.setColor(QPalette.Highlight, acc)
    pal.setColor(QPalette.HighlightedText, acctext)
    pal.setColor(QPalette.Link, acc)
    pal.setColor(QPalette.BrightText, acctext)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, ink4)
    return pal


# 首次 apply_to_app 时缓存的原始 app.font：setFont 只在本函数内发生，
# 首次调用时拿到的必是系统默认字体；「恢复默认（内置字体）」时靠它还原，
# 否则旧自定义字体会永久残留在 app.font 上（QSS 之外的对话框/菜单回不去）
_DEFAULT_APP_FONT = None


def apply_to_app(app, theme: str, font_family: str = "", font_scale: float = 1.0) -> None:
    """把应用主题钉死在应用层，与操作系统深浅模式彻底脱钩（三件套）。

    - colorScheme：Qt 6.5+ 起 Windows 默认跟随系统深浅；显式钉住后系统切换不再影响本应用；
    - QPalette：QSS 未覆盖的控件回退到应用 token 色（本机深色主题不再渗进来）；
    - 全局 QSS：既有全覆盖样式表，照旧；font_family/font_scale 来自设置的界面字体项。

    family 非空时额外 app.setFont：兜底 QSS 覆盖不到的控件（对话框、菜单）。
    字号与 QSS 同口径用像素（QFont 第二参是 pointSize，pt≠px 会整体偏大 ~33%）；
    family 为空时恢复首次 apply 缓存的默认字体，而不是把旧自定义值留在原地。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont

    global _DEFAULT_APP_FONT
    if _DEFAULT_APP_FONT is None:
        _DEFAULT_APP_FONT = QFont(app.font())

    t = tok(theme)
    scheme = Qt.ColorScheme.Light if t.get("is_light", False) else Qt.ColorScheme.Dark
    app.styleHints().setColorScheme(scheme)
    app.setPalette(build_palette(theme))
    name = sanitize_font_family(font_family)
    if name:
        font = QFont(name)
        font.setPixelSize(round(13 * font_scale))
        app.setFont(font)
    else:
        app.setFont(QFont(_DEFAULT_APP_FONT))
    app.setStyleSheet(build_qss(theme, font_family, font_scale))


def _qcolor(color: str, fallback: str = "#808080"):
    """token 颜色（#hex / #AARRGGBB / rgba(r,g,b,α) / rgb()）→ QColor；解析不了用 fallback，绝不返回无效色（无效色自绘成黑块）。"""
    from PySide6.QtGui import QColor

    q = QColor(_qt_color_str(color or ""))
    return q if q.isValid() else QColor(fallback)


qcolor = _qcolor  # 自绘控件取 token 颜色统一走这里


def preview_pixmap(theme_name: str, width: int = 150, height: int = 84, ratio: float = 3.0):
    """主题缩略卡（设置外观页 / 欢迎层共用）：底色 + 光团 + 药丸轨 + 半透明面板 + 主按钮。

    与主窗同一画法（aurora_bg.paint_aurora + 面板 rgba），所以缩略图里看到的就是切过去的样子。
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QPainter, QPixmap

    from .aurora_bg import paint_aurora

    t = tok(theme_name)
    pm = QPixmap(round(width * ratio), round(height * ratio))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(ratio, ratio)
    paint_aurora(p, QRectF(0, 0, width, height), t)
    floating = islands(theme_name)
    pad = 6 if floating else 0
    panel = _qcolor(t["panel"])
    edge = _qcolor(t["edge"])
    p.setPen(edge)
    p.setBrush(_qcolor(t["rail"]))
    p.drawRoundedRect(QRectF(pad, pad, 16, height - 2 * pad), 8 if floating else 0, 8 if floating else 0)
    p.setBrush(panel)
    x0 = pad + 16 + (5 if floating else 0)
    p.drawRoundedRect(QRectF(x0, pad, width - x0 - pad, 14), 7 if floating else 0, 7 if floating else 0)
    p.drawRoundedRect(QRectF(x0, pad + 18, width - x0 - pad, height - 2 * pad - 18),
                      7 if floating else 2, 7 if floating else 2)
    p.setPen(Qt.NoPen)
    p.setBrush(_qcolor(t["sel"]))
    p.drawRoundedRect(QRectF(x0 + 8, pad + 26, (width - x0) * 0.45, 5), 2.5, 2.5)
    p.setBrush(_qcolor(t["field"]))
    p.drawRoundedRect(QRectF(x0 + 8, pad + 36, (width - x0) * 0.3, 5), 2.5, 2.5)
    p.setBrush(_qcolor(t["accfill"]))
    p.drawRoundedRect(QRectF(width - pad - 34, height - pad - 14, 26, 8), 4, 4)
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm
