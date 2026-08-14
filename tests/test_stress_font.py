"""界面字体改动的对抗性压测与边界审查用例（2026-08 字体可调评审产出）。

覆盖：_QSS 字族注入面、_FONT_SIZE_RE 缩放确定性与 1.0 幂等（对照 git HEAD）、
apply_to_app 的 app.font 状态一致性、config 非法值回落、设置对话框回落路径。
offscreen 可跑；数据目录走 conftest 的临时目录隔离，不碰真实 LOCALAPPDATA。

评审确认的 4 条缺陷已修复（2026-08-14），对应用例全部转正：
- 字族白名单清洗（sanitize_font_family，config/theme/对话框三处同源）→ QSS 注入失效
- _FONT_SIZE_RE 收紧为合法小数 → 注入的 1.2.3px 不再让 build_qss 抛 ValueError
- apply_to_app 缓存首次 apply 的默认字体 → 恢复默认后 setFont 残留被还原
"""
from __future__ import annotations

import difflib
import random
import re
import subprocess
import types
from pathlib import Path

# 注意：不要在模块级 os.environ.setdefault("QT_QPA_PLATFORM", ...) ——
# pytest 收集期就会 import 本模块，环境变量会污染同进程的其他测试文件。
# 需要 offscreen 时请在命令行显式给：QT_QPA_PLATFORM=offscreen uv run pytest ...

import pytest
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QLabel

from pptx_finder import config
from pptx_finder.ui import theme
from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder.versioning.manager import VersionManager

ROOT = Path(__file__).resolve().parents[1]
THEME_NAMES = [n for n, _ in theme.THEMES]

# 病态/恶意字族名单：引号突围、花括号注入、换行、反斜杠、超长、不存在字体等
PATHOLOGICAL_FAMILIES = [
    "",
    "   ",
    '"',
    '""',
    "a;b",
    "a\nb",
    'x"; } QLabel#gtName { font-size: 99px } /*',  # 任意 QSS 注入
    'x"; } /*',                                    # 注释突围
    'x"; "',                                       # 奇数引号 → 整表被拒
    "x\\",                                         # 反斜杠吃掉收尾引号 → 整表被拒
    "A" * 200,
    "NotARealFontXYZ",                             # 不存在但合法的名字
    "微软雅黑",
    "a\tb",
    "'single'",
    "{}",                                          # 裸花括号
    'x"; } a { font-size: 1.2.3px',                # 畸形 font-size（scale=1.0 不触发缩放，安全）
]
# 实测会击碎整张样式表的载荷（Qt 报 "Could not parse application stylesheet"）。
# 注：制表符 \t 在 QSS 字符串里被 Qt 接受，不在此列（留在病态名单做不崩溃断言）。
SHEET_KILLERS = ['x"; "', "a\nb", "x\\"]


def _load_head_theme():
    """git show 取 HEAD 版 theme.py（只读），改相对 import 后 exec 成独立模块。"""
    proc = subprocess.run(
        ["git", "show", "HEAD:src/pptx_finder/ui/theme.py"],
        cwd=ROOT, capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("git show HEAD 不可用")
    src = proc.stdout.decode("utf-8").replace(
        "from ..config import", "from pptx_finder.config import"
    )
    mod = types.ModuleType("pptx_finder_theme_head")
    exec(compile(src, "theme@HEAD", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def qt_warnings(qapp):
    """收集本轮测试里的 Qt 警告（QSS 解析失败会在这里现形）。"""
    records: list[str] = []
    prev = qInstallMessageHandler(lambda _mode, _ctx, msg: records.append(msg))
    try:
        yield records
    finally:
        qInstallMessageHandler(prev)


def _parse_errors(records: list[str]) -> list[str]:
    return [m for m in records if "parse" in m.lower() and "stylesheet" in m.lower()]


def _polished_gtname() -> QLabel:
    """模板前段规则 QLabel#gtName { font-size:13px; font-weight:700 } 的活体探针。"""
    lab = QLabel("x")
    lab.setObjectName("gtName")
    lab.ensurePolished()
    return lab


@pytest.fixture
def mgr():
    m = VersionManager()
    yield m
    m.stop()


# ---------------------------------------------------------------- 缩放幂等 / 确定性

def test_scale_1_byte_safe_vs_git_head():
    """scale=1.0 + 默认字族：除 3 条有意改的 mono 字族声明外与 HEAD 逐字节一致。

    有意差异（字体功能顺带补的 CJK 回退，不是缩放误伤）：
    gtVer / kpiNum / legPc 三条的 font-family 值。任何 font-size 字节差异即失败。
    """
    head = _load_head_theme()
    allowed_rules = ("QLabel#gtVer", "QLabel#kpiNum", "QLabel#legPc")
    for name in THEME_NAMES:
        old, new = head.build_qss(name), theme.build_qss(name, "", 1.0)
        assert theme.build_qss(name) == new  # 缺省参数 = 显式 1.0
        if old == new:
            continue
        diff = [
            line for line in difflib.unified_diff(
                old.splitlines(), new.splitlines(), lineterm="")
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        assert diff, name
        # diff 行只允许是那 3 条规则，且剥离 font-family 段后 '-' 组与 '+' 组逐字节相等
        # （即这些行里唯一的变化是字族回退链，font-size 等其余声明一个字符都不许动）
        for line in diff:
            assert line[1:].lstrip().startswith(allowed_rules), (
                f"{name} 出现预期外 diff 行: {line[:120]}"
            )
        minus = sorted(re.sub(r"font-family: [^;}]*;?", "", l[1:]) for l in diff if l.startswith("-"))
        plus = sorted(re.sub(r"font-family: [^;}]*;?", "", l[1:]) for l in diff if l.startswith("+"))
        assert minus == plus, f"{name} diff 行除 font-family 外还有变化: {diff}"


# 模板 13 个字号值 × 两档倍率的实测取整表（2026-08-14 实测锁定）。
# 12.5*0.9=11.25 落在 0.5 网格中点，round() 银行家舍入 → 11（注释写"四舍五入"，此处实际不是）。
EXPECTED_SCALE_MAP = {
    0.9: {"9": "8", "10": "9", "10.5": "9.5", "11": "10", "11.5": "10.5",
          "12": "11", "12.5": "11", "13": "11.5", "14.5": "13", "15": "13.5",
          "21": "19", "27": "24.5", "38": "34"},
    1.15: {"9": "10.5", "10": "11.5", "10.5": "12", "11": "12.5", "11.5": "13",
           "12": "14", "12.5": "14.5", "13": "15", "14.5": "16.5", "15": "17",
           "21": "24", "27": "31", "38": "43.5"},
}


def test_scale_mapping_is_deterministic_table():
    for scale, mapping in EXPECTED_SCALE_MAP.items():
        for raw, want in mapping.items():
            m = theme._FONT_SIZE_RE.search(f"font-size: {raw}px")
            got = theme._scale_font_size(m, scale)
            assert got == f"font-size: {want}px", f"{raw}px * {scale}: {got} != {want}"
            # 幂等重入：同输入重算必须同输出
            m2 = theme._FONT_SIZE_RE.search(f"font-size: {raw}px")
            assert theme._scale_font_size(m2, scale) == got


def test_scaled_qss_wellformed_all_themes():
    import re
    for name in THEME_NAMES:
        for scale in (0.9, 1.15):
            qss = theme.build_qss(name, font_scale=scale)
            assert "$" not in qss, f"{name}@{scale} 残留模板变量"
            for m in re.finditer(r"font-size:\s*([0-9.]+)px", qss):
                v = float(m.group(1))
                assert v >= 8, f"{name}@{scale} 字号缩到危险小值 {v}"
                assert abs(v * 2 - round(v * 2)) < 1e-9, f"{name}@{scale} 非 0.5 网格 {v}"
            # margin/padding/radius 等非 font-size 的 px 不得被缩放误伤：
            # 模板 max-height: 8px 在 0.9 下若被误伤会变成 7px/7.5px
            assert "max-height: 8px" in qss, f"{name}@{scale} 误伤了非 font-size 的 px"


# ---------------------------------------------------------------- 注入面

@pytest.mark.parametrize("family", PATHOLOGICAL_FAMILIES)
def test_pathological_family_never_crashes(qapp, qt_warnings, family):
    """病态字族逐一过 build_qss + apply_to_app：不允许抛异常。"""
    qss = theme.build_qss("atelier", font_family=family)  # 不炸
    assert isinstance(qss, str) and "QWidget" in qss
    theme.apply_to_app(qapp, "atelier", family, 1.0)      # 不炸
    _polished_gtname()                                    # polish 路径也不炸


@pytest.mark.parametrize("family", SHEET_KILLERS)
def test_malicious_family_stylesheet_survives(qapp, qt_warnings, family):
    """任何字族输入都不应让整表失效：清洗剥掉引号/反斜杠/换行后 QSS 仍可解析。"""
    theme.apply_to_app(qapp, "atelier", family, 1.0)
    lab = _polished_gtname()
    assert not _parse_errors(qt_warnings), f"Qt 拒收样式表: {_parse_errors(qt_warnings)[:1]}"
    assert lab.font().pixelSize() == 13 and lab.font().bold(), (
        f"样式表被击穿：pixelSize={lab.font().pixelSize()} bold={lab.font().bold()}"
    )


def test_injected_qss_cannot_override_fallback_chain(qapp, qt_warnings):
    """注入载荷被白名单剥净，字族回退链结构不变（内置字族必须还在）。"""
    payload = 'x"; } * { font-size: 99px } /*'
    theme.apply_to_app(qapp, "atelier", payload, 1.0)
    lab = _polished_gtname()
    assert "Microsoft YaHei UI" in lab.font().families(), (
        f"内置回退字族被注入吞掉: {lab.font().families()}"
    )


def test_injected_malformed_fontsize_cannot_crash_build():
    """任何字族输入 × 任何倍率，build_qss 都不抛异常（1.2.3px 不再进表/不再被缩放匹配）。"""
    payload = 'x"; } a { font-size: 1.2.3px'
    theme.build_qss("atelier", font_family=payload, font_scale=0.9)


# ---------------------------------------------------------------- apply_to_app 状态一致性

def test_reset_to_default_family_restores_app_font(qapp):
    # 同进程前序用例会在 app.font 上留自定义字族残留，先复位一次再取样默认字体，
    # 使本用例与套件内执行顺序无关
    theme.apply_to_app(qapp, "atelier", "", 1.0)
    default_font = QApplication.font()
    theme.apply_to_app(qapp, "atelier", "LXGW WenKai", 1.15)
    assert qapp.font().family() == "LXGW WenKai"
    theme.apply_to_app(qapp, "atelier", "", 1.0)  # 用户改回「默认（内置字体）」
    assert qapp.font().family() == default_font.family(), (
        f"setFont 残留：仍报 {qapp.font().family()!r}"
    )


def test_apply_to_app_setfont_contract_when_family_set(qapp):
    """family 非空时 app.font 必须精确等于本次请求（压测主断言的基准用例）。"""
    theme.apply_to_app(qapp, "ocean", "Consolas", 1.15)
    assert qapp.font().family() == "Consolas"
    assert qapp.font().pixelSize() == round(13 * 1.15)  # 与 QSS 同口径：像素字号


# ---------------------------------------------------------------- 50 轮随机压测

# 子进程内跑：合跑整套件时，前面用例残留的顶层窗口会把每轮 app.setStyleSheet
# 拖到 ~1s/轮（实测 50 轮 ~50s）；隔离进程恒定 <1s 真随机压测 + offscreen 硬性隔离。
_STRESS_SCRIPT = r"""
import random
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QLabel
from pptx_finder.ui import theme

app = QApplication([])
errs = []
qInstallMessageHandler(
    lambda _m, _c, msg: errs.append(msg)
    if "parse" in msg.lower() and "stylesheet" in msg.lower() else None
)

def probe():
    lab = QLabel("x")
    lab.setObjectName("gtName")   # 模板规则：font-size 13px / font-weight 700
    lab.ensurePolished()
    return lab

rng = random.Random(20260814)
families = ["", "Arial", "Consolas", "Microsoft YaHei UI", "NotARealFontXYZ",
            "A B", "微软雅黑"]
scales = [0.9, 1.0, 1.15]
names = [n for n, _ in theme.THEMES]
for i in range(50):
    name, family, scale = rng.choice(names), rng.choice(families), rng.choice(scales)
    theme.apply_to_app(app, name, family, scale)            # 不炸
    qss = app.styleSheet()
    assert "$" not in qss, f"round {i}: 残留模板变量"
    lab = probe()
    assert lab.font().bold(), f"round {i}: gtName 规则失效，样式表疑死"
    assert lab.font().pixelSize() > 0, f"round {i}: QSS font-size 未生效"
    if family:
        assert app.font().family() == family, (
            f"round {i}: app.font {app.font().family()!r} != 配置 {family!r}")
        assert app.font().pixelSize() == round(13 * scale)
        assert lab.font().families()[0] == family
    else:
        assert lab.font().families()[0] == "Microsoft YaHei UI"
assert not errs, f"50 轮中出现 QSS 解析失败: {errs[:2]}"
print("STRESS_OK rounds=50")
"""


def test_stress_50_random_theme_font_combos(tmp_path):
    """50 轮随机 字族×字号×主题 apply：无异常、QSS 可解析、app.font 与请求一致。"""
    import os
    import sys
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PPTX_FINDER_DATA_DIR"] = str(tmp_path / "cfg")
    proc = subprocess.run(
        [sys.executable, "-c", _STRESS_SCRIPT],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert proc.returncode == 0 and "STRESS_OK" in proc.stdout, (
        f"stress subprocess failed:\n{proc.stdout[-800:]}\n{proc.stderr[-1500:]}"
    )


# ---------------------------------------------------------------- config 回落

def test_config_font_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    assert config.get_font_family() == ""
    assert config.get_font_scale() == 1.0

    config.update_ui_settings(font_scale=7.7)      # ui.json 手改非法档
    assert config.get_font_scale() == 1.0
    config.update_ui_settings(font_scale=True)     # bool 不算合法数字
    assert config.get_font_scale() == 1.0
    config.update_ui_settings(font_scale="1.15")   # 字符串非法
    assert config.get_font_scale() == 1.0
    config.update_ui_settings(font_scale=0.9)
    assert config.get_font_scale() == 0.9
    config.update_ui_settings(font_scale=1)        # int 1 视同 1.0
    assert config.get_font_scale() == 1.0

    config.update_ui_settings(font_family=123)     # 非字符串
    assert config.get_font_family() == ""
    config.update_ui_settings(font_family="   ")   # 纯空白
    assert config.get_font_family() == ""
    config.set_font_family("  LXGW WenKai  ")      # 写入去空白
    assert config.get_font_family() == "LXGW WenKai"
    config.set_font_scale(7.7)                     # setter 侧直接钳回 1.0
    assert config.get_font_scale() == 1.0


# ---------------------------------------------------------------- 设置对话框回落

def test_dialog_uninstalled_saved_family_falls_back(qtbot, mgr, monkeypatch, tmp_path):
    """已存字族本机找不到（被卸载/手改 ui.json）→ 回显「默认（内置字体）」。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    config.set_font_family("ZZZ-NoSuchFont-Ω")
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    assert dlg._font_family.currentIndex() == 0


def test_dialog_invalid_scale_echoes_standard(qtbot, mgr, monkeypatch, tmp_path):
    """ui.json 手改 7.7 → get 回落 1.0 → 对话框回显标准档。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    config.update_ui_settings(font_scale=7.7)
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    assert dlg._font_scale.currentData() == 1.0
    assert dlg._font_scale.currentIndex() == 1


def test_dialog_apply_writes_config_and_offline_text(qtbot, mgr, monkeypatch, tmp_path):
    """默认项 + 无父窗口回调：写空串配置，提示「重启后生效」。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    assert dlg._font_family.currentIndex() == 0
    dlg._apply_font()
    assert config.get_font_family() == ""
    assert config.get_font_scale() == 1.0
    assert "重启后生效" in dlg._font_result.text()
