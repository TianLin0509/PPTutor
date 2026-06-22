"""ROI 批次 UI 优化回归（#1 版本组折叠 / #3 复制本页文字 / #4 命中计数 / #9 主题明暗标志）。

版本折叠只在「相关度」默认视图生效（同组结果 search 已排成相邻）；用手工构造的
FileResult（带 group_id / is_latest）做确定性测试，不依赖 MinHash 阈值。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from test_ui import StubRender, _index_multi

from pptx_finder import config
from pptx_finder.models import FileResult, SearchHit
from pptx_finder.ui import theme
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.settings_dialog import HotkeyEdit, SettingsDialog
from pptx_finder.versioning.manager import VersionManager


def _grouped_results():
    """3 个同组版本（gid=1，proj_v0 为最新）相邻 + 1 个独立文件（gid=None）。"""
    group = [
        FileResult(
            file_id=i, path=f"C:/proj_v{i}.pptx", name=f"proj_v{i}.pptx", ext=".pptx",
            mtime=1000 + (3 - i), size=1, page_count=2, status="ok", score=5.0 - i,
            name_hit=False, hits=[SearchHit(1, "命中片段")], group_id=1, is_latest=(i == 0),
        )
        for i in range(3)
    ]
    solo = FileResult(
        file_id=9, path="C:/other.pptx", name="other.pptx", ext=".pptx",
        mtime=900, size=1, page_count=1, status="ok", score=0.5, name_hit=False,
        hits=[SearchHit(1, "命中片段")], group_id=None,
    )
    return group + [solo]


def _render(win, results):
    win._showing_recent = False
    win._results_raw = results
    win._results = results
    win._render_results(results)


def _primary_widget(win):
    """折叠态下找到版本组主卡（带展开器的那条）。"""
    for i in range(win.result_list.count()):
        w = win.result_list.itemWidget(win.result_list.item(i))
        if getattr(w, "_exp_btn", None) is not None:
            return w
    return None


# ---------- #1 版本组折叠 ----------
def test_version_group_collapsed_by_default(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    _render(win, _grouped_results())
    # 组折叠成 1 条主卡 + 1 条独立文件 = 2 行（2 个历史版本被折叠）
    assert win.result_list.count() == 2
    pw = _primary_widget(win)
    assert pw is not None
    assert "2 个历史版本" in pw._exp_btn.text()


def test_version_group_expand_and_collapse(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    _render(win, _grouped_results())
    assert win.result_list.count() == 2

    win._toggle_version_group(1)  # 展开：主卡 + 2 成员 + 独立 = 4
    assert win.result_list.count() == 4
    assert "收起版本" in _primary_widget(win)._exp_btn.text()

    win._toggle_version_group(1)  # 折叠回 2
    assert win.result_list.count() == 2
    assert "2 个历史版本" in _primary_widget(win)._exp_btn.text()


def test_version_group_toggle_stress(qtbot, tmp_path):
    """高频展开/折叠 200 次：行数稳定、无异常、成员项清理干净（就地插入/移除无泄漏）。"""
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    _render(win, _grouped_results())
    for _ in range(200):
        win._toggle_version_group(1)
        assert win.result_list.count() == 4   # 展开：主卡 + 2 成员 + 独立
        win._toggle_version_group(1)
        assert win.result_list.count() == 2   # 折叠回 2
    assert not win._group_member_items.get(1)  # 成员项已全部移除，无残留


def test_version_group_primary_is_latest(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    _render(win, _grouped_results())
    # 主卡（第 0 行）对应的结果应是 is_latest 的 proj_v0
    idx = win.result_list.item(0).data(0x0100)  # Qt.UserRole
    assert win._results[idx].name == "proj_v0.pptx"
    assert win._results[idx].is_latest


def test_version_fold_disabled_when_sort_by_name(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.sort_combo.setCurrentText("文件名")  # 非相关度 → 不折叠，平铺全部 4 条
    _render(win, _grouped_results())
    assert win.result_list.count() == 4
    assert _primary_widget(win) is None


def test_collapse_reselects_primary_when_member_selected(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    _render(win, _grouped_results())
    win._toggle_version_group(1)  # 展开
    # 选中一个历史版本（成员行 idx 指向 proj_v1/v2 之一）
    win.result_list.setCurrentRow(1)
    selected_before = win._cur
    assert selected_before is not None
    win._toggle_version_group(1)  # 折叠 → 选中应回落到组主卡（最新版），不丢选中
    assert win.result_list.count() == 2
    assert win._cur is not None
    assert win._cur.name == "proj_v0.pptx"


# ---------- #3 复制本页文字 ----------
def test_copy_page_text_uses_indexed_raw(qtbot, tmp_path):
    conn = _index_multi(tmp_path, {"deck.pptx": ["第一页内容甲", "第二页内容乙丙丁戊"]})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("内容")
    win._do_search()
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: win._cur is not None, timeout=2000)
    win._view_page = 2
    win._act_copy_page_text()
    assert "乙丙丁戊" in QApplication.clipboard().text()


def test_copy_page_text_button_visibility_follows_selection(qtbot, tmp_path):
    conn = _index_multi(tmp_path, {"deck.pptx": ["内容甲"]})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win.copy_text_btn.isHidden()      # 未选中时隐藏
    win.search_box.setText("内容")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert not win.copy_text_btn.isHidden()  # 选中后显示


# ---------- #4 命中序号计数 ----------
def test_page_label_shows_hit_ordinal(qtbot, tmp_path):
    conn = _index_multi(tmp_path, {"deck.pptx": ["页一 关键词", "页二 别的", "页三 关键词"]})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("关键词")
    win._do_search()
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: win._cur is not None and bool(win._cur.hits), timeout=2000)
    win._goto_hit(0)
    assert "命中 1/2" in win.page_label.text()
    win._step_hit(1)
    assert "命中 2/2" in win.page_label.text()


def test_focus_search_shortcut_method(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("旧查询")
    win._focus_search()
    # selectAll 生效（确定性，不依赖 offscreen 下不稳定的窗口激活/焦点态）
    assert win.search_box.selectedText() == "旧查询"


# ---------- #9 主题明暗标志（修复 titlebar 深浅判定） ----------
def test_theme_is_light_flag():
    assert theme.tok("cloud")["is_light"] is True       # 云白晨光：浅色
    assert theme.tok("ocean")["is_light"] is False      # 深海极光：深色（旧逻辑漏判）
    assert theme.tok("aurora")["is_light"] is False
    # 每套主题都带标志
    for name, _label in theme.THEMES:
        assert "is_light" in theme.tok(name)


# ---------- #2 全局热键可改 ----------
def test_hotkey_persist_and_merge_preserves_theme(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    assert config.get_hotkey() == config.GLOBAL_HOTKEY  # 默认值
    config.set_theme("ocean")
    config.set_hotkey("Ctrl+Alt+J")
    assert config.get_hotkey() == "Ctrl+Alt+J"
    assert config.get_theme() == "ocean"   # 合并写：设热键没清掉主题（修复旧整体覆写 bug）
    config.set_theme("magma")
    assert config.get_hotkey() == "Ctrl+Alt+J"  # 设主题也没清掉热键


def test_set_hotkey_status_label(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.set_hotkey_status("Ctrl+Alt+J", True)
    assert "Ctrl+Alt+J" in win.hotkey_label.text()
    assert "占用" not in win.hotkey_label.text()
    win.set_hotkey_status("Ctrl+Alt+K", False)
    assert "占用" in win.hotkey_label.text() and "点此改" in win.hotkey_label.text()


def test_hotkey_label_click_opens_settings(qtbot, tmp_path):
    win = MainWindow(conn=_index_multi(tmp_path, {"x.pptx": ["占位"]}),
                     render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls = []
    win._open_settings_cb = lambda: calls.append("open")

    class _Press:
        def type(self):
            return QEvent.MouseButtonPress

    handled = win.eventFilter(win.hotkey_label, _Press())
    assert handled is True
    assert calls == ["open"]


def test_hotkey_edit_records_combo(qtbot):
    edit = HotkeyEdit("Ctrl+Alt+P")
    qtbot.addWidget(edit)
    ev = QKeyEvent(QEvent.KeyPress, Qt.Key_J, Qt.ControlModifier | Qt.AltModifier)
    edit.keyPressEvent(ev)
    assert edit.spec() == "Ctrl+Alt+J"
    # 单独按修饰键不改变
    ev2 = QKeyEvent(QEvent.KeyPress, Qt.Key_Control, Qt.ControlModifier)
    edit.keyPressEvent(ev2)
    assert edit.spec() == "Ctrl+Alt+J"


def test_settings_apply_hotkey_validates_and_persists(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg2"))
    mgr = VersionManager()
    try:
        dlg = SettingsDialog(mgr)   # parent=None → 无 _apply_hotkey 回调 → 走「已保存（重启生效）」
        qtbot.addWidget(dlg)
        # 无效：缺修饰键
        dlg._hotkey_edit._spec = "P"
        dlg._apply_hotkey()
        assert "请用" in dlg._hotkey_result.text()
        assert config.get_hotkey() == config.GLOBAL_HOTKEY  # 未改
        # 有效：持久化
        dlg._hotkey_edit._spec = "Ctrl+Alt+J"
        dlg._apply_hotkey()
        assert config.get_hotkey() == "Ctrl+Alt+J"
        assert ("已保存" in dlg._hotkey_result.text()) or ("已生效" in dlg._hotkey_result.text())
        # 只有 Shift（或只有 Win）应被拒——会劫持正常打字 / 撞系统快捷键
        dlg._hotkey_edit._spec = "Shift+P"
        dlg._apply_hotkey()
        assert "请用" in dlg._hotkey_result.text()
        assert config.get_hotkey() == "Ctrl+Alt+J"  # 未被改写
    finally:
        mgr.stop()
