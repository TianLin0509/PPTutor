"""v2.0 子窗口交互走查修复（2026-10-13）：双击 / 右键 / 快捷键 / 打开所在文件夹 / 中文确认框。

用户原话：首页「最近活跃」的 PPT 不能直接双击打开——「各个功能也都花时间 check 下」。
这里覆盖走查清单里子窗口那一半：版本页、截图、转字、素材库、体检、设置、详情、瘦身、报告。
"""
from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

import pptx_finder.ui.version_window as vw
from pptx_finder.ui import owner_actions


# ---------------------------------------------------------------- 公共：假主窗口
class FakeOwner(QWidget):
    def __init__(self):
        super().__init__()
        self.opened: list[str] = []
        self.folders: list[str] = []
        self.previews: list[str] = []
        self.toasts: list[str] = []

    def _open_file_path(self, path):
        self.opened.append(path)

    def _open_folder_path(self, path):
        self.folders.append(path)

    def preview_path_in_search(self, path):
        self.previews.append(path)

    def _toast(self, text):
        self.toasts.append(text)


def _real_double_click(qtbot, widget, pos):
    """真实双击的事件序列：按下 → 松开 → 双击。列表只认「按下时也在这一项」的双击。"""
    qtbot.mouseClick(widget, Qt.LeftButton, pos=pos)
    qtbot.mouseDClick(widget, Qt.LeftButton, pos=pos)


def _no_menu_exec(monkeypatch):
    # 不进入菜单的模态循环；菜单对象留在窗口的 _last_menu 上供断言
    monkeypatch.setattr(owner_actions, "popup", lambda menu, pos: None)


def _action(menu, text):
    for act in menu.actions():
        if act.text().startswith(text):
            return act
    raise AssertionError(f"菜单里没有「{text}」：{[a.text() for a in menu.actions()]}")


# ---------------------------------------------------------------- owner_actions
def test_owner_actions_prefer_main_window_and_fall_back_to_background(qtbot, monkeypatch):
    owner = FakeOwner()
    qtbot.addWidget(owner)
    child = QWidget(owner)
    owner_actions.open_file(child, "C:/a.pptx")
    owner_actions.open_folder(child, "C:/a.pptx")
    assert owner.opened == ["C:/a.pptx"] and owner.folders == ["C:/a.pptx"]

    calls = []
    import pptx_finder.actions as actions
    monkeypatch.setattr(actions, "open_folder", lambda p: calls.append(p) or True)
    lonely = QWidget()
    qtbot.addWidget(lonely)
    owner_actions.open_folder(lonely, "C:/b.pptx")
    qtbot.waitUntil(lambda: calls == ["C:/b.pptx"], timeout=2000)


# ---------------------------------------------------------------- 版本页
class _Mgr:
    def __init__(self, docs):
        self.docs = docs
        self.previews = []

    def list_docs_details(self):
        return list(self.docs)

    def list_docs(self):
        return list(self.docs)

    def list_versions_by_doc_details(self, doc_id, limit=None):
        return [{"version_id": f"{doc_id}-v1", "ts": 1_700_000_000, "page_count": 3}]

    def list_versions_by_doc(self, doc_id):
        return self.list_versions_by_doc_details(doc_id)

    def ensure_version_preview(self, vid):
        self.previews.append(vid)
        return None


def _version_window(qtbot, docs):
    owner = FakeOwner()
    qtbot.addWidget(owner)
    owner.resize(900, 600)
    win = vw.VersionWindow(_Mgr(docs), owner, embedded=True)
    qtbot.addWidget(win)
    win.resize(900, 600)
    owner.show()
    qtbot.waitExposed(owner)
    # 先出现的是「正在加载版本文档…」占位行；等真正的文档行铺上来
    qtbot.waitUntil(lambda: win._docs_inflight_token is None and win.doc_list.count() >= 1
                    and win.doc_list.item(0).data(Qt.UserRole) is not None, timeout=4000)
    return owner, win


DOCS = [{"doc_id": "d1", "path": "C:/work/季度汇报.pptx", "status": "active"},
        {"doc_id": "d2", "path": "C:/work/旧稿.pptx", "status": "deleted"}]


def test_version_doc_list_double_click_opens_and_context_menu(qtbot, monkeypatch):
    owner, win = _version_window(qtbot, DOCS)
    item = win.doc_list.item(0)
    rect = win.doc_list.visualItemRect(item)
    _real_double_click(qtbot, win.doc_list.viewport(), rect.center())
    assert owner.opened == ["C:/work/季度汇报.pptx"]

    _no_menu_exec(monkeypatch)
    win._doc_context_menu(rect.center())
    menu = win._last_menu
    _action(menu, "在搜索页预览").trigger()
    _action(menu, "打开所在文件夹").trigger()
    assert owner.previews == ["C:/work/季度汇报.pptx"]
    assert owner.folders == ["C:/work/季度汇报.pptx"]

    # 已删除的文档：双击不去打开一个不存在的文件，而是提示可以找回
    deleted = win.doc_list.item(1)
    win._on_doc_double_clicked(deleted)
    assert owner.opened == ["C:/work/季度汇报.pptx"]
    assert any("找回" in t for t in owner.toasts)


def test_version_timeline_double_click_previews_and_menu(qtbot, monkeypatch):
    _owner, win = _version_window(qtbot, DOCS)
    win.doc_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: win.ver_list.count() >= 1 and bool(win.ver_list.item(0).data(Qt.UserRole)),
                    timeout=4000)
    called = []
    monkeypatch.setattr(win, "_preview_selected_version", lambda: called.append("preview"))
    item = win.ver_list.item(0)
    _real_double_click(qtbot, win.ver_list.viewport(), win.ver_list.visualItemRect(item).center())
    assert called == ["preview"]
    _no_menu_exec(monkeypatch)
    win._version_context_menu(win.ver_list.visualItemRect(item).center())
    texts = [a.text() for a in win._last_menu.actions()]
    assert "生成预览" in texts and any(t.startswith("恢复此版本") for t in texts)


def test_version_focus_doc_clears_filter_and_reports_missing(qtbot):
    owner, win = _version_window(qtbot, DOCS)
    win.doc_filter.setText("不存在的词")
    win._apply_doc_filter()
    assert win.right_title.text() == "没有匹配的文档"
    assert not win.btn_clear_filter.isHidden()

    win.focus_doc("C:/work/季度汇报.pptx")
    qtbot.waitUntil(lambda: win._cur_doc is not None and win._cur_doc[1] == "C:/work/季度汇报.pptx",
                    timeout=4000)
    assert win.doc_filter.text() == ""

    win.focus_doc("C:/work/从没留过版.pptx")
    qtbot.waitUntil(lambda: any("还没有历史版本" in t for t in owner.toasts), timeout=4000)
    assert "还没有历史版本" in win.right_title.text()


def test_version_clear_filter_button_restores_list(qtbot):
    _owner, win = _version_window(qtbot, DOCS)
    win.doc_filter.setText("zzz")
    win._apply_doc_filter()
    qtbot.mouseClick(win.btn_clear_filter, Qt.LeftButton)
    assert win.doc_filter.text() == ""
    assert win.doc_list.count() == 2
    assert win.btn_clear_filter.isHidden()


# ---------------------------------------------------------------- 截图：保存原图可覆盖
def test_screenshot_save_original_overwrites_existing_and_offers_folder(qtbot, tmp_path, monkeypatch):
    import pptx_finder.ui.screenshot_window as sw
    win = sw.ScreenshotWindow(clipboard_writer=lambda *_: None, output_root=tmp_path)
    qtbot.addWidget(win)
    win.setAttribute(Qt.WA_DontShowOnScreen)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen)
    image = QImage(64, 32, QImage.Format_RGB32)
    image.fill(Qt.blue)
    win._paths = []
    win._preview_image = image
    target = tmp_path / "截图.png"
    target.write_bytes(b"old")
    monkeypatch.setattr(sw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    win.save_result()
    qtbot.waitUntil(lambda: "原图已保存" in win.status.text() or "失败" in win.status.text(), timeout=5000)
    assert "原图已保存" in win.status.text()
    assert target.read_bytes() != b"old"
    assert not win.open_saved_btn.isHidden()
    opened = []
    monkeypatch.setattr(owner_actions, "open_folder", lambda w, p: opened.append(p))
    qtbot.mouseClick(win.open_saved_btn, Qt.LeftButton)
    assert opened == [str(target)]


# ---------------------------------------------------------------- 转字：结果的出口
def test_imgtext_result_buttons_open_and_save_as(qtbot, tmp_path, monkeypatch):
    import pptx_finder.ui.imgtext_window as iw
    win = iw.ImgTextWindow({})
    qtbot.addWidget(win)
    for btn in (win._open_result_btn, win._open_folder_btn, win._save_as_btn):
        assert not btn.isEnabled()
    result = tmp_path / "abc123.pptx"
    result.write_bytes(b"pptx-bytes")
    win._result_path = str(result)
    win._sync_buttons()
    assert win._open_result_btn.isEnabled() and win._save_as_btn.isEnabled()
    opened, folders = [], []
    monkeypatch.setattr(owner_actions, "open_file", lambda w, p: opened.append(p))
    monkeypatch.setattr(owner_actions, "open_folder", lambda w, p: folders.append(p))
    qtbot.mouseClick(win._open_result_btn, Qt.LeftButton)
    qtbot.mouseClick(win._open_folder_btn, Qt.LeftButton)
    assert opened == [str(result)] and folders == [str(result)]
    dest = tmp_path / "我的转换"
    monkeypatch.setattr(iw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
    qtbot.mouseClick(win._save_as_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: (tmp_path / "我的转换.pptx").exists(), timeout=4000)
    assert (tmp_path / "我的转换.pptx").read_bytes() == b"pptx-bytes"
    qtbot.waitUntil(lambda: "已另存为" in win._status.text(), timeout=2000)


# ---------------------------------------------------------------- 素材库
def _material_window(qtbot, tmp_path, monkeypatch):
    from pptx_finder.materials import Material, MaterialLibrary
    from pptx_finder.ui.material_window import MaterialWindow
    library = MaterialLibrary(tmp_path / "materials")
    items = [Material("a" * 32, "红色箭头", "箭头", 1)]
    calls = []
    monkeypatch.setattr(library, "list", lambda: list(items))
    monkeypatch.setattr(library, "copy", lambda i: calls.append(("copy", i)))
    monkeypatch.setattr(library, "delete", lambda ids: calls.append(("delete", list(ids))))
    monkeypatch.setattr(library, "capture", lambda n, c: calls.append(("capture", n, c)) or items[0])
    win = MaterialWindow(library=library)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: not win._tasks and win.list_widget.count() == 1, timeout=4000)
    return win, calls


def test_material_keyboard_and_context_menu(qtbot, tmp_path, monkeypatch):
    win, calls = _material_window(qtbot, tmp_path, monkeypatch)
    win.list_widget.item(0).setSelected(True)
    win.list_widget.setFocus()
    qtbot.keyClick(win.list_widget, Qt.Key_C, Qt.ControlModifier)
    qtbot.waitUntil(lambda: ("copy", "a" * 32) in calls, timeout=3000)
    qtbot.waitUntil(lambda: not win._tasks, timeout=3000)
    monkeypatch.setattr(owner_actions, "ask", lambda *a, **k: True)
    win.list_widget.setFocus()
    qtbot.keyClick(win.list_widget, Qt.Key_Delete)
    qtbot.waitUntil(lambda: ("delete", ["a" * 32]) in calls, timeout=3000)
    qtbot.waitUntil(lambda: not win._tasks, timeout=3000)

    _no_menu_exec(monkeypatch)
    rect = win.list_widget.visualItemRect(win.list_widget.item(0))
    win._context_menu(rect.center())
    texts = [a.text().split("\t")[0] for a in win._last_menu.actions() if a.text()]
    assert texts[:2] == ["复制素材（回 PPT 粘贴）", "编辑名称 / 分类"]
    assert "移出素材库" in texts


def test_material_enter_in_name_box_captures(qtbot, tmp_path, monkeypatch):
    win, calls = _material_window(qtbot, tmp_path, monkeypatch)
    win.name_edit.setText("蓝色框")
    qtbot.keyClick(win.name_edit, Qt.Key_Return)
    qtbot.waitUntil(lambda: any(c[0] == "capture" for c in calls), timeout=3000)


# ---------------------------------------------------------------- 体检
def test_health_dup_group_lists_copies_before_recycle(qtbot, monkeypatch):
    from pptx_finder import health
    from pptx_finder.ui import health_window as hw
    g = health.DuplicateGroup(content_hash="sha256:" + "a" * 64,
                              paths=["C:/keep.pptx", "D:/备份/dup1.pptx", "E:/dup2.pptx"],
                              keep_path="C:/keep.pptx", size=1000, reclaimable=2000)
    rep = health.HealthReport(deck_count=3, score=80, duplicate_groups=[g],
                              duplicate_reclaimable=2000, duplicate_redundant=2)
    win = hw.HealthWindow({"win": "#ffffff", "acc": "#2563EB"}, lambda: rep, lambda p: {})
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recycle_btn is not None, timeout=4000)
    toggles = [w for w in win.findChildren(hw.QToolButton) if w.objectName() == "dupToggle"]
    copies = [w for w in win.findChildren(QWidget) if w.objectName() == "dupCopies"]
    assert len(toggles) == 1 and len(copies) == 1
    assert copies[0].isHidden()
    toggles[0].click()
    assert not copies[0].isHidden()
    tips = [lbl.toolTip() for lbl in copies[0].findChildren(hw.QLabel) if lbl.objectName() == "healthLink"]
    assert tips == ["D:/备份/dup1.pptx", "E:/dup2.pptx"]
    # 独立窗（拿不到主窗定位钩子）时，打开文件夹走后台，不在界面线程直接调 explorer
    opened = []
    monkeypatch.setattr(hw.owner_actions, "open_folder", lambda w, p: opened.append(p))
    win._activate_health_link("D:/备份/dup1.pptx", "dup1.pptx")
    assert opened == ["D:/备份/dup1.pptx"]


# ---------------------------------------------------------------- 设置：未保存的索引目录
def test_settings_warns_before_dropping_unsaved_index_roots(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    from pptx_finder.versioning.manager import VersionManager
    from pptx_finder.ui.settings_dialog import SettingsDialog
    mgr = VersionManager()
    try:
        dlg = SettingsDialog(mgr)
        qtbot.addWidget(dlg)
        dlg.setAttribute(Qt.WA_DontShowOnScreen)
        dlg.show()
        dlg.index_root_edit.setText(str(tmp_path))
        dlg._add_index_root_network()
        answers = ["back"]
        monkeypatch.setattr(dlg, "_ask_unsaved_index_roots", lambda: answers.pop(0))
        dlg.reject()
        assert dlg.isVisible()          # 选「返回」：不关
        answers.append("discard")
        dlg.reject()
        assert not dlg.isVisible()      # 选「放弃改动」：关，且不再问第二遍
        assert answers == []
    finally:
        mgr.stop()


def test_settings_save_choice_saves_then_closes(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    from pptx_finder import config
    from pptx_finder.versioning.manager import VersionManager
    from pptx_finder.ui.settings_dialog import SettingsDialog
    mgr = VersionManager()
    try:
        dlg = SettingsDialog(mgr)
        qtbot.addWidget(dlg)
        dlg.setAttribute(Qt.WA_DontShowOnScreen)
        dlg.show()
        dlg.index_root_edit.setText(str(tmp_path))
        dlg._add_index_root_network()
        monkeypatch.setattr(dlg, "_ask_unsaved_index_roots", lambda: "save")
        dlg.reject()
        qtbot.waitUntil(lambda: not dlg.isVisible(), timeout=5000)
        assert str(tmp_path) in config.get_index_roots()
    finally:
        mgr.stop()


# ---------------------------------------------------------------- 详情 Tab
def _file_result(path="C:/work/a.pptx"):
    from pptx_finder.models import FileResult
    return FileResult(file_id=1, path=path, name="a.pptx", ext=".pptx", mtime=0, size=2048,
                      page_count=3, status="ok", score=1, name_hit=False)


def test_detail_path_buttons_and_clickable_thumbnail(qtbot):
    from pptx_finder.ui import theme
    from pptx_finder.ui.detail_panel import DetailPanel
    dp = DetailPanel(theme.tok("atelier"))
    qtbot.addWidget(dp)
    copied, folders, previews = [], [], []
    dp.copy_path_requested.connect(copied.append)
    dp.open_folder_requested.connect(folders.append)
    dp.preview_requested.connect(previews.append)
    versions = [{"version_id": "v2", "ts": 1_700_000_100, "page_count": 3},
                {"version_id": "v1", "ts": 1_700_000_000, "page_count": 3}]
    dp.update_for(_file_result(), versions=versions)
    assert not dp._path_ops.isHidden()
    dp._copy_path_btn.click()
    dp._open_folder_btn.click()
    assert copied == ["C:/work/a.pptx"] and folders == ["C:/work/a.pptx"]
    thumb = dp._version_preview_labels["v1"]
    qtbot.mouseClick(thumb, Qt.LeftButton)
    assert previews == ["v1"]
    restore_btns = [b for b in dp.findChildren(type(dp._copy_path_btn)) if b.text() == "恢复"]
    assert restore_btns and all(b.objectName() == "verBtn" for b in restore_btns)
    dp.clear_selection()
    assert dp._path_ops.isHidden()


# ---------------------------------------------------------------- 瘦身 / 报告：产物可一键打开
def test_slim_window_offers_output_folder_after_success(qtbot, tmp_path, monkeypatch):
    import pptx_finder.ui.slim_window as sw
    from pptx_finder import slim
    from pptx_finder.ui import theme
    monkeypatch.setattr(sw, "QMessageBox", SimpleNamespace(information=lambda *a, **k: None,
                                                           warning=lambda *a, **k: None), raising=False)
    deck = tmp_path / "a.pptx"
    deck.write_bytes(b"x")
    monkeypatch.setattr(sw.SlimWindow, "refresh", lambda self: None)
    win = sw.SlimWindow(theme.tok("atelier"), str(deck))
    qtbot.addWidget(win)
    win._slim_inflight = True
    out = tmp_path / "a_瘦身.pptx"
    result = slim.SlimResult(ok=True, source_path=str(deck), output_path=str(out), original_size=10,
                             slim_size=5, saved_bytes=5, removed_parts=(), deduped_media=0, actions=())
    win._on_slim_done(result)
    assert not win._open_out_btn.isHidden()
    opened = []
    monkeypatch.setattr(owner_actions, "open_folder", lambda w, p: opened.append(p))
    win._open_out_btn.click()
    assert opened == [str(out)]


def test_version_export_offers_folder_button(qtbot, monkeypatch, tmp_path):
    _owner, win = _version_window(qtbot, DOCS)
    monkeypatch.setattr(vw.QMessageBox, "information", lambda *a, **k: None)
    assert win.btn_open_export.isHidden()
    dest = str(tmp_path / "导出.pptx")
    tasks = []

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.done = SimpleNamespace(connect=lambda cb: tasks.append(cb))
            self.finished = SimpleNamespace(connect=lambda cb: None)

        def start(self):
            pass

    monkeypatch.setattr(vw, "BackgroundTask", FakeTask)
    win._run_file_op("version-export", lambda: True, "导出", "正在导出…", "已导出", "导出失败",
                     ok_folder=dest)
    tasks[-1](True)
    assert not win.btn_open_export.isHidden()
    opened = []
    monkeypatch.setattr(owner_actions, "open_folder", lambda w, p: opened.append(p))
    win.btn_open_export.click()
    assert opened == [dest]
