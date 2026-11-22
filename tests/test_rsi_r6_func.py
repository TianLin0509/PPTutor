"""RSI 第六轮：日常搜索状态、键盘、延迟退出与诊断摘要。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import re
import threading
import time

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTextEdit

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
    output.parent.mkdir(exist_ok=True)
    assert win.grab().save(str(output))


def test_pending_transfer_defers_close(preview, monkeypatch):
    win = preview
    win._bg_tasks.append(SimpleNamespace(_label="ppt-transfer"))
    calls = []
    shutdown = win._shutdown
    monkeypatch.setattr(win, "_shutdown", lambda: (calls.append("shutdown"), shutdown()))
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
    qtbot.waitUntil(view.isVisible)
    qtbot.wait(350)  # 给原生弹层防误触留出余量，避免卡在 200 ms 的边界上。
    target = combo.model().index((combo.currentIndex() + 1) % combo.count(), 0)
    pos = view.visualRect(target).center()
    qtbot.mouseMove(view.viewport(), pos=pos)
    qtbot.mouseClick(view.viewport(), Qt.LeftButton, pos=pos)
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
    win._toggle_version_group(1)
    assert win.result_list.count() == 2
    assert win._cur.path == results[0].path
    win._toggle_version_group(1)
    assert win.result_list.count() == 4


@pytest.mark.parametrize("reopen_early", [False, True])
def test_collapsed_group_does_not_reappear_in_later_batches(preview, reopen_early):
    win = preview
    results = [replace(r, group_id=1, is_latest=i == 0) for i, r in enumerate(_results(30))]
    _install_results(win, results)
    win._toggle_version_group(1)
    win._search_keep_selection = True
    win._finish_search("命中", results)
    win._toggle_version_group(1)
    if reopen_early:
        win._toggle_version_group(1)
    while win._render_more_item is not None:
        win._load_more_results()
    if reopen_early:
        assert win.result_list.count() == len(results)
        win._toggle_version_group(1)
    assert win.result_list.count() == 1
    win._toggle_version_group(1)
    assert win.result_list.count() == len(results)


def test_transfer_quit_timeout_is_bounded_and_logged(preview, monkeypatch, caplog):
    win = preview
    task = SimpleNamespace(_label="ppt-transfer")
    calls = []
    shutdown = win._shutdown
    monkeypatch.setattr(win, "_shutdown", lambda: (calls.append("shutdown"), shutdown()))
    monkeypatch.setattr(QApplication, "quit", lambda: calls.append("quit"))
    win._bg_tasks.append(task)
    try:
        started = time.monotonic()
        win.force_quit()
        assert started + 10 <= win._storage_quit_deadline <= time.monotonic() + 15
        win._finish_storage_quit()
        assert not calls
        win._storage_quit_deadline = time.monotonic() - 1
        win._finish_storage_quit()
        assert calls == ["shutdown", "quit"]
        assert win._bg_task_shutdown_wait_ms(task) == 0
        assert "ppt-transfer" in caplog.text and "timed out" in caplog.text
        win._finish_storage_quit()  # 已排队的轮询不能重复退出。
        assert calls == ["shutdown", "quit"]
    finally:
        win._bg_tasks.remove(task)


def test_transfer_timeout_never_bypasses_vault_transaction(preview, monkeypatch):
    win = preview
    calls = []
    shutdown = win._shutdown
    monkeypatch.setattr(win, "_shutdown", lambda: (calls.append("shutdown"), shutdown()))
    win._bg_tasks.extend([SimpleNamespace(_label="ppt-transfer"),
                          SimpleNamespace(_label="vault-storage-configure")])
    win._to_tray_on_close = False
    try:
        win.close()
        win._storage_quit_deadline = time.monotonic() - 1
        win._finish_storage_quit()
        assert not calls and win._storage_quit_pending
    finally:
        win._bg_tasks.clear()
    win._finish_storage_quit()
    assert calls == ["shutdown"]


@pytest.mark.parametrize("state", ["pending", "closing"])
def test_insert_chooser_does_not_open_during_quit(preview, monkeypatch, state):
    win = preview
    callbacks, dialogs = [], []
    monkeypatch.setattr(win, "_run_slide_transfer", lambda fn, done, operation: callbacks.append(done))
    monkeypatch.setattr(main_window.QInputDialog, "getItem", lambda *a: dialogs.append(a))
    win._insert_preview_slide(win._cur.path, 2)
    setattr(win, "_storage_quit_pending" if state == "pending" else "_closing", True)
    try:
        callbacks[0]([SimpleNamespace(name="target", slides=3, full_name="target.pptx")])
        assert dialogs == [] and len(callbacks) == 1
    finally:
        win._closing = False
        win._storage_quit_pending = False


def test_inflight_target_enumeration_quits_without_chooser(preview, qtbot, monkeypatch):
    win = preview
    entered, release = threading.Event(), threading.Event()
    dialogs, quits = [], []

    def enumerate_targets():
        entered.set()
        assert release.wait(5)
        return [SimpleNamespace(name="target", slides=3, full_name="target.pptx")]

    monkeypatch.setattr(main_window.ppt_transfer, "list_presentations", enumerate_targets)
    monkeypatch.setattr(main_window.QInputDialog, "getItem",
                        lambda *args: (dialogs.append(args) or "", False))
    monkeypatch.setattr(QApplication, "quit", lambda: quits.append(True))
    win._insert_preview_slide(win._cur.path, 2)
    try:
        qtbot.waitUntil(entered.is_set)
        win.force_quit()
        assert win._storage_quit_pending and not quits
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(quits))
    assert not dialogs and win._closing


@pytest.mark.parametrize("page", ["search", "versions"])
@pytest.mark.parametrize("widget_type", [QLineEdit, QPlainTextEdit, QTextEdit])
def test_escape_and_focus_restore_respect_other_inputs(preview, qtbot, page, widget_type):
    win = preview
    win._switch_page("search")
    if page == "versions":
        # 真实侧栏切换需要版本后端；只替换页码查询，仍使用主窗内真实输入框。
        win._current_page_key = lambda: "versions"
    field = widget_type(win)
    field.setPlainText("保留") if hasattr(field, "setPlainText") else field.setText("保留")
    field.show()
    win.activateWindow()
    field.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is field)
    qtbot.keyClick(field, Qt.Key_Escape)
    assert win.search_box.text() == "命中" and win.isVisible()
    win._restore_search_focus()
    assert QApplication.focusWidget() is field


def test_escape_on_other_page_does_not_clear_or_hide(preview, qtbot, monkeypatch):
    win = preview
    monkeypatch.setattr(win, "_current_page_key", lambda: "versions")
    qtbot.keyClick(win.open_btn, Qt.Key_Escape)
    assert win.search_box.text() == "命中" and win.isVisible()
    win.search_box.clear()
    qtbot.keyClick(win.open_btn, Qt.Key_Escape)
    assert win.isVisible()


def test_facet_apply_keeps_popup_focus(preview, qtbot):
    win = preview
    win.activateWindow()
    win._toggle_facet()
    win.facet_panel._clear_btn.setFocus()
    win.facet_panel._toggle("page", "1-10", True)
    assert not win.facet_panel.isHidden()
    assert QApplication.focusWidget() is win.facet_panel._clear_btn


@pytest.mark.parametrize("outcome", ["inflight", "locked"])
def test_repeated_new_query_does_not_inherit_old_query_state(preview, outcome):
    win = preview
    win.facet_panel._toggle("page", "1-10", True)
    win._zoom = 2.25
    win._search_worker = PendingSearchWorker()
    win.search_box.setText("新词")
    win._do_search()
    assert win._last_query_text == "命中"
    if outcome == "locked":
        win._on_search_done(win._search_seq, "新词", [], 1, "database is locked")
    win._do_search()
    assert not win._search_keep_selection
    win._on_search_done(win._search_seq, "新词", _results(2), 1, None)
    assert win._last_query_text == "新词"
    assert win._facet_filters == {} and win._view_page == 1 and win._zoom == 1.0
    win._clear_search_now()
    assert win._last_query_text == ""


@pytest.mark.parametrize("operation", ["search", "recent", "theme"])
def test_facet_empty_clears_preview_and_survives_theme(preview, operation):
    win = preview
    results = _results(2)
    _install_results(win, results)
    win.facet_panel._toggle("type", "pptx", True)
    win.facet_panel._toggle("page", "1-10", True)
    changed = [replace(results[0], page_count=40), replace(results[1], ext=".pdf")]
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.red)
    win.image_label.setPixmap(pixmap)
    if operation == "recent":
        win._recent_keep_selection = True
        win._apply_recent_results(changed)
    else:
        win._search_keep_selection = True
        win._finish_search("命中", changed)
        if operation == "theme":
            win._apply_theme("atelier_dark", persist=False)
    assert win._results_raw and not win._results and win._facet_filters
    assert win._cur is None and win.image_label.pixmap().isNull()
    assert "筛选后没有可预览结果" in win.image_label.text()
    assert not win.empty_hint.isHidden() and win._empty_query_label.text() == "筛选后没有结果"
    assert win.result_count.text() == "筛选出 0 / 2 个文件"


@pytest.mark.parametrize("recent", [False, True])
def test_facet_count_is_consistent_on_apply_refresh_and_clear(preview, recent):
    win = preview
    if recent:
        win._clear_search_now()
        win._recent_keep_selection = False
        win._apply_recent_results(_results(2))
    win.facet_panel._toggle("page", "1-10", True)
    count = win.result_count.text()
    assert count == f"筛选出 {len(win._results)} / {len(win._results_raw)} 个文件"
    if recent:
        win._recent_keep_selection = True
        win._apply_recent_results(win._results_raw)
    else:
        win._search_keep_selection = True
        win._finish_search("命中", win._results_raw)
    assert win.result_count.text() == count
    win.facet_panel._clear_all()
    assert win.result_count.text().startswith("最近修改 · " if recent else "命中 ")


def test_autostart_snapshot_and_failure_count_as_one_issue(settings):
    settings._on_diagnostics_ready(settings._diag_refresh_token,
                                   {"lines": ["autostart: preference=on actual=off"]})
    settings._on_autostart_toggle_done(settings._autostart_toggle_token, True, False)
    assert "发现 1 个" in settings.diag_summary.text()
    assert "失败" in settings.diag_summary.text()


@pytest.mark.parametrize("key,expected", [(Qt.Key_Return, 0), (Qt.Key_Down, 1)])
def test_key_after_debounce_waits_for_async_result(preview, qtbot, monkeypatch, key, expected):
    win = preview
    opened = []
    monkeypatch.setattr(win, "_act_goto", lambda: opened.append(win._cur.path))
    win._search_worker = PendingSearchWorker()
    win.search_box.setText("新词")
    win._do_search()  # 280 ms 防抖已结束，但结果还未到达。
    qtbot.keyClick(win.search_box, key)
    new = _results(2)
    win._on_search_done(win._search_seq, "新词", new, 1, None)
    assert win._cur.path == new[expected].path
    assert opened == ([new[0].path] if key == Qt.Key_Return else [])


@pytest.mark.parametrize("key", [Qt.Key_Down, Qt.Key_Return])
def test_history_popup_owns_search_keys(preview, monkeypatch, key):
    win = preview
    actions = []
    monkeypatch.setattr(win._completer.popup(), "isVisible", lambda: True)
    monkeypatch.setattr(win, "_act_search_key", lambda k: actions.append(k))
    event = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
    assert not win.eventFilter(win.search_box, event)
    assert actions == []
