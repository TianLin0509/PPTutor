"""左侧导航轨 + 主区页面化：rail 结构、默认页、搜索切页仲裁、版本/健康页懒加载。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from pptx_finder import db
from pptx_finder.ui.health_window import HealthWindow
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.version_window import VersionWindow


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


class FakeVersionManager:
    """版本页懒加载用的最小 manager：空文档表即可。"""

    def __init__(self):
        self.calls: list[str] = []

    def list_docs(self):
        self.calls.append("list_docs")
        return []

    def list_docs_details(self):
        self.calls.append("list_docs_details")
        return []


def _win(qtbot, tmp_path, **kwargs):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False, **kwargs)
    qtbot.addWidget(win)
    return win


def test_nav_rail_exists_and_default_page_is_dashboard(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)

    rail = win.findChild(QWidget, "navRail")
    assert rail is not None
    assert rail.width() == 56
    assert set(win._rail_page_btns) == {"search", "dashboard", "version", "health"}
    assert win.rail_report_btn is not None
    assert win.rail_settings_btn is not None

    # 启动默认页 = 概览，rail checked 态跟随
    assert win._current_page_key() == "dashboard"
    assert win._page_stack.currentWidget() is win.dashboard
    assert win._rail_page_btns["dashboard"].isChecked()
    assert not win._rail_page_btns["search"].isChecked()


def test_search_input_switches_pages_by_arbitration_rules(qtbot, tmp_path):
    win = _win(qtbot, tmp_path, version_mgr=FakeVersionManager())

    # 输入非空搜索词 → 自动切搜索页（搜索框在工具栏全局可见，输入即意图）
    win.search_box.setText("昇腾")
    win._do_search()
    assert win._current_page_key() == "search"
    assert win._rail_page_btns["search"].isChecked()

    # 在搜索页清空搜索词 → 回落概览页
    win.search_box.setText("")
    win._do_search()
    assert win._current_page_key() == "dashboard"

    # 在版本页输入搜索词 → 也切到搜索页
    win._switch_page("version")
    assert win._current_page_key() == "version"
    win.search_box.setText("昇腾")
    win._do_search()
    assert win._current_page_key() == "search"

    # 在版本页清空搜索词 → 不切走
    win._switch_page("version")
    win.search_box.setText("")
    win._do_search()
    assert win._current_page_key() == "version"

    # 在健康页清空搜索词 → 同样不切走
    win._switch_page("health")
    win.search_box.setText("")
    win._do_search()
    assert win._current_page_key() == "health"


def test_version_and_health_pages_lazy_load(qtbot, tmp_path):
    mgr = FakeVersionManager()
    win = _win(qtbot, tmp_path, version_mgr=mgr)

    # 初始未构造（懒加载）
    assert win._version_page_win is None
    assert win._health_page_win is None

    # 首次切到版本页才构造嵌入 VersionWindow，且文档加载跑完
    win._switch_page("version")
    assert isinstance(win._version_page_win, VersionWindow)
    assert win._version_page_win._embedded is True
    qtbot.waitUntil(lambda: "list_docs_details" in mgr.calls, timeout=4000)

    # 首次切到健康页才构造嵌入 HealthWindow，且首次体检完成（内容就绪）
    win._switch_page("health")
    assert isinstance(win._health_page_win, HealthWindow)
    assert win._health_page_win._embedded is True
    qtbot.waitUntil(lambda: win._health_page_win._report is not None, timeout=4000)


def test_version_rail_button_follows_feature_flag(qtbot, tmp_path):
    win = _win(qtbot, tmp_path)  # version_mgr=None → 版本功能关
    btn = win._rail_page_btns["version"]
    assert btn.isHidden()

    # 设置里开启版本管理 → set_version_manager 回到主窗，rail 入口即显示
    win.set_version_manager(FakeVersionManager())
    assert not btn.isHidden()

    # 再关掉 → 入口隐藏
    win.set_version_manager(None)
    assert btn.isHidden()


def test_locate_health_item_switches_to_search_filename_mode(qtbot, tmp_path):
    """体检条目点击定位：切搜索页 + 仅文件名模式 + 去扩展名的文件名作为查询文本。"""
    win = _win(qtbot, tmp_path)

    win._locate_health_item("/docs/昇腾 汇报.pptx", "昇腾 汇报.pptx")

    assert win._current_page_key() == "search"
    assert win._rail_page_btns["search"].isChecked()
    assert win.mode.currentIndex() == 1
    assert win.search_box.text() == "昇腾 汇报"
