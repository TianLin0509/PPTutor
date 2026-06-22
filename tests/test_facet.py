"""08 facet 筛选：聚合 count + 多维过滤 + 抽屉。"""
from __future__ import annotations

import datetime

from test_ui import StubRender, _index

from pptx_finder.models import FileResult
from pptx_finder.ui import theme
from pptx_finder.ui.facet_panel import FacetPanel
from pptx_finder.ui.main_window import MainWindow, facet_counts, facet_filter

NOW = datetime.datetime(2026, 6, 19, 12, 0).timestamp()


def _fr(name="a.pptx", mtime=None, pc=5, path=None):
    ext = ".pptx" if name.endswith(".pptx") else ".ppt"
    return FileResult(file_id=1, path=path or f"C:/d/{name}", name=name, ext=ext,
                      mtime=mtime if mtime is not None else NOW, size=1,
                      page_count=pc, status="ok", score=1, name_hit=False)


def test_facet_counts_page():
    c = facet_counts([_fr(pc=5), _fr(pc=20), _fr(pc=40), _fr(pc=8)], NOW)
    page = dict(c["page"])
    assert page["1-10"] == 2
    assert page["10-30"] == 1
    assert page["30+"] == 1


def test_facet_filter_page():
    out = facet_filter([_fr(pc=5), _fr(pc=20), _fr(pc=40)], {"page": {"10-30"}}, NOW)
    assert [r.page_count for r in out] == [20]


def test_facet_filter_multi_dim_is_and():
    rs = [_fr(name="a.pptx", pc=20), _fr(name="b.ppt", pc=20), _fr(name="c.pptx", pc=5)]
    out = facet_filter(rs, {"type": {"pptx"}, "page": {"10-30"}}, NOW)
    assert [r.name for r in out] == ["a.pptx"]


def test_facet_filter_empty_passes_all():
    rs = [_fr(pc=5), _fr(pc=20)]
    assert len(facet_filter(rs, {}, NOW)) == 2


def test_facet_panel_emits_filters(qtbot):
    fp = FacetPanel(theme.tok("raycast"))
    qtbot.addWidget(fp)
    fp.update_counts({"time": [("今天", 3)], "type": [("pptx", 3)],
                      "page": [("1-10", 3)], "folder": [("d", 3)]})
    fired = []
    fp.filters_changed.connect(lambda f: fired.append(f))
    fp._chip_btns[("page", "1-10")].click()
    assert fired
    assert "1-10" in fired[-1].get("page", set())


def test_facet_panel_caps_large_folder_bucket_count(qtbot):
    fp = FacetPanel(theme.tok("raycast"))
    qtbot.addWidget(fp)
    folders = [(f"folder-{i:02d}", 1) for i in range(60)]

    fp.update_counts({"folder": folders})

    folder_chips = [key for key in fp._chip_btns if key[0] == "folder"]
    assert len(folder_chips) <= 16


def test_mainwindow_facet_toggle(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win.facet_panel.isHidden()
    win._toggle_facet()
    assert not win.facet_panel.isHidden()


def test_mainwindow_facet_filters_results(qtbot, tmp_path):
    import fixtures_gen as fx
    from pptx_finder import db, indexer
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "small.pptx", [{"body": "算力 一页"}])
    fx.make_pptx(docs / "big.pptx", [{"body": f"算力 第{i}页"} for i in range(15)])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("算力")
    win._do_search()
    assert len(win._results) == 2
    # 只看 10-30 页 → 只剩 big.pptx
    win._apply_facet({"page": {"10-30"}})
    assert [r.name for r in win._results] == ["big.pptx"]
