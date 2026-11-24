"""素材库一级页 MaterialTabWidget：三栏、分页网格、搜索与来源、插入错误码就地提示、窄窗不溢出。"""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from pptx_finder import materials, ppt_transfer, team_materials
from pptx_finder.materials import Material, MaterialLibrary
from pptx_finder.ui.material_tab import MaterialTabWidget, resolve_tokens, sizes_for_tests
from pptx_finder.ui.material_window import MaterialWindow
from materials_testkit import build_pack_dir, make_pack, rows


class _Center:
    """最小下载中心替身：不联网、不弹窗。"""
    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class S(QObject):
            state_changed = Signal(str)
            installed = Signal(str)
        self._s = S()
        self.state_changed, self.installed = self._s.state_changed, self._s.installed
        self.state = {"phase": "idle", "status": {}}
        self.started = []

    def snapshot(self, key):
        return dict(self.state)

    def busy(self, key):
        return self.state["phase"] in ("checking", "downloading", "installing")

    def check_latest(self, key, force=False):
        pass

    def upgradable(self, key):
        return ""

    def start(self, key):
        self.started.append(key)


_CORE_CACHE = {}


@pytest.fixture
def core(tmp_path_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    team_materials._started = False
    if "dir" in _CORE_CACHE:          # 造包要跑构建工具，整个文件只造一次
        monkeypatch.setattr(materials, "builtin_core_dir", lambda: _CORE_CACHE["dir"])
        return _CORE_CACHE["dir"]
    base = tmp_path_factory.mktemp("core")
    data = (rows(40, "箭头与流程", "a") + rows(30, "容器与卡片", "b") + rows(20, "图标与小图", "i") +
            rows(12, "组合件·流程与步骤", "c") + rows(9, "组合件·图示与标注", "d") +
            [{"id": f"w{i}", "name": f"整页{i}", "category": c} for i, c in enumerate(
                ["整页模板·方案与原理", "整页模板·规划与计划", "整页模板·盘点与总结",
                 "整页模板·现状与问题", "整页模板·结果与增益"])])
    # 全部 18 类里挑 10 个零件类凑满左栏，验证窄窗不溢出
    for k, cat in enumerate(["示意组合", "编号·标签·胶囊", "栏头与标题条", "团队母题", "光带·托板·展台",
                             "装饰与其他", "通信图元"]):
        data += rows(8, cat, f"z{k}_")
    data += rows(6, "组合件·对比与增益", "e")
    out, core_dir, _ = build_pack_dir(base, data, "1.2.0", core=False)
    full = base / "full"
    import zipfile
    with zipfile.ZipFile(out / "component.zip") as z:
        z.extractall(full)
    (full / "team-materials.pptx").replace(full / "team-materials-core.pptx")
    _CORE_CACHE["dir"] = full
    monkeypatch.setattr(materials, "builtin_core_dir", lambda: full)
    return full


def _tab(qtbot, tmp_path, **kw):
    center = kw.pop("center", _Center())
    tab = MaterialTabWidget(component_center=center, library=MaterialLibrary(tmp_path / "lib"), **kw)
    qtbot.addWidget(tab)
    qtbot.waitUntil(lambda: tab._loaded, timeout=10000)
    return tab, center


def _wait_idle(qtbot, tab):
    qtbot.waitUntil(lambda: not tab._busy_op, timeout=10000)


def test_zero_arg_construction_is_safe(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(materials, "builtin_core_dir", lambda: tmp_path / "none")
    team_materials._started = False
    tab = MaterialTabWidget(component_center=_Center())
    qtbot.addWidget(tab)
    qtbot.waitUntil(lambda: tab._loaded)
    assert tab.empty_title.text() == "还没有团队素材" and tab.empty_primary.text() == "获取完整库"
    for name in ("apply_theme", "refresh_library", "focus_search", "import_material_file",
                 "collect_from_clipboard"):
        assert callable(getattr(tab, name))


def test_browse_search_source_and_categories(qtbot, core, tmp_path):
    tab, _ = _tab(qtbot, tmp_path)
    tab.resize(1200, 820)
    tab.show()
    total = len(tab._all)
    assert total == 178 and tab.count_label.text() == "178 个素材"
    assert "内置精选 178 条" in tab.pill.text()
    tab.search.setText("容器 素材1")                  # 空格分词，全部命中
    qtbot.waitUntil(lambda: tab.count_label.text() != "178 个素材")
    assert all("容器" in e.category and "素材1" in e.name for e in tab._view) and tab._view
    tab.search.setText("不存在的词")
    qtbot.waitUntil(lambda: tab.stack.currentWidget() is tab.empty)
    assert tab.empty_title.text() == "没有符合条件的素材"
    tab.empty_primary.click()
    assert tab.search.text() == "" and len(tab._view) == total
    tab._pick_category("组合件·流程与步骤")
    assert {e.category for e in tab._view} == {"组合件·流程与步骤"} and len(tab._view) == 12
    assert tab._cat_buttons["组合件·流程与步骤"].isChecked()
    tab._pick_category("@mine")
    assert tab.src_buttons["mine"].isChecked() and tab.empty_title.text() == "我的收藏还是空的"
    tab._set_source("team")
    assert len(tab._view) == total and tab._cat_buttons["@all"].isChecked()


@pytest.mark.parametrize("size, min_per_page", [((1200, 820), 15), ((1040, 600), 8)])
def test_one_screen_layout_without_overflow(qtbot, core, tmp_path, size, min_per_page):
    tab, _ = _tab(qtbot, tmp_path)
    tab.resize(*size)
    tab.show()
    qtbot.waitExposed(tab)
    qtbot.wait(150)
    info = sizes_for_tests(tab)
    assert info["per_page"] >= min_per_page
    narrow = size[0] < 1100
    assert info["left"] == (176 if narrow else 196) and info["right"] == (270 if narrow else 320)
    # 右栏动作区完整可见
    for w in (tab.insert_btn, tab.copy_btn, tab.copy_image_link):
        top_left = w.mapTo(tab, w.rect().topLeft())
        assert w.isVisible() and top_left.y() + w.height() <= tab.height()
    # 左栏不出滚动条（窄窗自动手风琴）
    from PySide6.QtWidgets import QScrollArea
    area = tab.left.findChild(QScrollArea, "mtCatScroll")
    assert not area.verticalScrollBar().isVisible()
    # 网格本身不滚动：翻页
    pages = tab.grid.page_count()
    assert pages == -(-len(tab._view) // info["per_page"])
    tab.next_btn.click()
    assert tab.grid.page == 1 and tab.page_label.text() == f"2 / {pages}"


def test_thumbnails_decode_in_background_and_preview(qtbot, core, tmp_path):
    tab, _ = _tab(qtbot, tmp_path)
    tab.resize(1200, 820)
    tab.show()
    first = tab._view[0]
    qtbot.waitUntil(lambda: tab.cache.get(first) is not None, timeout=10000)
    tab.grid.select(0)
    assert tab.preview.image is not None and tab.det_name.text() == first.name
    assert "团队素材（内置精选） v1.2.0（只读）" == tab.det_source.text()
    assert tab.save_mine_link.isVisibleTo(tab) and not tab.edit_link.isVisibleTo(tab)


def test_insert_error_code_shows_inline_fix_and_copy_fallback(qtbot, core, tmp_path, monkeypatch):
    tab, _ = _tab(qtbot, tmp_path)
    toasts = []
    tab.toastRequested.connect(lambda m, lv: toasts.append((m, lv)))
    calls = []

    def insert(path, page):
        calls.append((Path(path).name, page))
        raise ppt_transfer.MaterialTransferError("no_powerpoint")
    monkeypatch.setattr(ppt_transfer, "insert_material", insert)
    copied = []
    monkeypatch.setattr(ppt_transfer, "copy_material", lambda path, page: copied.append(page))
    tab.grid.select(3)
    tab.insert_btn.click()
    _wait_idle(qtbot, tab)
    assert calls == [("team-materials-core.pptx", tab._view[3].item.page)]
    assert tab.warn.isVisibleTo(tab) and "先打开要放素材的 PPT" in tab.warn_text.text()
    assert tab.warn_copy.isVisibleTo(tab) and toasts[-1][1] == "warn"
    tab.warn_copy.click()
    _wait_idle(qtbot, tab)
    assert copied == [tab._view[3].item.page] and not tab.warn.isVisibleTo(tab)
    assert toasts[-1] == ("已复制形状，回 PPT 按 Ctrl+V。", "ok")


def test_insert_success_toast_names_deck_and_page(qtbot, core, tmp_path, monkeypatch):
    tab, _ = _tab(qtbot, tmp_path)
    toasts = []
    tab.toastRequested.connect(lambda m, lv: toasts.append((m, lv)))
    monkeypatch.setattr(ppt_transfer, "insert_material",
                        lambda path, page: {"presentation": "季度汇报.pptx", "slide": 3, "shapes": 1})
    tab.grid.select(0)
    tab.insert_btn.click()
    _wait_idle(qtbot, tab)
    assert toasts[-1] == ("已插入到「季度汇报.pptx」第 3 页（1 个形状，可编辑，未自动保存）", "ok")


def test_busy_error_has_no_copy_offer(qtbot, core, tmp_path, monkeypatch):
    tab, _ = _tab(qtbot, tmp_path)

    def insert(path, page):
        raise ppt_transfer.MaterialTransferError("busy")
    monkeypatch.setattr(ppt_transfer, "insert_material", insert)
    tab.grid.select(0)
    tab.insert_btn.click()
    _wait_idle(qtbot, tab)
    assert "正忙" in tab.warn_text.text() and not tab.warn_copy.isVisibleTo(tab)


def test_mine_collect_edit_and_delete(qtbot, core, tmp_path, monkeypatch):
    tab, _ = _tab(qtbot, tmp_path)
    library = tab.library

    def capture(name, category):
        item = Material("a" * 32, name or "我的箭头", category, 1)
        folder = library.folder(item.id)
        folder.mkdir()
        library._write(folder, item)
        image = QImage(80, 40, QImage.Format_ARGB32)
        image.fill(Qt.red)
        image.save(str(folder / "preview.png"))
        return item
    monkeypatch.setattr(library, "capture", capture)
    tab.collect_from_clipboard()
    qtbot.waitUntil(lambda: any(e.source == "mine" for e in tab._all), timeout=10000)
    tab._pick_category("@mine")
    assert [e.name for e in tab._view] == ["我的箭头"]
    tab.grid.select(0)
    assert tab.edit_link.isVisibleTo(tab) and tab.delete_link.isVisibleTo(tab)
    assert not tab.save_mine_link.isVisibleTo(tab) and tab.export_btn.isVisibleTo(tab)
    from pptx_finder.ui import owner_actions
    monkeypatch.setattr(owner_actions, "ask", lambda *a, **k: True)
    tab._delete_mine()
    qtbot.waitUntil(lambda: not any(e.source == "mine" for e in tab._all), timeout=10000)
    assert (library.root / ".trash").is_dir()           # 移出可找回


def test_import_material_pptx_shows_imported_source(qtbot, core, tmp_path):
    tab, _ = _tab(qtbot, tmp_path)
    pack = make_pack(tmp_path / "同事的形状.pptx", rows(3, "容器与卡片", "q"))
    toasts = []
    tab.toastRequested.connect(lambda m, lv: toasts.append(m))
    tab.import_material_file(str(pack))
    qtbot.waitUntil(lambda: any(e.source == "imported" for e in tab._all), timeout=10000)
    assert sum(e.source == "imported" for e in tab._all) == 3 and "3 个素材" in toasts[-1]
    tab.import_material_file(str(tmp_path / "notes.txt"))
    assert ".pptx" in toasts[-1]


def test_status_text_and_theme_tokens(qtbot, core, tmp_path):
    seen = []
    center = _Center()
    tab = MaterialTabWidget(component_center=center, library=MaterialLibrary(tmp_path / "lib"))
    tab.statusTextChanged.connect(seen.append)
    qtbot.addWidget(tab)
    qtbot.waitUntil(lambda: bool(seen))
    assert seen[-1].startswith("团队素材 v1.2.0")
    tab.apply_theme({"name": "night_glass", "is_dark": True, "surface": "#252529", "accent": "#2464CD",
                     "accent_hover": "#1F57B5", "text": "#F5F5F7"})
    assert tab.tok["accent"] == "#2464CD" and "#2464CD" in tab.styleSheet()
    legacy = resolve_tokens({"panel": "#111111", "ink1": "#EEEEEE", "acc": "#0A84FF", "is_light": False})
    assert legacy["surface"] == "#111111" and legacy["accent"] == "#0A84FF" and legacy["is_dark"]


def test_offline_component_unreachable_notice(qtbot, core, tmp_path):
    tab, center = _tab(qtbot, tmp_path)
    center.state = {"phase": "error", "status": {}, "error": "连不上"}
    center.state_changed.emit("materials")
    assert "内源暂时连不上" in tab.notice_text.text() and tab.notice_retry.isVisibleTo(tab)
    tab.notice_retry.click()
    assert center.started == ["materials"]


def test_material_window_shell_hosts_tab_without_native_children(qtbot, core, tmp_path):
    from pptx_finder.ui import window_drag
    win = MaterialWindow(library=MaterialLibrary(tmp_path / "lib"), component_center=_Center())
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win.tab._loaded, timeout=10000)
    assert isinstance(win.tab, MaterialTabWidget)
    win.tab.toastRequested.emit("你好", "info")
    assert win.toast_line.text() == "你好"
    assert window_drag.native_children(win) == []
