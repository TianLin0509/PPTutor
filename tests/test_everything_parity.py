# -*- coding: utf-8 -*-
"""与 Everything 的能力对齐：排序、结果显示、查询语法在真实链路上的表现。

目标是「不比 Everything 差」。这一组用例逐条钉住那些原来缺、现在补上的能力，
同时守住硬约束：PPT 相关功能一个字节都不受影响。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel

import fixtures_gen as fx

from pptx_finder import db, namequery, namestore, search
from pptx_finder.models import FileResult
from pptx_finder.ui import theme
from pptx_finder.ui.main_window import ALL_FILES_SCOPE_LABEL, MainWindow, ResultItem
from pptx_finder.ui.result_utils import SORT_KEYS, sort_results

BASE_T = 1_700_000_000


class _StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, req_id, path, page_no, cache_key=None):
        self.rendered.emit(req_id, "")


def _res(name, *, size=100, mtime=BASE_T, path=None, is_dir=False, fid=-1):
    return FileResult(
        file_id=fid, path=path or rf"C:\work\{name}", name=name,
        ext="" if is_dir else ".txt", mtime=mtime, size=size, page_count=0,
        status="filename_only", score=1.0, name_hit=True, hits=[], is_dir=is_dir)


# ---------------------------------------------------------------- 排序

def test_sort_by_size_and_path_exist():
    assert "size" in SORT_KEYS and "path" in SORT_KEYS


def test_sort_by_size_is_biggest_first():
    """按大小找出占地方的大文件，是这类工具最常见的用法之一。"""
    items = [_res("small.txt", size=10, fid=-1),
             _res("huge.txt", size=9_000_000, fid=-2),
             _res("mid.txt", size=5_000, fid=-3)]
    got = [r.name for r in sort_results(items, "size")]
    assert got == ["huge.txt", "mid.txt", "small.txt"]


def test_sort_by_path_is_alphabetical():
    items = [_res("a.txt", path=r"C:\zeta\a.txt", fid=-1),
             _res("b.txt", path=r"C:\alpha\b.txt", fid=-2)]
    got = [r.path for r in sort_results(items, "path")]
    assert got == [r"C:\alpha\b.txt", r"C:\zeta\a.txt"]


def test_descending_reverses_the_whole_order():
    """反向就是把当前顺序整个倒过来。逐键取反在多键排序下会得出谁也预料不到的顺序。"""
    items = [_res("small.txt", size=10, fid=-1),
             _res("huge.txt", size=9_000_000, fid=-2),
             _res("mid.txt", size=5_000, fid=-3)]
    up = [r.name for r in sort_results(items, "size", descending=True)]
    assert up == ["small.txt", "mid.txt", "huge.txt"]


def test_unknown_sort_key_falls_back_to_relevance():
    items = [_res("a.txt", fid=-1)]
    assert sort_results(items, "nonsense") == items


def test_sort_ui_offers_the_new_keys(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=_StubRender(), do_index=False)
    qtbot.addWidget(win)
    labels = [win.sort_combo.itemText(i) for i in range(win.sort_combo.count())]
    assert labels == ["相关度", "最近修改", "文件名", "大小", "路径"]
    win.sort_combo.setCurrentText("大小")
    assert win._sort_key() == "size"
    assert win.sort_desc_btn.isChecked() is False
    win.sort_desc_btn.setChecked(True)
    assert win.sort_desc_btn.text() == "↑"
    conn.close()


# ---------------------------------------------------------------- 结果显示

def test_card_shows_size_and_location(qtbot):
    """Everything 的结果表里大小和位置一直都在。只给文件名和时间的话，
    「找到了但不知道在哪、多大」——同名文件散落在十几个目录是常态。"""
    r = _res("report.txt", size=2_500_000, path=r"C:\work\2026\report.txt")
    item = ResultItem(r, theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(item)
    texts = " | ".join(lb.text() for lb in item.findChildren(QLabel))
    assert "2.4 MB" in texts
    assert "2026" in texts          # 所在目录


def test_card_omits_size_for_folders(qtbot):
    r = _res("bin", is_dir=True, size=0, path=r"C:\work\bin")
    item = ResultItem(r, theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(item)
    texts = [lb.text() for lb in item.findChildren(QLabel)]
    assert "文件夹" in texts
    assert not any("B" == t.strip() for t in texts)


# ---------------------------------------------------------------- 查询语法端到端

def _store(entries):
    b = namestore.NameStoreBuilder()
    for path, size, mtime in entries:
        b.add(path, size, mtime)
    return namestore.NameStore(b.write())


def test_everything_syntax_end_to_end(tmp_path, monkeypatch):
    """通配符 / ext: / size: / dm: / | / ! 走完整的 search_names 链路。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    files = {
        "report.pdf": (2_000_000, BASE_T),
        "report-draft.pdf": (500, BASE_T),
        "notes.txt": (100, BASE_T),
        "photo.png": (5_000_000, BASE_T),
    }
    entries = []
    for name, (size, mtime) in files.items():
        f = tmp_path / name
        f.write_text("x", encoding="utf-8")
        entries.append((str(f), size, mtime))

    with _store(entries) as store:
        def names(q):
            return sorted(r.name for r in search.search_names(store, q, limit=50))

        assert names("*.pdf") == ["report-draft.pdf", "report.pdf"]
        assert names("ext:pdf") == ["report-draft.pdf", "report.pdf"]
        assert names("report !draft") == ["report.pdf"]
        assert names("notes|photo") == ["notes.txt", "photo.png"]
        assert names("size:>1mb") == ["photo.png", "report.pdf"]
        assert names("ext:pdf size:>1mb") == ["report.pdf"]
        assert names("report*") == ["report-draft.pdf", "report.pdf"]


def test_bad_syntax_is_zero_results_not_a_crash(tmp_path, monkeypatch):
    """语法写错时给零结果，绝不能把搜索线程打崩。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with _store([(str(f), 1, BASE_T)]) as store:
        assert search.search_names(store, "regex:[unclosed") == []
        assert search.search_names(store, "size:banana") == []
        assert search.search_names(store, "<unbalanced") == []


# ---------------------------------------------------------------- 硬约束

def test_ppt_content_search_syntax_is_untouched(tmp_path):
    """PPT 内容搜索仍用 parse_query 那套：`*` 在那里不是通配符，是分隔符。

    两套语法必须各管各的——把 Everything 语法混进内容搜索会当场改变 PPT 的搜索行为。
    """
    from pptx_finder.text_tokenize import parse_query

    terms, phrases = parse_query('产品 "路线图" a*b')
    assert "产品" in terms and phrases == ["路线图"]

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(
        conn, path=str(tmp_path / "deck.pptx"), name="deck.pptx", ext=".pptx",
        size=1, mtime=BASE_T, content_hash="stat:1", page_count=1,
        status="ok", error="", indexed_at=BASE_T)
    db.replace_pages(conn, fid, [(1, "产品路线图", "产 品 路 线 图")])
    conn.commit()
    hits = search.search(conn, "路线图")
    conn.close()
    assert [r.name for r in hits] == ["deck.pptx"]
    assert hits[0].file_id > 0 and hits[0].is_dir is False


def test_known_gaps_are_declared_not_hidden():
    """没做的 Everything 功能逐条写在 KNOWN_GAPS 里，不假装支持。"""
    assert namequery.KNOWN_GAPS
    assert all(isinstance(g, str) and g for g in namequery.KNOWN_GAPS)
