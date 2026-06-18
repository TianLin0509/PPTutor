"""UI 集成测试：搜索→结果→选中→预览请求命中页（注入 stub 渲染器，免 COM）。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal

import fixtures_gen as fx

from pptx_finder import db, indexer
from pptx_finder.ui.main_window import MainWindow


def _index_multi(tmp_path, files: dict[str, list[str]]):
    docs = tmp_path / "d"
    docs.mkdir()
    for fn, bodies in files.items():
        fx.make_pptx(docs / fn, [{"body": b} for b in bodies])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


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


def test_instant_search_debounce(qtbot, tmp_path):
    """输入触发防抖后自动搜索（不手动调 _do_search）。"""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")  # 仅 setText，靠 textChanged→防抖→自动搜
    qtbot.wait(420)
    assert win.result_list.count() == 1


def test_keyboard_nav(qtbot, tmp_path):
    """search_box 上按 ↑↓ 移动结果选中。"""
    conn = _index_multi(tmp_path, {f"f{i}.pptx": ["共同词 算力 集群"] for i in range(3)})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("算力 集群")
    win._do_search()
    assert win.result_list.count() == 3
    win.result_list.setCurrentRow(0)
    qtbot.keyClick(win.search_box, Qt.Key_Down)
    assert win.result_list.currentRow() == 1
    qtbot.keyClick(win.search_box, Qt.Key_Down)
    assert win.result_list.currentRow() == 2
    qtbot.keyClick(win.search_box, Qt.Key_Up)
    assert win.result_list.currentRow() == 1


def test_thumbnail_strip(qtbot, tmp_path):
    """多命中页生成对应数量的缩略图按钮，可切换命中页。"""
    conn = _index_multi(tmp_path, {"multi.pptx": ["算力 第一页", "算力 第二页", "算力 第三页"]})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("算力")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert len(win._thumb_btns) == 3       # 命中 3 页 → 3 个缩略图按钮
    win._goto_hit(2)
    assert win._hit_idx == 2


def test_theme_toggle(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    t0 = win._theme
    win._toggle_theme()
    assert win._theme != t0
    assert win._theme in ("cloud", "raycast")
