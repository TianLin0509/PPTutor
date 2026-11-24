"""RSI 第 1 轮（2026-11-22）观感/引导：零结果收起预览、素材库空状态居中、健康诊断先给人话结论、索引范围空列表有说明。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from test_ui import StubRender, _index

from pptx_finder.materials import Material, MaterialLibrary
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.material_window import MaterialWindow
from pptx_finder.ui.settings_dialog import SettingsDialog


def test_zero_result_hides_preview_and_results_bring_it_back(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("绝对不存在的词xyz123")
    win._do_search()
    assert win._preview_panel.isHidden()          # 没结果：右侧整块空预览收起
    win.search_box.setText("算力方案")
    win._do_search()
    assert win.result_list.count() >= 1
    assert not win._preview_panel.isHidden()


def test_material_empty_state_is_centered_and_switches_with_items(qtbot, tmp_path, monkeypatch):
    from pptx_finder import materials, team_materials
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(materials, "builtin_core_dir", lambda: tmp_path / "no-core")
    team_materials._started = False
    library = MaterialLibrary(tmp_path / "materials")
    win = MaterialWindow(library=library)
    qtbot.addWidget(win)
    win.show()
    tab = win.tab
    qtbot.waitUntil(lambda: tab._loaded, timeout=4000)
    assert tab.stack.currentWidget() is tab.empty
    assert "还没有" in tab.empty_title.text()
    assert tab.status.text() == ""                 # 底部不再重复一遍

    item = Material("b" * 32, "红色箭头", "箭头", 1)
    folder = library.folder(item.id)
    folder.mkdir(parents=True)
    library._write(folder, item)
    image = QImage(40, 20, QImage.Format_RGB32)
    image.fill(Qt.red)
    image.save(str(folder / "preview.png"))
    tab.refresh_library()
    qtbot.waitUntil(lambda: len(tab._all) == 1, timeout=4000)
    assert tab.stack.currentWidget() is tab.grid
    tab.search.setText("没有这个")
    tab._apply_filter()
    assert tab.stack.currentWidget() is tab.empty
    assert "没有符合条件" in tab.empty_title.text()


def test_health_tab_shows_plain_summary_and_folds_raw_log(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    assert dlg.diagnostic_text.isHidden()
    dlg._render_diag_summary(["autostart: preference=on actual=off"])
    assert "发现 1 个需要关注的问题" in dlg.diag_summary.text()
    assert "开机自启目标无效" in dlg.diag_summary.text()
    dlg._render_diag_summary(["diagnostic_summary: 未发现明显异常"])
    assert "一切正常" in dlg.diag_summary.text()
    dlg.diag_toggle.setChecked(True)
    assert not dlg.diagnostic_text.isHidden()


def test_version_feature_off_without_manager_is_not_reported_as_fault(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "data_dir:" in dlg.diagnostic_text.toPlainText(), timeout=5000)
    text = dlg.diagnostic_text.toPlainText()
    assert "versions: unavailable" not in text
    assert "versions: disabled" in text


def test_index_roots_empty_list_explains_default(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    dlg.index_roots_list.clear()
    dlg._sync_index_roots_empty()
    assert not dlg._index_roots_empty.isHidden()
    assert dlg.index_roots_list.isHidden()
    dlg._append_index_root_item(str(tmp_path))
    assert dlg._index_roots_empty.isHidden()
    assert not dlg.index_roots_list.isHidden()


# ---------- Codex 审查补的三条 ----------

def test_health_summary_does_not_claim_ok_when_diagnostics_failed(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    dlg._render_diag_summary(["diagnostics: unavailable"])
    assert "诊断没能完成" in dlg.diag_summary.text()
    assert "一切正常" not in dlg.diag_summary.text()


def test_deep_vault_check_failure_updates_visible_summary(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    dlg.diagnostic_text.setPlainText("data_dir: x")
    dlg._rerender_diag_summary()
    assert "一切正常" in dlg.diag_summary.text()
    dlg._on_vault_audit_done({"ok": False, "versions_checked": 10, "missing_objects": 2, "hash_errors": 1})
    assert "版本库深度检查发现问题" in dlg.diag_summary.text()


def test_health_summary_reports_true_issue_total(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    lines = [f"renderer_ipc_last_error: 错误{i}" for i in range(8)]
    dlg._render_diag_summary(lines)
    assert "发现 8 个需要关注的问题" in dlg.diag_summary.text()
    assert "只列出前 6 项" in dlg.diag_summary.text()


def test_settings_destroy_with_index_roots_does_not_touch_deleted_widgets(qtbot, tmp_path, monkeypatch):
    """析构时列表逐项删除不能回调到已删除的提示标签（2026-11-22 闸门 0xC0000005）。"""
    import shiboken6

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    for i in range(3):
        dlg._append_index_root_item(str(tmp_path / f"d{i}"))
    shiboken6.delete(dlg)
    qtbot.wait(50)
