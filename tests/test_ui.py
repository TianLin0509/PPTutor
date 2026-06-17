"""UI 集成测试：搜索→结果→选中→预览请求命中页（注入 stub 渲染器，免 COM）。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

import fixtures_gen as fx

from pptx_finder import db, indexer
from pptx_finder.ui.main_window import MainWindow


class StubRender(QObject):
    """假渲染器：记录请求并立即回 ''（触发 UI 的「无法预览」兜底分支）。"""
    rendered = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[int, str, int]] = []

    def request(self, req_id: int, path: str, page_no: int, cache_key=None):
        self.calls.append((req_id, path, page_no))
        self.rendered.emit(req_id, "")


def _index(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "算力方案v2.pptx", [{"body": "第一页封面"}, {"body": "第二页 昇腾 集群部署"}])
    fx.make_pptx(docs / "周报.pptx", [{"body": "本周进展无关词"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


def test_search_select_preview(qtbot, tmp_path):
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    # 内容搜索 → 命中 1 个文件
    win.search_box.setText("昇腾")
    win._do_search()
    assert win.result_list.count() == 1

    # 选中 → 预览请求应指向命中页（第 2 页）
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: len(stub.calls) >= 1, timeout=2000)
    assert stub.calls[-1][2] == 2
    # 渲染回空串 → 显示兜底文案
    assert "无法预览" in win.image_label.text()


def test_filename_mode_and_multi_term(qtbot, tmp_path):
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    # 文件名命中
    win.search_box.setText("算力方案")
    win._do_search()
    assert win.result_list.count() == 1
    assert win._results[0].name_hit is True

    # 多词 AND：两个词需同页
    win.search_box.setText("昇腾 集群")
    win._do_search()
    assert win.result_list.count() == 1

    win.search_box.setText("昇腾 不存在的词xyz")
    win._do_search()
    assert win.result_list.count() == 0


def test_empty_clears(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    assert win.result_list.count() == 1
    win.search_box.setText("")
    win._do_search()
    assert win.result_list.count() == 0
