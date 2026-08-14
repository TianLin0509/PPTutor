"""主题系统单测：多风格 token 完整性 + QSS 生成 + 有序风格列表。"""
from __future__ import annotations

from pptx_finder.ui import theme

# 所有组件（main_window / report_overlay / heatmap）依赖的 token key，每套风格必须填齐
REQUIRED = {
    "win", "canvas", "field", "hover", "sel", "selblur",
    "ink1", "ink2", "ink3", "ink4", "bd", "bd2",
    "acc", "accd", "acctext", "grn", "scroll", "scrollh",
    "hl_r", "hl_g", "hl_b", "hl_a",
    # 阶段2：氛围背景 + 面板透出 + 圆角
    "base", "appbg", "panel", "panel2", "radius",
}
EXPECTED = ["atelier", "atelier_dark",
            "aurora", "cinema", "cyber", "ocean", "magma", "forest", "sakura", "midnight", "graphite", "cloud"]


def test_all_themes_registered():
    for name in EXPECTED:
        assert name in theme.TOKENS, f"风格 {name} 未注册"


def test_every_theme_has_all_required_keys():
    for name in EXPECTED:
        missing = REQUIRED - set(theme.TOKENS[name])
        assert not missing, f"{name} 缺 key: {missing}"


def test_build_qss_leaves_no_unsubstituted_token():
    """QSS 模板里所有 $token 都被替换（漏 key 会残留 $xxx）。"""
    for name in EXPECTED:
        qss = theme.build_qss(name)
        assert "$" not in qss, f"{name} 的 QSS 有未替换 token"
        assert "QWidget" in qss


def test_themes_ordered_with_labels():
    assert [n for n, _ in theme.THEMES] == EXPECTED
    assert all(label for _, label in theme.THEMES)


def test_build_qss_default_font_matches_legacy_hardcode():
    """不传字体参数时与旧硬编码逐字节一致（字族 + 13px 基础字号 + 等宽字族）。"""
    qss = theme.build_qss("atelier")
    assert 'font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;' in qss
    assert "font-size: 13px" in qss
    assert qss.count('"Consolas", "Microsoft YaHei UI"') == 9
    assert theme.build_qss("atelier", font_family="", font_scale=1.0) == qss


def test_build_qss_font_family_injected_quoted_with_fallback():
    """用户字族加引号注入 * 规则并回退内置字族；等宽 $mono_family 不受影响。"""
    qss = theme.build_qss("atelier", font_family="LXGW WenKai")
    assert '* { font-family: "LXGW WenKai", "Microsoft YaHei UI"' in qss
    assert '"Consolas", "Microsoft YaHei UI"' in qss
    assert "$" not in qss  # 注入后无残留模板变量


def test_build_qss_font_scale_scales_all_px_sizes():
    """倍率缩放模板内全部 px 字号：四舍五入到 0.5px，小数值不丢精度。"""
    qss = theme.build_qss("atelier", font_scale=1.15)
    assert "font-size: 15px" in qss      # 13 * 1.15 = 14.95 -> 15
    assert "font-size: 12px" in qss      # 10.5 * 1.15 = 12.075 -> 12
    assert "font-size: 43.5px" in qss    # 38 * 1.15 = 43.7 -> 43.5
    # 缩放后不应残留未处理的小数毛边（如 14.95px）
    import re as _re
    assert not _re.search(r"font-size:\s*[0-9]+\.[0-9][0-9]+px", qss)

    qss = theme.build_qss("atelier", font_scale=0.9)
    assert "font-size: 11.5px" in qss    # 13 * 0.9 = 11.7 -> 11.5
    assert "font-size: 9.5px" in qss     # 10.5 * 0.9 = 9.45 -> 9.5
    assert "$" not in qss
