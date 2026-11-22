"""RSI 第六轮：日常搜索状态、键盘、延迟退出与诊断摘要。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import re
import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from pptx_finder.models import FileResult, SearchHit
from pptx_finder.ui import main_window, theme
from test_rsi_r3_slide_take import preview
from test_rsi_r4_func import settings
from test_ui import PendingSearchWorker as BasePendingSearchWorker


class PendingSearchWorker(BasePendingSearchWorker):
    def request(self, *args, **kwargs):
        super().request(*args)


def test_imports_use_current_copy():
    assert Path(main_window.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


def test_same_query_keeps_filter_page_and_zoom(preview):
    win = preview
    win.facet_panel._toggle("page", "1-10", True)
    win._act_goto_page(2)
    win._zoom = 2.0
    win._do_search()
    assert win._facet_filters == {"page": {"1-10"}}
    assert win.facet_panel.active_filters() == win._facet_filters
    assert win._view_page == 2 and win._zoom == 2.0


def test_theme_keeps_page_zoom_and_navigation(preview, qtbot):
    win = preview
    win._zoom = 2.0
    win._apply_theme("atelier_dark", persist=False)
    assert win._view_page == 2 and win._zoom == 2.0
    assert "第 2 / 3 页" in win.page_label.text()
    assert not win.prev_btn.isEnabled() and not win.next_btn.isEnabled()
    qtbot.wait(200)  # 等现有列表淡入完成，再核对真实绘制。
    output = Path(__file__).resolve().parents[1] / "artifacts/rsi-r6-dark.png"
    assert win.grab().save(str(output))


def test_pending_transfer_defers_close(preview, monkeypatch):
    win = preview
    win._bg_tasks.append(SimpleNamespace(_label="ppt-transfer"))
    calls = []
    monkeypatch.setattr(win, "_shutdown", lambda: calls.append("shutdown"))
    win._to_tray_on_close = False
    try:
        win.close()
        assert not calls and win._storage_quit_pending
    finally:
        win._bg_tasks.clear()
    win._finish_storage_quit()
    assert calls == ["shutdown"]


def test_autostart_failure_updates_visible_summary(settings):
    settings._on_autostart_toggle_done(settings._autostart_toggle_token, True, False)
    assert "自启" in settings.diag_summary.text() and "失败" in settings.diag_summary.text()


def test_rescan_failure_updates_visible_summary(settings):
    def fail():
        raise RuntimeError("scan failed")
    settings._on_rescan = fail
    settings._request_rescan()
    assert "扫描" in settings.diag_summary.text() and "失败" in settings.diag_summary.text()


def test_down_from_dashboard_switches_to_search(preview, qtbot):
    preview._switch_page("dashboard")
    qtbot.keyClick(preview.search_box, Qt.Key_Down)
    assert preview._current_page_key() == "search"


def test_escape_is_available_outside_search_box(preview, qtbot):
    preview.open_btn.setFocus()
    qtbot.keyClick(preview.open_btn, Qt.Key_Escape)
    assert preview.search_box.text() == ""


def _luminance(rgb):
    values = [v / 255 for v in rgb]
    return sum(w * (v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4)
               for w, v in zip((.2126, .7152, .0722), values))


def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize("surface", ["field", "panel", "rail", "win", "#203854"])
def test_dark_secondary_text_meets_aa(surface):
    tok = theme.tok("atelier_dark")
    background = surface if surface.startswith("#") else tok[surface]
    assert (_luminance(_rgb(tok["ink4"])) + .05) / (_luminance(_rgb(background)) + .05) >= 4.5


def _results(count=30):
    now = time.time()
    return [FileResult(file_id=i, path=f"C:/slides/result-{i}.pptx", name=f"result-{i}.pptx",
                       ext=".pptx", mtime=now - (i // 5) * 15 * 86400, size=i + 1,
                       page_count=3, status="ok", score=count - i, name_hit=False,
                       hits=[SearchHit(1, "hit"), SearchHit(3, "hit")]) for i in range(count)]


def _install_results(win, results):
    win._cancel_auto_preview()
    win._suppress_select_preview = False
    win._showing_recent = False
    win._results_raw = results
    win._refresh_facets()
    win._apply_sort_render()
    win._select_first()


@pytest.mark.parametrize("operation", ["live", "sort", "reverse", "match"])
def test_async_same_query_retains_complete_browse_state(preview, operation):
    win = preview
    results = _results()
    _install_results(win, results)
    win.facet_panel._toggle("page", "1-10", True)
    win._suppress_select_preview = False
    win._select_path(results[20].path)
    win._hit_idx, win._view_page, win._zoom = 1, 2, 2.25
    win._search_worker = PendingSearchWorker()
    if operation == "live":
        win._last_user_input_ts = win._last_bg_research_ts = 0
        win._do_live_refresh()
    elif operation == "sort":
        win.sort_combo.setCurrentIndex(1)
    elif operation == "reverse":
        win.sort_desc_btn.click()
    else:
        win.match_mode_combo.setCurrentIndex(1)
    assert win._search_pending_req is not None
    win._on_search_done(win._search_seq, win.search_box.text(), results[::-1], 1, None)
    assert win._cur.path == results[20].path
    assert (win._hit_idx, win._view_page, win._zoom) == (1, 2, 2.25)
    assert win._facet_filters == win.facet_panel.active_filters() == {"page": {"1-10"}}
    assert win.findChildren(QPushButton, "facetActiveChip")


def test_recent_refresh_keeps_browse_state(preview):
    win = preview
    results = _results(2)
    win._clear_search_now()
    win._recent_cache = results
    win._recent_cache_at = time.monotonic()
    win._show_recent()
    win._select_first()
    win.facet_panel._toggle("page", "1-10", True)
    win._view_page, win._zoom = 2, 2.0
    win._show_recent()  # 相同空查询；走真实缓存刷新入口。
    assert win._cur.path == results[0].path
    assert (win._view_page, win._zoom) == (2, 2.0)
    assert win._facet_filters == {"page": {"1-10"}}


def test_requery_clears_filters_and_preview(preview):
    win = preview
    win.facet_panel._toggle("page", "1-10", True)
    win._zoom = 2.0
    win.search_box.setText("第三页")
    win._do_search()
    assert win._facet_filters == {} and win.facet_panel.active_filters() == {}
    assert win._view_page == 3 and win._zoom == 1.0


def test_changed_result_clamps_page_and_preserves_hit_page(preview):
    win = preview
    old = replace(win._cur, hits=[SearchHit(1, "a"), SearchHit(3, "b")])
    _install_results(win, [old])
    win._hit_idx, win._view_page, win._zoom = 1, 3, 2.0
    win._search_keep_selection = True
    win._finish_search("命中", [replace(old, page_count=2, hits=[SearchHit(2, "new")])])
    assert win._view_page == 2 and win._hit_idx == 0 and win._zoom == 2.0
    assert "第 2 / 2 页" in win.page_label.text()


def test_removed_selection_falls_back_to_first(preview):
    win = preview
    _install_results(win, _results(2))
    removed = win._cur.path
    win._search_keep_selection = True
    win._finish_search("命中", [win._results[-1]])
    assert win._cur.path != removed
    assert win._view_page == 1 and win._zoom == 1.0


def test_refresh_drops_only_missing_filter_buckets(preview):
    win = preview
    win.facet_panel._filters = {"page": {"1-10", "30+"}, "folder": {"gone"}}
    win._refresh_facets(keep=True)
    assert win._facet_filters == {"page": {"1-10"}}


def test_keyboard_skips_time_headers_and_loads_all_batches(preview, qtbot):
    win = preview
    results = _results()
    win.sort_combo.setCurrentText("最近修改")
    _install_results(win, results)
    assert any(win.result_list.item(i).data(Qt.UserRole) is None
               for i in range(win.result_list.count()))
    for expected in range(1, len(results)):
        qtbot.keyClick(win.search_box, Qt.Key_Down)
        assert win.result_list.currentItem().data(Qt.UserRole) == expected
    assert win._render_more_item is None
    qtbot.keyClick(win.search_box, Qt.Key_Down)
    assert win._cur.path == results[-1].path
    for expected in range(len(results) - 2, -1, -1):
        qtbot.keyClick(win.search_box, Qt.Key_Up)
        assert win.result_list.currentItem().data(Qt.UserRole) == expected
    qtbot.keyClick(win.search_box, Qt.Key_Up)
    assert win._cur.path == results[0].path


@pytest.mark.parametrize("key,expected", [(Qt.Key_Return, 0), (Qt.Key_Down, 1), (Qt.Key_Up, 0)])
def test_debounce_key_waits_for_new_async_results(preview, qtbot, monkeypatch, key, expected):
    win = preview
    opened = []
    monkeypatch.setattr(win, "_act_goto", lambda: opened.append(win._cur.path))
    win._search_worker = PendingSearchWorker()
    win.search_box.setText("new query")
    assert win._debounce.isActive()
    qtbot.keyClick(win.search_box, key)
    assert len(win._search_worker.requests) == 1 and not win._debounce.isActive()
    assert not opened
    new = _results(2)
    win._on_search_done(win._search_seq, "new query", new, 1, None)
    assert win._cur.path == new[expected].path
    assert opened == ([new[0].path] if key == Qt.Key_Return else [])


def test_debounce_enter_synchronous_search_opens_new_result(preview, qtbot, monkeypatch):
    win = preview
    opened = []
    monkeypatch.setattr(win, "_act_goto", lambda: opened.append(win._view_page))
    win.search_box.setText("第三页")
    qtbot.keyClick(win.search_box, Qt.Key_Return)
    assert opened == [3] and not win._debounce.isActive()


@pytest.mark.parametrize("outcome", ["error", "empty", "superseded"])
def test_queued_enter_never_opens_stale_results(preview, qtbot, monkeypatch, outcome):
    win = preview
    opened = []
    monkeypatch.setattr(win, "_act_goto", lambda: opened.append(win._cur.path))
    win._search_worker = PendingSearchWorker()
    win.search_box.setText("new query")
    qtbot.keyClick(win.search_box, Qt.Key_Return)
    old_seq = win._search_seq
    if outcome == "superseded":
        win.search_box.setText("even newer")
        win._do_search()
    win._on_search_done(old_seq, "new query", [] if outcome != "superseded" else _results(2),
                        1, "database is locked" if outcome == "error" else None)
    if outcome == "superseded":
        win._on_search_done(win._search_seq, "even newer", _results(2), 1, None)
    assert not opened


@pytest.mark.parametrize("control", ["sort_combo", "sort_secondary", "match_mode_combo"])
def test_refinement_returns_focus_to_search(preview, qtbot, control):
    win = preview
    win.activateWindow()
    combo = getattr(win, control)
    combo.setFocus()
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    qtbot.waitUntil(lambda: QApplication.focusWidget() is win.search_box)


def test_card_click_focuses_list_and_keyboard_continues(preview, qtbot):
    win = preview
    _install_results(win, _results(3))
    win.activateWindow()
    card = win.result_list.itemWidget(win.result_list.item(1))
    qtbot.mouseClick(card, Qt.LeftButton)
    assert QApplication.focusWidget() is win.result_list
    qtbot.keyClick(win.result_list, Qt.Key_Down)
    assert win._cur.path == win._results[2].path


def test_escape_closes_filter_before_clearing_query(preview, qtbot):
    win = preview
    win._toggle_facet()
    qtbot.keyClick(win.facet_panel._clear_btn, Qt.Key_Escape)
    assert win.facet_panel.isHidden() and win.search_box.text() == "命中"
    qtbot.keyClick(win.search_box, Qt.Key_Escape)
    assert not win.search_box.text()


@pytest.mark.parametrize("failed", [False, True])
def test_real_background_transfer_finishes_before_force_quit(preview, qtbot, monkeypatch, failed):
    win = preview
    entered, release = threading.Event(), threading.Event()
    events = []
    monkeypatch.setattr(QApplication, "quit", lambda: events.append("quit"))
    shutdown = win._shutdown
    monkeypatch.setattr(win, "_shutdown", lambda: (events.append("shutdown"), shutdown()))
    toast = win._toast
    monkeypatch.setattr(win, "_toast", lambda msg: (events.append(msg), toast(msg)))

    def work():
        entered.set()
        assert release.wait(5)
        if failed:
            raise PermissionError("locked")
        return "done"

    def done(value):
        events.append(value)
        win._toast("已复制本页")

    win._run_slide_transfer(work, done, "复制本页")
    try:
        qtbot.waitUntil(entered.is_set)
        win.force_quit()
        qtbot.wait(150)
        assert "quit" not in events and "shutdown" not in events and not win._closing
        assert "幻灯片" in win.statusBar().currentMessage()
    finally:
        release.set()
    qtbot.waitUntil(lambda: win._active_heavy_op is None)
    assert win._toast_timer.isActive()
    qtbot.wait(150)
    assert "quit" not in events  # 给用户留下读完现有结果提示的时间。
    qtbot.waitUntil(lambda: "quit" in events)
    result = next(i for i, e in enumerate(events) if "失败" in e) if failed else events.index("done")
    assert result < events.index("shutdown") < events.index("quit")


@pytest.mark.parametrize("kind", ["autostart_toggle", "rescan"])
def test_latest_diagnostic_result_survives_inflight_refresh_and_clears_on_success(settings, kind):
    settings._record_diag_operation(kind, f"{kind}: failed (test)")
    settings._on_diagnostics_ready(settings._diag_refresh_token, {"lines": ["index: ok"]})
    assert "失败" in settings.diag_summary.text()
    assert "发现 1 个" in settings.diag_summary.text()
    settings._record_diag_operation(kind, f"{kind}: applied")
    assert "失败" not in settings.diag_summary.text() and "一切正常" in settings.diag_summary.text()
    assert "failed" in settings.diagnostic_text.toPlainText()  # 完整日志仍可追查。


def test_cached_preview_still_restores_navigation(preview):
    win = preview
    win._view_page = 1
    win._last_preview_key = (win._cur.path, 1, win._cur.mtime, win._cur.size)
    win._cur_pixmap = QPixmap(100, 80)
    win.page_label.setText("—")
    win.prev_btn.setEnabled(True)
    calls = len(win._render.calls)
    win._request_preview()
    assert "第 1 / 3 页" in win.page_label.text() and not win.prev_btn.isEnabled()
    assert len(win._render.calls) == calls


def test_dark_highlight_and_relaxed_badge_meet_aa(preview):
    tok = theme.tok("atelier_dark")
    channels = [float(x) for x in re.findall(r"[\d.]+", tok["hl_bg"])]
    for base in (tok["panel"], tok["field"], "#203854"):
        mixed = [round(c * channels[3] + b * (1 - channels[3]))
                 for c, b in zip(channels[:3], _rgb(base))]
        assert (_luminance(_rgb(tok["hl_fg"])) + .05) / (_luminance(mixed) + .05) >= 4.5
    result = replace(preview._cur, relaxed=True)
    card = main_window.ResultItem(result, tok, theme.highlight_css("atelier_dark"))
    label = next(label for label in card.findChildren(QLabel) if label.text() == "联想")
    color = re.search(r"color:(#[\dA-Fa-f]+)", label.styleSheet())[1]
    assert color == "#A78BFA"
    assert (_luminance(_rgb(color)) + .05) / (_luminance(_rgb(tok["field"])) + .05) >= 4.5
    card.deleteLater()


@pytest.mark.parametrize("control", ["sort_combo", "match_mode_combo", "type_filter", "mode"])
def test_real_combo_popup_returns_keyboard_focus(preview, qtbot, control):
    win = preview
    win.activateWindow()
    combo = getattr(win, control)
    combo.showPopup()
    view = combo.view()
    qtbot.wait(200)  # 与用户展开后再点击一致，避开 Qt 的弹出防误触时间。
    target = combo.model().index((combo.currentIndex() + 1) % combo.count(), 0)
    qtbot.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualRect(target).center())
    def focus_restored():
        assert QApplication.focusWidget() is win.search_box, (
            f"focus={QApplication.focusWidget()!r}, index={combo.currentIndex()}, popup={view.isVisible()}")
    qtbot.waitUntil(focus_restored)
    assert not view.isVisible()


def test_refresh_keeps_selected_history_version_expanded(preview):
    from test_ui_roi_batch import _grouped_results
    win = preview
    results = _grouped_results()
    _install_results(win, results)
    win._toggle_version_group(1)
    win.result_list.setCurrentRow(win._row_for_result_index(1))  # 点击已展开的历史成员。
    assert win._cur.path == results[1].path
    win._view_page, win._zoom = 2, 2.0
    win._search_keep_selection = True
    win._finish_search("命中", results)
    assert win._cur.path == results[1].path and 1 in win._expanded_groups
    assert (win._view_page, win._zoom) == (2, 2.0)
