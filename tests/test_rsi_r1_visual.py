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


def test_material_empty_state_is_centered_and_switches_with_items(qtbot, tmp_path):
    library = MaterialLibrary(tmp_path / "materials")
    win = MaterialWindow(library=library)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: not win._tasks)
    assert win._list_stack.currentWidget() is win._empty_view
    assert "还没有素材" in win._empty_title.text()
    assert win.status.text() == ""                 # 底部不再重复一遍

    item = Material("b" * 32, "红色箭头", "箭头", 1)
    folder = library.folder(item.id)
    folder.mkdir(parents=True)
    library._write(folder, item)
    image = QImage(40, 20, QImage.Format_RGB32)
    image.fill(Qt.red)
    image.save(str(folder / "preview.png"))
    win._loaded([item])
    assert win._list_stack.currentWidget() is win.list_widget
    win.search.setText("没有这个")
    assert win._list_stack.currentWidget() is win._empty_view
    assert "没有符合条件" in win._empty_title.text()


def test_health_tab_shows_plain_summary_and_folds_raw_log(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    dlg = SettingsDialog(None)
    qtbot.addWidget(dlg)
    assert dlg.diagnostic_text.isHidden()
    dlg._render_diag_summary(["diagnostic_summary: 发现 1 个", "diagnostic_issue: 开机自启目标无效，需要重新写入"])
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
    assert not dlg._index_roots_empty.isHidden()
    assert dlg.index_roots_list.isHidden()
    dlg._append_index_root_item(str(tmp_path))
    assert dlg._index_roots_empty.isHidden()
    assert not dlg.index_roots_list.isHidden()
