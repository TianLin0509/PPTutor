"""P0-1 版本存在感：留版回调 → 跨线程桥 VersionBridge → UI 盾牌 + 仅首次告知。"""
from __future__ import annotations

import fixtures_gen as fx
from test_ui import StubRender, _index

from pptx_finder import config
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.version_bridge import VersionBridge
from pptx_finder.versioning.manager import VersionManager


# ---------- manager 回调 ----------
def test_snapshot_fires_callback(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "算力 集群"}])
    seen = []
    mgr = VersionManager(on_snapshot=lambda path, vid: seen.append((path, vid)))
    vid = mgr.snapshot_now(str(p))
    assert seen == [(str(p), vid)]              # 留版成功 → 回调
    seen.clear()
    assert mgr.snapshot_now(str(p)) is None     # 内容没变
    assert seen == []                            # 不回调


def test_restore_keepbottom_does_not_notify(tmp_path):
    """恢复前的自动留底 notify=False，不触发用户可见的「新版本」通知。"""
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "OLD"}])
    seen = []
    mgr = VersionManager(on_snapshot=lambda *a: seen.append(a))
    mgr.snapshot_now(str(p))
    v1 = mgr.list_versions(str(p))[0]["version_id"]
    fx.make_pptx(p, [{"body": "NEW"}])
    mgr.snapshot_now(str(p))
    seen.clear()
    mgr.restore_to(str(p), v1)                   # 恢复前留底应静默
    assert seen == []


# ---------- 跨线程桥 ----------
def test_bridge_emits_signal(qtbot):
    bridge = VersionBridge()
    got = []
    bridge.snapshotted.connect(lambda path, vid: got.append((path, vid)))
    with qtbot.waitSignal(bridge.snapshotted, timeout=500):
        bridge.emit_snapshot("C:/x.pptx", "v1")
    assert got == [("C:/x.pptx", "v1")]


# ---------- 主窗盾牌 + 首次告知 ----------
class _StubVer:
    def __init__(self, docs=0, versions=None):
        self._docs = docs
        self._v = versions or []

    def list_docs(self):
        return list(range(self._docs))

    def list_versions(self, path):
        return self._v

    def is_managed(self, path):
        return True

    def restore_to(self, *a, **k):
        return True

    def export(self, *a, **k):
        return True


def test_shield_shows_count(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=3), do_index=False)
    qtbot.addWidget(win)
    win.refresh_version_shield()
    assert not win.version_shield.isHidden()
    assert "3" in win.version_shield.text()


def test_shield_hidden_when_zero(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=0), do_index=False)
    qtbot.addWidget(win)
    win.refresh_version_shield()
    assert win.version_shield.isHidden()


def test_first_snapshot_intro_once(qtbot, tmp_path):
    (config.data_dir() / "version_intro.flag").unlink(missing_ok=True)  # 清首次标记
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=_StubVer(docs=1), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(30)
    win.on_version_snapshot("C:/x.pptx", "v1")
    assert getattr(win, "_spotlight", None) is not None     # 首次 → 聚光灯
    win._spotlight = None
    win.on_version_snapshot("C:/y.pptx", "v2")
    assert getattr(win, "_spotlight", None) is None         # 之后永久静默


# ---------- P0-2 详情按钮红点 ----------
def test_detail_dot_shows_on_versions(qtbot, tmp_path):
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert not win._detail_dot.isHidden()        # 有版本 + 详情关 → 红点
    win._toggle_detail()                          # 打开详情
    assert win._detail_dot.isHidden()             # 已在看版本 → 红点隐藏


def test_detail_dot_hidden_no_versions(qtbot, tmp_path):
    vm = _StubVer(docs=0, versions=[])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert win._detail_dot.isHidden()             # 无版本 → 无红点


# ---------- P0-3 首次搜索框 coachmark ----------
def test_search_coach_targets_searchbox(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(20)
    win._welcome = None
    win._show_search_coach()
    assert getattr(win, "_spotlight", None) is not None
    assert win._spotlight._target is win.search_box


def test_show_spotlight_replaces_old(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(20)
    win._show_spotlight(win.search_box, "a")
    first = win._spotlight
    win._show_spotlight(win.detail_btn, "b")      # 弹新的应替换旧的
    assert win._spotlight is not first
    assert win._spotlight._target is win.detail_btn


# ---------- P1-1 索引完成庆祝 ----------
def test_index_celebration_once(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(20)
    win._on_index_done({})
    assert win._index_celebrated is True
    assert "PPT" in win._toast_label.text()


# ---------- P1-2 空白起步引导 ----------
def test_start_hint_when_no_recent(qtbot, tmp_path):
    from pptx_finder import db
    conn = db.connect(tmp_path / "empty.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._do_search()                              # 空查询 + 无最近文件 → 起步引导
    assert not win.empty_hint.isHidden()
    assert "整理" in win._empty_query_label.text()


# ---------- P1-3 恢复确认 ----------
def test_restore_requires_confirm(qtbot, tmp_path, monkeypatch):
    calls = []
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    vm.restore_to = lambda *a, **k: (calls.append(a), True)[1]
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(win, "_confirm_restore", lambda: False)
    win._act_restore_version("C:/nonexist.pptx", "v1")   # 取消 → 不恢复
    assert calls == []
    monkeypatch.setattr(win, "_confirm_restore", lambda: True)
    win._act_restore_version("C:/nonexist.pptx", "v1")   # 确认 → 恢复
    assert len(calls) == 1
    assert "✓" in win._toast_label.text()


# ---------- P1-4 详情首开提示 ----------
def test_detail_first_open_hint(qtbot, tmp_path):
    vm = _StubVer(docs=1, versions=[{"version_id": "v1", "ts": 1, "page_count": 3}])
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(),
                     version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    win._toggle_detail()                          # 首次展开 + 有版本 → 提示
    assert "历史版本" in win._toast_label.text()
