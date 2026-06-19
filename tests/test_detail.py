"""07 详情面板：版本时间线 + 大纲 + 元信息。"""
from __future__ import annotations

import fixtures_gen as fx
from test_ui import StubRender, _index

from pptx_finder import db, indexer
from pptx_finder.models import FileResult
from pptx_finder.ui import theme
from pptx_finder.ui.detail_panel import DetailPanel
from pptx_finder.ui.main_window import MainWindow


def _fr(path="C:/a.pptx", page_count=7, size=2 << 20):
    return FileResult(file_id=1, path=path, name="a.pptx", ext=".pptx", mtime=0,
                      size=size, page_count=page_count, status="ok", score=1, name_hit=False)


class StubVerMgr:
    def __init__(self, versions=None, managed=True):
        self._v = versions or []
        self._m = managed

    def list_versions(self, path):
        return self._v

    def is_managed(self, path):
        return self._m

    def restore_to(self, p, v, dest=None):
        return True

    def export(self, p, v, d):
        return True


def test_page_titles(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "x.pptx", [{"body": "第一页标题\n内容"}, {"body": "第二页\n更多"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    titles = db.page_titles(conn, fid)
    assert len(titles) == 2
    assert titles[0][0] == 1


def test_detail_meta_shows_pages(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    dp.update_for(_fr(page_count=12), versions=[])
    assert "12" in dp._meta_label.text()


def test_detail_version_nodes(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    versions = [{"version_id": "v3", "ts": 3000, "page_count": 24},
                {"version_id": "v2", "ts": 2000, "page_count": 22}]
    dp.update_for(_fr(), versions)
    assert len(dp._version_nodes) == 2


def test_detail_no_version_no_nodes(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    dp.update_for(_fr(), versions=[])
    assert len(dp._version_nodes) == 0
    # 新文案：无版本提示「改存即留」，不再提「在设置里加目录」
    assert "无需任何设置" in dp._version_box.itemAt(0).widget().text()


def test_detail_outline_click_emits_page(qtbot):
    dp = DetailPanel(theme.tok("raycast"))
    qtbot.addWidget(dp)
    fired = []
    dp.page_requested.connect(lambda p: fired.append(p))
    dp.set_outline([(1, "封面"), (2, "目录"), (3, "正文")])
    dp._outline_box.itemAt(1).widget().click()
    assert fired == [2]


def test_mainwindow_detail_toggle(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win.detail_panel.isHidden()
    win._toggle_detail()
    assert not win.detail_panel.isHidden()


def test_mainwindow_select_updates_detail(qtbot, tmp_path):
    vm = StubVerMgr(versions=[{"version_id": "v1", "ts": 1000, "page_count": 5}], managed=True)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), version_mgr=vm, do_index=False)
    qtbot.addWidget(win)
    win._toggle_detail()
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert len(win.detail_panel._version_nodes) >= 1
