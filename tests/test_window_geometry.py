# -*- coding: utf-8 -*-
r"""窗口尺寸要记得住，而且必须放得进当下这块屏。

用户反馈「打开时窗口尺寸有时候不可控」。原因是 `main_window.__init__` 里写死了
`self.resize(1180, 760)`，既不记忆也不夹取——而 760 是**逻辑**像素：

    屏幕            缩放    可用逻辑高度   写死的 760
    1920x1080      150%    约 672        放不下
    1920x1200      150%    约 752        放不下
    1366x768       100%    约 728        放不下
    3840x2160      225%    912           放得下

「有时候」正来自这里：取决于分辨率和缩放。而本窗口是**无边框**的（自绘标题栏），
超出的部分压在任务栏下面，用户连下边缘都抓不到，缩不回来。

所以这里钉两件事：**夹取**（任何情况下都不许超过可用区域）和**记忆**（下次按上次的来）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRect                                # noqa: E402

from pptx_finder import config                                  # noqa: E402
from pptx_finder.ui.main_window import MainWindow               # noqa: E402


fit = MainWindow._fit_rect_to_screen


# 真实的「可用区域」：已扣掉任务栏
LAPTOP_1080P_150 = QRect(0, 0, 1280, 672)      # 1920x1080 @150%
LAPTOP_1200_150 = QRect(0, 0, 1280, 752)       # 1920x1200 @150%
NETBOOK = QRect(0, 0, 1366, 728)               # 1366x768 @100%
BIG_4K_225 = QRect(0, 0, 1707, 912)            # 3840x2160 @225%


@pytest.mark.parametrize("avail", [LAPTOP_1080P_150, LAPTOP_1200_150, NETBOOK, BIG_4K_225])
def test_default_size_never_exceeds_the_work_area(avail):
    """无边框窗口一旦比工作区高，下边缘就压在任务栏下面，用户没法缩回来。"""
    want = QRect(0, 0, MainWindow.DEFAULT_WIN_W, MainWindow.DEFAULT_WIN_H)
    got = fit(want, avail)
    assert got.width() <= avail.width()
    assert got.height() <= avail.height()
    assert avail.contains(got), f"{got} 没落在可用区域 {avail} 内"


def test_the_hardcoded_default_really_did_not_fit_common_laptops():
    """守住这条测试存在的理由，别让人以为夹取只是防御性代码。"""
    for avail in (LAPTOP_1080P_150, LAPTOP_1200_150, NETBOOK):
        assert MainWindow.DEFAULT_WIN_H > avail.height(), \
            f"{avail} 上默认高度本来是放得下的？那这条用例该更新了"


def test_a_window_from_a_big_monitor_shrinks_onto_a_laptop():
    """在 4K 上拉到很大，回家插笔记本，不能就此打不开。"""
    from_big = QRect(100, 100, 1700, 900)
    got = fit(from_big, LAPTOP_1080P_150)
    assert LAPTOP_1080P_150.contains(got)
    assert got.width() == 1280 and got.height() == 672


def test_offscreen_position_is_pulled_back():
    """外接屏拔掉之后，保存的坐标可能整个在屏幕外。"""
    got = fit(QRect(3000, 1500, 900, 600), LAPTOP_1080P_150)
    assert LAPTOP_1080P_150.contains(got)
    assert got.width() == 900 and got.height() == 600   # 放得下就别改尺寸


def test_negative_position_is_pulled_back():
    got = fit(QRect(-500, -300, 900, 600), NETBOOK)
    assert NETBOOK.contains(got)


def test_a_fitting_window_is_left_exactly_alone():
    """能放下就一个像素都别动，否则每次开都会漂。"""
    r = QRect(120, 80, 1000, 600)
    assert fit(r, BIG_4K_225) == r


def test_second_monitor_offset_is_respected():
    """副屏的 availableGeometry 原点不是 (0,0)，夹取不能把窗口拽回主屏。"""
    second = QRect(1707, 0, 1920, 1040)
    got = fit(QRect(1800, 100, 900, 600), second)
    assert second.contains(got)
    assert got.x() == 1800 and got.y() == 100


def test_a_window_larger_than_the_screen_in_one_axis_only():
    got = fit(QRect(0, 0, 800, 5000), NETBOOK)
    assert got.width() == 800
    assert got.height() == NETBOOK.height()


# ---- 落盘：读写与损坏容错 ----

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "d"))
    return tmp_path


def test_geometry_round_trips(isolated):
    config.set_window_geometry(12, 34, 1000, 700, maximized=False)
    got = config.get_window_geometry()
    assert got == {"x": 12, "y": 34, "w": 1000, "h": 700, "maximized": False}


def test_maximized_flag_round_trips(isolated):
    config.set_window_geometry(0, 0, 1000, 700, maximized=True)
    assert config.get_window_geometry()["maximized"] is True


def test_nothing_saved_yet_reads_as_none(isolated):
    assert config.get_window_geometry() is None


@pytest.mark.parametrize("junk", [
    {"w": 0, "h": 700, "x": 0, "y": 0},         # 零宽 → 窗口消失
    {"w": 1000, "h": -1, "x": 0, "y": 0},       # 负高
    {"w": "big", "h": 700, "x": 0, "y": 0},     # 类型不对
    {"w": 1000, "h": 700},                      # 缺坐标
    "not-a-dict",
    None,
])
def test_corrupt_geometry_falls_back_to_the_default(isolated, junk):
    """手改坏了 ui.json 不该让窗口开成 0x0 或直接崩。"""
    config.update_ui_settings(window_geometry=junk)
    assert config.get_window_geometry() is None


def test_saving_geometry_keeps_other_settings(isolated):
    """ui.json 是合并写的：存尺寸不能把主题和热键冲掉。"""
    config.set_theme("cloud")
    config.update_ui_settings(hotkey="Alt+F")
    config.set_window_geometry(1, 2, 900, 600)
    data = config.load_ui_settings()
    assert data["theme"] == "cloud"
    assert data["hotkey"] == "Alt+F"
    assert data["window_geometry"]["w"] == 900


def test_close_to_tray_also_saves():
    """关到托盘是最常见的『关窗』，不存的话下次唤起又变回默认尺寸。"""
    import inspect

    src = inspect.getsource(MainWindow.closeEvent)
    body = src.split("if self._to_tray_on_close", 1)[0]
    assert "_save_window_geometry()" in body, "必须在判断托盘分支之前就存"


def test_geometry_is_also_saved_while_dragging():
    """只在关窗时存不够：增量更新的 helper 会直接强杀本进程。"""
    import inspect

    assert "_schedule_geometry_save" in inspect.getsource(MainWindow.resizeEvent)
    assert "_schedule_geometry_save" in inspect.getsource(MainWindow.moveEvent)


def test_maximized_window_saves_the_restored_size():
    """最大化时 geometry() 是全屏，存它等于把『还原后的大小』弄丢。"""
    import inspect

    assert "normalGeometry()" in inspect.getsource(MainWindow._save_window_geometry)


# ---- 比「写死 resize」更深的一层：窗口的最小高度 ----
#
# 光夹取还不够。实测发现主窗口有个硬性最小高度：
#
#     minimumSizeHint    785 x 708
#     resize(1280, 672)  -> 实际 1280 x 747     顶不回去
#
# 也就是说在 1920x1080@150%（可用高度约 672）上，窗口**根本缩不进屏幕**，会比工作区
# 高 75px；而窗口是无边框的，下边缘压在任务栏下面抓不到，用户没法自己缩回来。
#
# 撑高的是概览页：dashView 内容高 652，作为普通子部件直接进页栈，它的高度就成了
# 整窗的下限（链条 central 720 <- contentWrap 668 <- pageStack 652 <- dashView 652）。
# 套一层 QScrollArea 之后，同样几档尺寸全部能缩进去。

COMMON_WORK_AREAS = [
    (1280, 672, "1920x1080@150%"),
    (1280, 752, "1920x1200@150%"),
    (1366, 728, "1366x768@100%"),
]


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("PPTX_FINDER_ROOTS", str(tmp_path / "d"))
    monkeypatch.setenv("PPTX_FINDER_SINGLETON_NAME", "geomtest")
    w = MainWindow(do_index=False)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w


def test_the_overview_page_is_scrollable(win):
    """不套滚动区的话，概览页 652px 的内容高度会变成整窗的最小高度。"""
    from PySide6.QtWidgets import QScrollArea

    page = win._pages["dashboard"]
    assert isinstance(page, QScrollArea), f"概览页不是滚动区：{type(page).__name__}"
    assert page.widgetResizable() is True
    assert page.widget() is win.dashboard


@pytest.mark.parametrize("w,h,label", COMMON_WORK_AREAS)
def test_window_can_shrink_into_common_laptop_screens(win, qtbot, w, h, label):
    win.resize(w, h)
    qtbot.wait(50)
    assert win.height() <= h, f"{label}：缩不到 {h}，实际 {win.height()}"
    assert win.width() <= w, f"{label}：缩不到 {w}，实际 {win.width()}"


def test_geometry_survives_a_close_and_reopen(win, qtbot, tmp_path):
    """真的存进去、真的读得回来（不只是断言调用了保存函数）。"""
    win.resize(1024, 700)
    win.move(60, 40)
    qtbot.wait(50)
    win._save_window_geometry()

    saved = config.get_window_geometry()
    assert saved is not None
    assert (saved["w"], saved["h"]) == (1024, 700)
    assert (saved["x"], saved["y"]) == (60, 40)
