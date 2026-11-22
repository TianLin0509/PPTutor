"""RSI 第四轮：功能、设置与概览；Office 默认只用模拟对象。"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu, QScrollArea

from pptx_finder import ppt_transfer as transfer
from pptx_finder.ui import main_window as main_mod, settings_dialog as settings_mod, theme
from pptx_finder.ui.dashboard_view import DashboardView
from pptx_finder.ui.settings_dialog import SettingsDialog
from test_ppt_transfer import deck
from test_rsi_r3_slide_take import preview


def test_imports_use_current_worktree():
    root = Path(__file__).resolve().parents[1]
    for module in (transfer, main_mod, settings_mod):
        assert Path(module.__file__).resolve().is_relative_to(root)


def test_targets_include_all_instances_and_insert_in_original_instance(monkeypatch, tmp_path):
    first, second = deck(), deck()  # 同一路径、同窗口标题也不能跨实例插入。
    apps = []
    for hwnd, decks in ((1, []), (2, [first]), (3, [second])):
        apps.append(SimpleNamespace(
            HWND=hwnd, Presentations=SimpleNamespace(Count=len(decks), Item=lambda i, ds=decks: ds[i - 1]),
            StartNewUndoEntry=lambda: None))
    monkeypatch.setattr(transfer, "_app_identity", lambda app: app.HWND, raising=False)

    @contextmanager
    def contexts():
        yield apps

    monkeypatch.setattr(transfer, "existing_apps", contexts, raising=False)
    targets = transfer.list_presentations()
    assert len(targets) == 2
    chosen = targets[1]
    apps.reverse()
    source = tmp_path / "source.pptx"
    source.touch()
    assert transfer.append_slide(source, chosen) == 4
    assert not first.calls and len(second.calls) == 1
    apps.pop(0)
    with pytest.raises(transfer.TransferError, match="已关闭"):
        transfer.append_slide(source, chosen)
    assert not first.calls


def test_rot_enumeration_deduplicates_instances_and_skips_wps(monkeypatch, tmp_path):
    import pythoncom
    import win32com.client
    power = tmp_path / "powerpoint"
    power.mkdir()
    (power / "POWERPNT.EXE").touch()
    apps = []
    for hwnd, folder, decks in ((1, power, []), (2, power, [deck()]), (3, tmp_path / "wps", [])):
        apps.append(SimpleNamespace(
            HWND=hwnd, Path=str(folder),
            Presentations=SimpleNamespace(Count=len(decks), Item=lambda i, ds=decks: ds[i - 1])))
    objects = [apps[0], SimpleNamespace(Application=apps[1]), apps[1], apps[2]]
    names = ["!{91493441-5A91-11CF-8700-00AA0060263B}", "C:/work/deck.pptx",
             "!{91493441-5A91-11CF-8700-00AA0060263B}", "C:/work/wps.pptx"]
    objects[-1] = SimpleNamespace(Application=apps[2])
    monikers = [SimpleNamespace(GetDisplayName=lambda ctx, unused, name=name: name) for name in names]
    rot = SimpleNamespace(EnumRunning=lambda: iter(monikers), GetObject=lambda m:
                          SimpleNamespace(QueryInterface=lambda iid: objects[monikers.index(m)]))
    monkeypatch.setattr(pythoncom, "GetRunningObjectTable", lambda: rot)
    monkeypatch.setattr(pythoncom, "CreateBindCtx", lambda _: None)
    monkeypatch.setattr(win32com.client, "Dispatch", lambda obj: obj)
    monkeypatch.setattr(win32com.client.gencache, "EnsureDispatch", lambda app: app)
    monkeypatch.setattr(transfer, "_app_identity", lambda app: app.HWND)
    assert transfer._find_apps() == apps[:2]
    assert transfer._find_app() is apps[1]


@pytest.fixture
def settings(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(SettingsDialog, "schedule_diagnostics_refresh", lambda *a, **k: None)
    dlg = SettingsDialog(SimpleNamespace())
    qtbot.addWidget(dlg)
    dlg.diagnostic_text.setPlainText("index: 1 files / 3 pages")
    return dlg


def test_deep_audit_exception_reports_reason_and_clears_running(settings, qtbot):
    def fail(**kwargs):
        raise RuntimeError("对象池不可读")
    settings._mgr = SimpleNamespace(audit_repository=fail)
    settings._check_vault_repository()
    qtbot.waitUntil(lambda: not settings._vault_audit_inflight)
    assert settings.vault_audit_btn.isEnabled()
    assert "对象池不可读" in settings.diag_summary.text()
    assert "重试" in settings.diag_summary.text()
    assert "正在深度检查" not in settings.diag_summary.text()
    assert "running deep" not in settings.diagnostic_text.toPlainText()


@pytest.mark.parametrize("result", [None, {"unavailable": True}])
def test_deep_audit_invalid_result_is_visible(settings, result):
    settings._on_vault_audit_done(result)
    assert "未能完成" in settings.diag_summary.text()
    assert "重试" in settings.diag_summary.text()


def test_latest_deep_result_survives_refresh_without_old_failures(settings):
    settings._on_vault_audit_done({"ok": False, "hash_errors": 2})
    assert "需要关注" in settings.diag_summary.text()
    settings._on_vault_audit_done({"ok": True, "objects_hashed": 8})
    assert "一切正常" in settings.diag_summary.text()
    assert "ok=False" not in settings.diagnostic_text.toPlainText()
    for _ in range(2):
        settings._on_diagnostics_ready(settings._diag_refresh_token, {
            "lines": ["index: 1 files / 3 pages", "vault_fsck: ok=False versions=3 hash_errors=2"]})
        assert "一切正常" in settings.diag_summary.text()
        assert "objects=8" in settings.diagnostic_text.toPlainText()


def test_refresh_during_audit_keeps_running_note(settings):
    settings._vault_audit_inflight = True
    settings._vault_audit_line = "vault_fsck_manual: running deep object verification..."
    settings._vault_audit_note = "正在深度检查版本库，大库约需一分钟…"
    settings._on_diagnostics_ready(settings._diag_refresh_token, {"lines": ["index: 1 files / 3 pages"]})
    assert "正在深度检查" in settings.diag_summary.text()


@pytest.mark.parametrize("ext,is_dir", [(".txt", False), (".docx", False), (".pdf", False), (".pptx", True)])
def test_context_menu_only_offers_jump_for_ppt(preview, monkeypatch, ext, is_dir):
    menus = []
    class Menu(QMenu):
        def exec(self, *args):
            menus.append(self)
    monkeypatch.setattr(main_mod, "QMenu", Menu)
    preview._cur.ext, preview._cur.is_dir = ext, is_dir
    item = preview.result_list.currentItem()
    preview._context_menu(preview.result_list.visualItemRect(item).center())
    labels = [a.text() for a in menus[-1].actions()]
    assert "打开文件" in labels
    assert "打开并跳到命中页" not in labels


def test_filename_only_text_copy_disabled_with_reason(preview):
    preview._cur.status = "filename_only"
    preview._cur.file_id = -1
    preview._update_preview_header(preview._cur)
    preview._set_ops_enabled(True)
    assert not preview.copy_text_btn.isEnabled()
    assert "仅登记文件名" in preview.copy_text_btn.toolTip()
    preview._cur.status, preview._cur.file_id = "ok", 1
    preview._update_preview_header(preview._cur)
    preview._set_ops_enabled(True)
    assert preview.copy_text_btn.isEnabled()
    assert "纯文本" in preview.copy_text_btn.toolTip()


@pytest.mark.parametrize("ext,is_dir", [(".docx", False), (".pdf", False), ("", True)])
def test_filename_only_preview_does_not_claim_fulltext(preview, ext, is_dir):
    preview._cur.status, preview._cur.is_dir = "filename_only", is_dir
    preview._show_non_powerpoint_preview(ext)
    text = preview.image_label.text()
    assert "内容已全文索引" not in text
    assert "文件夹" in text if is_dir else "仅文件名" in text
    preview._cur.status, preview._cur.is_dir = "ok", False
    if ext:
        preview._show_non_powerpoint_preview(ext)
        assert "内容已全文索引" in preview.image_label.text()


def test_development_update_center_gives_feedback(preview):
    preview._updater = None
    preview._open_update_center()
    assert "开发版" in preview._toast_label.text()
    opened = []
    preview._updater = SimpleNamespace(open_dialog=lambda: opened.append(True))
    preview._open_update_center()
    assert opened == [True]
    preview._updater = None


def test_diagnostics_card_and_log_use_remaining_height(settings, qtbot):
    settings._on_diagnostics_ready(settings._diag_refresh_token, {"lines": ["index: 1 files / 3 pages"]})
    settings.show_tab("健康诊断")
    settings.resize(1100, 800)
    settings.show()
    assert settings.diag_summary.parentWidget().objectName() == "setCard"
    assert settings.vault_audit_btn.parentWidget() is settings.diag_summary.parentWidget()
    assert settings.diag_toggle.text().startswith("展开")
    settings.diag_toggle.click()
    qtbot.wait(20)
    assert settings.diag_toggle.text().startswith("收起")
    assert settings.diagnostic_text.height() > 300
    dest = Path(__file__).resolve().parents[1] / "artifacts" / "rsi-r4-health.png"
    dest.parent.mkdir(exist_ok=True)
    assert settings.grab().save(str(dest))


def test_index_roots_primary_save_and_remove_selection(settings):
    assert settings._index_roots_save.objectName() == "primary"
    assert not settings._index_root_remove.isEnabled()
    settings._append_index_root_item("D:/work")
    assert not settings._index_root_remove.isEnabled()
    settings.index_roots_list.setCurrentRow(0)
    assert settings._index_root_remove.isEnabled()
    settings._remove_index_root()
    assert not settings._index_root_remove.isEnabled()
    settings._set_index_root_editing_enabled(False)
    settings._set_index_root_editing_enabled(True)
    assert not settings._index_root_remove.isEnabled()


def test_settings_destroy_with_selected_root_is_safe(qtbot, monkeypatch, tmp_path):
    import shiboken6
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(SettingsDialog, "schedule_diagnostics_refresh", lambda *a, **k: None)
    dlg = SettingsDialog(None)
    dlg._append_index_root_item("D:/selected")
    dlg.index_roots_list.setCurrentRow(0)
    assert dlg._index_root_remove.isEnabled()
    shiboken6.delete(dlg)
    qtbot.wait(50)


@pytest.fixture
def dashboard(qtbot):
    calls = []
    win = SimpleNamespace(_conn=None, _version_mgr=None, _tok=theme.tok("atelier"),
                          _version_feature_enabled=lambda: False,
                          open_settings_page=lambda page: calls.append(page))
    dash = DashboardView(win)
    qtbot.addWidget(dash)
    dash._test_calls = calls
    return dash


def test_dashboard_disabled_version_state_is_consistent_and_clickable(dashboard):
    payload = dashboard._build_payload()
    assert "未开启" in payload["kpi_vals"][1][2]
    assert "开启" in payload["week_shield_text"]
    dashboard._apply_payload(payload)
    dashboard._apply_data()
    dashboard._open_kpi(1)
    assert dashboard._test_calls == ["功能"]
    dashboard._week_shield.linkActivated.emit("features")
    assert dashboard._test_calls == ["功能", "功能"]


def test_dashboard_recent_replaces_empty_label_with_real_sqlite_results(dashboard, tmp_path, qtbot):
    from test_ui import _index_multi
    conn = _index_multi(tmp_path, {"recent.pptx": ["已索引内容"]})
    dashboard._win._conn = conn
    old = dashboard._rec_box.itemAt(0).widget()
    dashboard.refresh(force=True)
    qtbot.waitUntil(lambda: bool(dashboard._recent) and not dashboard._refresh_tasks)
    # 旧空态必须立即移除，不能等 deleteLater 才消失。
    from shiboken6 import isValid
    assert not isValid(old) or old.parent() is None
    assert dashboard._rec_box.count() == len(dashboard._recent)
    assert all(hasattr(dashboard._rec_box.itemAt(i).widget(), "path") for i in range(dashboard._rec_box.count()))
    conn.close()


def test_main_window_initial_dashboard_loads_existing_index(qtbot, tmp_path, monkeypatch):
    from test_ui import _index_multi, StubRender
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    conn = _index_multi(tmp_path, {"已有文稿.pptx": ["正文"]})
    win = main_mod.MainWindow(conn=conn, render_worker=StubRender(), do_index=False,
                             index_all_files_enabled=False)
    qtbot.addWidget(win)
    win.resize(1440, 900)
    win.show()
    qtbot.waitUntil(lambda: bool(win.dashboard._recent) and not win.dashboard._refresh_tasks)
    assert win.dashboard._recent[0][0] == "已有文稿.pptx"
    row = win.dashboard._rec_box.itemAt(0).widget()
    assert hasattr(row, "path")
    qtbot.waitUntil(row.isVisible)
    assert row.width() > 100 and row.height() > 15
    qtbot.wait(50)
    assert win.grab().save(str(Path(__file__).resolve().parents[1] / "artifacts" / "rsi-r4-main-dashboard-1440.png"))


def test_dashboard_empty_copy_and_numbers_fit_real_fonts(dashboard, qtbot):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.setWidgetResizable(True)
    area.setWidget(dashboard)
    area.setPalette(theme.build_palette("atelier"))
    area.setAutoFillBackground(True)
    area.setStyleSheet(theme.build_qss("atelier"))
    area.resize(1440, 900)
    area.show()
    dashboard._apply_payload(dashboard._build_payload())
    dashboard._topics = [("本周", 4)]
    dashboard._health = {"score": 100, "deck_count": 4}
    dashboard._apply_data()
    qtbot.wait(50)
    labels = [dashboard._health_score, dashboard._leg_rows[0][2]]
    for label in labels:
        assert label.width() >= label.fontMetrics().horizontalAdvance(label.text()) + 4
    assert "暂未" in dashboard._rec_box.itemAt(0).widget().text()
    dest = Path(__file__).resolve().parents[1] / "artifacts" / "rsi-r4-dashboard-1440.png"
    try:
        assert area.grab().save(str(dest))
    finally:
        area.takeWidget()  # dashboard 由独立 fixture 管理，避免父窗口先析构导致重复清理。


def test_transfer_tooltips_explain_destination_and_distinguish_plaintext(preview):
    assert "纯文本" in preview.copy_text_btn.toolTip()
    assert "无需打开 PowerPoint" in preview.copy_text_btn.toolTip()
    for button in (preview.copy_slide_btn, preview.insert_slide_btn):
        assert "先在 PowerPoint 中打开目标文稿" in button.toolTip()

