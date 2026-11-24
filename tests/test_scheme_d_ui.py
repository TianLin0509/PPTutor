"""方案 D 通透玻璃（第二阶段）回归：主题 token、深浅切换、药丸轨、素材页挂载、矮窗不溢出、无原生子窗。"""
from __future__ import annotations

import sys
import types

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from pptx_finder import db
from pptx_finder.ui import theme, window_drag
from pptx_finder.ui.main_window import MainWindow

CONTRACT_KEYS = {"name", "is_dark", "is_glass", "floating_cards", "appbg", "blobs", "surface",
                 "surface_hover", "surface_border", "text", "text_muted", "accent", "accent_hover",
                 "accent_text", "accent_soft", "danger", "warn", "ok"}


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def _win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    return win


def _lum(c: str) -> float:
    q = QColor(c)

    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(q.red()) + 0.7152 * ch(q.green()) + 0.0722 * ch(q.blue())


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_default_theme_is_mist_and_main_themes_first():
    assert theme.DEFAULT_THEME == "mist_glass"
    names = [n for n, _ in theme.THEMES]
    assert names[:2] == ["mist_glass", "night_glass"]
    assert set(theme.MAIN_THEMES) == {"mist_glass", "night_glass", "atelier", "atelier_dark"}
    assert set(theme.MORE_THEMES).isdisjoint(theme.MAIN_THEMES)
    assert len(theme.MORE_THEMES) == 10


def test_contract_tokens_complete_and_qcolor_parsable():
    for name, _ in theme.THEMES:
        t = theme.contract_tokens(name)
        assert CONTRACT_KEYS <= set(t), (name, CONTRACT_KEYS - set(t))
        for k, v in t.items():
            if isinstance(v, str) and k not in ("name", "label"):
                assert QColor(v).isValid(), (name, k, v)   # rgba() 字符串会被 QColor 画成黑块
    assert theme.contract_tokens("mist_glass")["is_dark"] is False
    assert theme.contract_tokens("night_glass")["is_dark"] is True


def test_floating_cards_only_for_main_four():
    for name, _ in theme.THEMES:
        expect = name in ("mist_glass", "night_glass", "atelier", "atelier_dark")
        assert theme.contract_tokens(name)["floating_cards"] is expect, name


def test_night_glass_accent_and_aux_text_contrast():
    t = theme.TOKENS["night_glass"]
    assert t["accfill"].upper() == "#2464CD" and t["accfillh"].upper() == "#1F57B5"
    assert _contrast("#FFFFFF", t["accfill"]) >= 5.5
    assert _contrast("#FFFFFF", t["accfillh"]) >= 5.5
    # 辅助字（最淡一级）在暗夜卡片合成底 #182344 上 ≥ 4.5:1
    assert _contrast(t["ink4"], "#182344") >= 4.5
    m = theme.TOKENS["mist_glass"]
    assert _contrast("#FFFFFF", m["accfill"]) >= 4.5
    assert _contrast(m["ink4"], "#F0F5FF") >= 4.5


def test_dark_light_toggle_pairs(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._apply_theme("mist_glass", persist=False)
    win._toggle_dark_light()
    assert win._theme == "night_glass"
    win._toggle_dark_light()
    assert win._theme == "mist_glass"
    win._apply_theme("ocean", persist=False)   # 旧主题：按明暗切到晨雾 / 暗夜
    win._toggle_dark_light()
    assert win._theme in ("mist_glass", "night_glass")


def test_rail_pages_and_material_navigation(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    assert list(win._rail_page_btns)[:2] == ["search", "material"]
    assert set(win._rail_page_btns) == {"search", "material", "dashboard", "health", "version"}
    win._rail_page_btns["material"].click()
    QApplication.processEvents()
    assert win._current_page_key() == "material"
    assert win._rail_page_btns["material"].isChecked()
    win.show_materials_page()
    assert win._current_page_key() == "material"
    # 页头换成「素材库」，搜索胶囊收起
    heads = [w.text() for w in win.findChildren(QLabel) if w.isVisibleTo(win)]
    assert any("素材库" in h for h in heads)


def test_material_tab_mounted_with_contract_tokens(qtbot, tmp_path, monkeypatch):
    calls = {"apply": [], "compact": [], "shutdown": 0, "refresh": 0}

    class FakeTab(QWidget):
        toastRequested = Signal(str, str)
        statusTextChanged = Signal(str)

        def __init__(self, parent=None, *, data_dir=None, component_center=None, theme_tokens=None):
            super().__init__(parent)
            calls["apply"].append(theme_tokens)

        def apply_theme(self, tokens):
            calls["apply"].append(tokens)

        def refresh_library(self):
            calls["refresh"] += 1

        def focus_search(self):
            pass

        def set_header_compact(self, on):
            calls["compact"].append(on)

        def shutdown(self):
            calls["shutdown"] += 1

    mod = types.ModuleType("pptx_finder.ui.material_tab")
    mod.MaterialTabWidget = FakeTab
    monkeypatch.setitem(sys.modules, "pptx_finder.ui.material_tab", mod)
    win = _win(qtbot, tmp_path)
    win.show_materials_page()
    QApplication.processEvents()
    assert isinstance(win._material_tab, FakeTab)
    assert calls["compact"] == [True]
    assert CONTRACT_KEYS <= set(calls["apply"][0])
    win._apply_theme("night_glass", persist=False)
    assert calls["apply"][-1]["name"] == "night_glass" and calls["apply"][-1]["is_dark"] is True
    win._material_tab.statusTextChanged.emit("团队素材 · 测试状态")
    QApplication.processEvents()
    assert any("测试状态" in w.text() for w in win.findChildren(QLabel) if w.isVisibleTo(win))
    win._shutdown()
    assert calls["shutdown"] == 1


def test_compact_rail_and_1280x672_fits(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    win._apply_theme("mist_glass", persist=False)
    win.show()
    qtbot.waitExposed(win)
    for page in ("dashboard", "search", "material", "health", "version"):
        win._switch_page(page)
        win.resize(1280, 672)
        qtbot.wait(60)
        QApplication.processEvents()
        assert (win.width(), win.height()) == (1280, 672), page
        assert win._nav_rail.width() == win._RAIL_COMPACT_W
        cur = win._page_stack.currentWidget()
        assert cur.geometry().bottom() <= win.height(), page
    win.resize(1440, 900)
    qtbot.wait(60)
    QApplication.processEvents()
    assert win._nav_rail.width() == win._RAIL_W
    assert window_drag.native_children(win) == []


def test_preview_action_row_primary_secondary_icon(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)
    assert win.goto_btn.objectName() == "primary"
    assert win.insert_slide_btn.text() == "插入到当前 PPT"
    assert win.copy_slide_btn.text() == "" and "复制本页" in win.copy_slide_btn.toolTip()
    assert "打开文件" in win.open_btn.toolTip()
    assert win.more_btn.text() == "更多"
    assert win.preview_error_open_btn.text() == "打开文件"


def test_qcolor_parses_rgba_tokens_never_black():
    for name, _ in theme.THEMES:
        for k, v in theme.TOKENS[name].items():
            if isinstance(v, str) and v.strip().lower().startswith("rgb"):
                q = theme.qcolor(v)
                assert q.isValid(), (name, k, v)
                assert theme._qt_color_str(v).startswith("#"), (name, k, v)
    q = theme.qcolor("rgba(40,60,100,0.09)")
    assert (q.red(), q.green(), q.blue(), q.alpha()) == (40, 60, 100, 23)
    assert theme.qcolor("not-a-color", "#123456").name() == "#123456"


def test_health_score_ring_track_not_black(qtbot):
    from pptx_finder.ui.health_window import _ScoreRing

    class Owner:
        _tok = dict(theme.TOKENS["mist_glass"])

        def _score_color(self, s):
            return "#3A9E42"

    ring = _ScoreRing(Owner(), 132)
    qtbot.addWidget(ring)
    ring.set_score(None)   # 只画轨道
    img = ring.grab().toImage()
    c = img.pixelColor(int(img.width() / 2), int(13 * img.height() / 132))   # 顶部轨道中线
    assert c.alpha() > 0
    assert not (c.red() < 40 and c.green() < 40 and c.blue() < 40), c.name()
