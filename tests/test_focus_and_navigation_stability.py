"""搜词后切页 / 开浮层不许被后台事件拽回搜索页、不许被抢焦点。

现场事故：搜了「AI」后点概览、报告，界面一次次自己跳回搜索页。根因是后台刷新
（文件变动、索引完成、回收站同步、排序补跑）和输入防抖都直接走「用户搜索」口径，
有词就 _show_list() 切页；报告浮层又不接管焦点，Esc / 打字落到底下的搜索框。
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from pptx_finder import db
from pptx_finder.ui import stats_entry
from pptx_finder.ui.main_window import MainWindow

PAGES = ["dashboard", "material", "health", "version"]


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


class FakeVersionManager:
    def list_docs(self):
        return []

    def list_docs_details(self):
        return []


@pytest.fixture
def win(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    w = MainWindow(conn=conn, render_worker=StubRender(), do_index=False,
                   version_mgr=FakeVersionManager())
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=3000)
    yield w
    ov = getattr(w, "_stats_overlay", None)
    if ov is not None:
        ov.close()


def _search_ai(w, qtbot):
    w.search_box.setFocus()
    w.search_box.setText("AI")
    w._do_search()
    assert w._current_page_key() == "search"


def _click_rail(w, qtbot, key):
    qtbot.mouseClick(w._rail_page_btns[key], Qt.LeftButton)


def _unthrottle(w):
    w._last_bg_research_ts = 0.0
    w._last_user_input_ts = 0.0


def _fire_all_background_refreshes(w, qtbot):
    """把会触发重搜的后台入口挨个打一遍。"""
    _unthrottle(w)
    w._after_live_index()            # live watcher 写库
    w._do_live_refresh()             # 去抖到点
    _unthrottle(w)
    w._after_name_index_update()     # 全部文件热层
    w._do_live_refresh()
    w._after_health_recycle({"index_deleted": 1})   # 健康页清理回收
    w._sort_refresh_pending = True                  # 搜索中改了排序
    w._refresh_search_passive()
    qtbot.wait(50)


@pytest.mark.parametrize("page", PAGES)
def test_background_refresh_never_pulls_user_back_to_search(win, qtbot, page):
    _search_ai(win, qtbot)
    _click_rail(win, qtbot, page)
    assert win._current_page_key() == page
    _fire_all_background_refreshes(win, qtbot)
    qtbot.wait(400)  # 盖过输入防抖与零延迟定时器
    assert win._current_page_key() == page
    assert win._rail_page_btns[page].isChecked()
    assert win.search_box.text() == "AI"          # 搜索词保留，随时点回
    assert QApplication.focusWidget() is not win.search_box


@pytest.mark.parametrize("page", PAGES)
def test_index_done_on_other_page_stays_put(win, qtbot, page):
    _search_ai(win, qtbot)
    _click_rail(win, qtbot, page)
    _unthrottle(win)
    win._on_index_done({"indexed": 3, "skipped": 0, "errors": 0, "deleted": 0})
    win._live_refresh.stop()
    _unthrottle(win)
    win._do_live_refresh()
    qtbot.wait(100)
    assert win._current_page_key() == page


@pytest.mark.parametrize("target", ["dashboard", "material"])
def test_pending_debounce_does_not_drag_back_after_rail_click(win, qtbot, target):
    """刚敲完「AI」、防抖还没到点就点了别的页：到点后也不许被拽回去。"""
    win._switch_page("search")
    win.search_box.setFocus()
    qtbot.keyClicks(win.search_box, "AI")
    assert win._debounce.isActive()
    _click_rail(win, qtbot, target)
    qtbot.wait(win._debounce.interval() + 200)
    assert win._current_page_key() == target
    # 回到搜索页时补跑这次被推迟的搜索
    _click_rail(win, qtbot, "search")
    assert win._current_page_key() == "search"
    assert win._last_query_text == "AI"


def test_pending_debounce_does_not_drag_back_when_report_opens(win, qtbot):
    win._switch_page("search")
    win.search_box.setFocus()
    qtbot.keyClicks(win.search_box, "AI")
    win._switch_page("dashboard")   # 概览页上的搜索框同样可输入
    win.search_box.setFocus()
    qtbot.keyClicks(win.search_box, "x")
    assert win._debounce.isActive()
    stats_entry._open_report_sync(win)
    qtbot.wait(win._debounce.interval() + 200)
    assert win._current_page_key() == "dashboard"
    assert win._stats_overlay is not None and win._stats_overlay.isVisible()


def test_typing_on_dashboard_still_goes_to_search(win, qtbot):
    """概览页是搜索首页：在那里输入仍然直接出结果。"""
    win._switch_page("dashboard")
    win.search_box.setFocus()
    qtbot.keyClicks(win.search_box, "AI")
    qtbot.waitUntil(lambda: win._current_page_key() == "search", timeout=2000)


def test_rail_click_moves_focus_so_arrows_do_not_switch_page(win, qtbot):
    _search_ai(win, qtbot)
    _click_rail(win, qtbot, "dashboard")
    assert QApplication.focusWidget() is not win.search_box
    focus = QApplication.focusWidget()
    if focus is not None:
        qtbot.keyClick(focus, Qt.Key_Down)
        qtbot.keyClick(focus, Qt.Key_Return)
    assert win._current_page_key() == "dashboard"
    # 用户主动点回搜索框按方向键 = 要看结果，照常切到搜索页
    qtbot.mouseClick(win.search_box, Qt.LeftButton)
    qtbot.keyClick(win.search_box, Qt.Key_Down)
    assert win._current_page_key() == "search"


def test_report_overlay_takes_focus_and_esc_keeps_query(win, qtbot):
    _search_ai(win, qtbot)
    _click_rail(win, qtbot, "dashboard")
    stats_entry._open_report_sync(win)
    ov = win._stats_overlay
    qtbot.waitUntil(lambda: QApplication.focusWidget() is ov or ov.isAncestorOf(QApplication.focusWidget()),
                    timeout=2000)
    # 浮层开着时后台各种刷新 / 焦点归还都不许抢焦点
    _fire_all_background_refreshes(win, qtbot)
    win._restore_search_focus()
    win.facet_panel.hide()
    qtbot.wait(100)
    focus = QApplication.focusWidget()
    assert focus is ov or ov.isAncestorOf(focus)
    assert win._current_page_key() == "dashboard"
    assert ov.isVisible()
    # 打字不会落进底下的搜索框
    qtbot.keyClicks(focus, "zz")
    assert win.search_box.text() == "AI"
    # Esc 关浮层，不清底层搜索词
    qtbot.keyClick(QApplication.focusWidget(), Qt.Key_Escape)
    qtbot.waitUntil(lambda: getattr(win, "_stats_overlay", None) is None, timeout=2000)
    assert win.search_box.text() == "AI"
    assert win._current_page_key() == "dashboard"


def test_report_over_search_page_defers_refresh_until_closed(win, qtbot):
    """浮层盖着搜索页时后台变更只记脏（不在浮层背后重建列表），关浮层后补刷一次。"""
    _search_ai(win, qtbot)
    stats_entry._open_report_sync(win)
    ov = win._stats_overlay
    seq = win._search_seq
    _unthrottle(win)
    win._after_live_index()
    win._do_live_refresh()
    assert win._search_seq == seq and win._search_stale
    focus = QApplication.focusWidget()
    assert focus is ov or ov.isAncestorOf(focus)
    qtbot.keyClick(focus, Qt.Key_Escape)
    qtbot.waitUntil(lambda: win._search_seq == seq + 1, timeout=2000)
    assert win.search_box.text() == "AI"
    assert win._current_page_key() == "search"
    assert not win._search_stale


def test_report_click_cancels_pending_debounce_while_report_builds(win, qtbot, monkeypatch):
    """敲完词 <280ms 点「报告」：报告还在后台生成（浮层没出来）时，防抖到点也不许切页。"""
    win._switch_page("dashboard")
    win.search_box.setFocus()
    qtbot.keyClicks(win.search_box, "AI")
    assert win._debounce.isActive()
    started = []
    monkeypatch.setattr(stats_entry, "_open_report",
                        lambda mw: (started.append(1), setattr(mw, "_stats_report_loading", True)))
    qtbot.mouseClick(win.rail_report_btn, Qt.LeftButton)
    assert started == [1] and not win._debounce.isActive()
    win._debounce.start()  # 就算还有别的输入防抖到点，报告生成期间也不切页
    qtbot.wait(win._debounce.interval() + 200)
    assert win._current_page_key() == "dashboard"
    win._stats_report_loading = False


@pytest.mark.parametrize("page,query,expected", [
    ("material", "AI", "search"), ("health", "", "dashboard"), ("version", "AI", "search")])
def test_ctrl_l_on_pages_without_search_box_goes_back_and_focuses(win, qtbot, page, query, expected):
    win.search_box.setText(query)
    win._debounce.stop()
    win._switch_page(page)
    assert not win.search_box.isVisible()
    win.focus_search()
    assert win._current_page_key() == expected
    qtbot.waitUntil(lambda: QApplication.focusWidget() is win.search_box, timeout=2000)
    assert win.search_box.selectedText() == query


def test_update_auto_prompt_waits_while_report_open(win, qtbot, monkeypatch):
    from pptx_finder.ui import update_ui
    opened = []
    ctl = update_ui.UpdateController.__new__(update_ui.UpdateController)
    monkeypatch.setattr(update_ui.UpdateController, "pending_version", lambda self: "9.9.9")
    monkeypatch.setattr(update_ui.UpdateController, "_dialog_parent", lambda self: win)
    monkeypatch.setattr(update_ui.UpdateController, "open_dialog",
                        lambda self, mark_seen=True: opened.append(mark_seen))
    monkeypatch.setattr(update_ui, "_read_setting", lambda key: "")
    monkeypatch.setattr(update_ui, "_write_setting", lambda **kw: None)
    monkeypatch.setattr(update_ui.UpdateController, "_PROMPT_RETRY_MS", 50)
    QObject.__init__(ctl)
    ctl._state, ctl._manual_check, ctl._auto_prompt, ctl._prompt_waiting = "available", False, True, False
    stats_entry._open_report_sync(win)
    ctl._maybe_prompt_once()
    qtbot.wait(150)
    assert opened == []                    # 浮层开着：不弹
    win._stats_overlay.close()
    qtbot.waitUntil(lambda: opened == [False], timeout=2000)


def test_reopen_report_refocuses_overlay(win, qtbot):
    stats_entry._open_report_sync(win)
    ov = win._stats_overlay
    win.search_box.setFocus()
    stats_entry._open_report(win)  # 已开着 → 只带到前台
    assert win._stats_overlay is ov
    qtbot.waitUntil(lambda: QApplication.focusWidget() is ov, timeout=2000)


def test_stale_results_refresh_on_return_and_explicit_search_still_switches(win, qtbot):
    _search_ai(win, qtbot)
    _click_rail(win, qtbot, "health")
    seq = win._search_seq
    _unthrottle(win)
    win._after_live_index()
    win._do_live_refresh()
    assert win._search_seq == seq          # 不在搜索页：不跑、只记脏
    assert win._search_stale
    _click_rail(win, qtbot, "search")
    assert win._search_seq == seq + 1      # 回到搜索页补刷一次
    assert not win._search_stale
    # 在别的页显式发起搜索（按钮 / 回车）仍切到搜索页
    _click_rail(win, qtbot, "version")
    win.search_btn.click()
    assert win._current_page_key() == "search"


def test_ctrl_l_closes_report_and_focuses_search(win, qtbot):
    stats_entry._open_report_sync(win)
    win.focus_search()
    qtbot.waitUntil(lambda: getattr(win, "_stats_overlay", None) is None, timeout=2000)
    assert QApplication.focusWidget() is win.search_box
