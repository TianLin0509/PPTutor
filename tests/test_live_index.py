"""实时索引：watcher 留版事件 → 单文件并入搜索（无需重扫）+ 启动跳过全盘扫。"""
from __future__ import annotations

import fixtures_gen as fx
from test_ui import StubRender, _index

from pptx_finder import db, indexer, search
from pptx_finder.ui.live_indexer import LiveIndexer
from pptx_finder.ui.main_window import MainWindow


def test_index_single_adds_without_deleting(tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "old.pptx", [{"body": "旧文件保留"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    fx.make_pptx(docs / "new.pptx", [{"body": "新内容关键词XYZ"}])
    assert indexer.index_single(conn, str(docs / "new.pptx"))
    assert search.search(conn, "旧文件保留")       # 旧记录没被删
    assert search.search(conn, "新内容关键词XYZ")    # 新文件并入


def test_index_single_missing_file(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    assert indexer.index_single(conn, str(tmp_path / "nope.pptx")) is False


def test_live_index_via_snapshot(qtbot, tmp_path):
    """on_version_snapshot（watcher 事件）应把新建文件实时并入搜索索引。"""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    newp = tmp_path / "实时测试LT.pptx"
    fx.make_pptx(newp, [{"body": "实时索引验证内容"}])
    assert not search.search(win._conn, "实时测试LT")   # 索引前搜不到
    win.on_version_snapshot(str(newp), "v1")            # 模拟 watcher 留版事件
    res = search.search(win._conn, "实时测试LT")
    assert any("实时测试LT" in r.name for r in res)      # 实时进索引后可搜


def test_startup_skips_scan_when_indexed(qtbot, tmp_path):
    """已有索引时 _index_is_empty False（启动不再全盘扫）。"""
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win._index_is_empty() is False


def test_index_is_empty_on_blank_db(qtbot, tmp_path):
    conn = db.connect(tmp_path / "blank.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert win._index_is_empty() is True


def test_live_indexer_async_off_main_thread(qtbot, tmp_path):
    """LiveIndexer：submit 入队 → 后台线程索引 → indexed 信号 → 文件可搜。

    这是 UI 冻结修复的核心——实时索引在后台串行线程跑，主线程绝不 parse/写库。
    """
    docs = tmp_path / "d"
    docs.mkdir()
    dbp = tmp_path / "i.db"
    conn = db.connect(dbp)
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)  # 空基线
    fx.make_pptx(docs / "异步LT.pptx", [{"body": "异步索引内容ABC"}])

    li = LiveIndexer(str(dbp))
    li.start()
    try:
        with qtbot.waitSignal(li.indexed, timeout=5000):
            li.submit(str(docs / "异步LT.pptx"))  # 主线程仅入队
    finally:
        li.stop()
        li.wait(3000)

    conn2 = db.connect(dbp)  # 新连接读后台已提交的数据
    assert any("异步LT" in r.name for r in search.search(conn2, "异步索引内容ABC"))
