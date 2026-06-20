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
EXPECTED = ["aurora", "cinema", "cyber", "ocean", "magma", "forest", "sakura", "midnight", "graphite", "cloud"]


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
