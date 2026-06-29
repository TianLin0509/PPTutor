"""多类型内容索引：update_index / index_single 认 pptx+docx+pdf（2026-06-29 砍掉 xlsx/txt）。"""
from __future__ import annotations

from pathlib import Path

import fixtures_gen as fx

from pptx_finder import db, indexer, search


def _make_one_of_each(tmp_path):
    pptx = tmp_path / "deck.pptx"
    fx.make_pptx(pptx, [{"body": "PPT 赋能闭环"}])
    docx = tmp_path / "doc.docx"
    fx.make_docx(docx, ["Word 抓手落地"])
    pdf = tmp_path / "doc.pdf"
    fx.make_pdf(pdf, ["PdfRevenueAlpha content"])
    return [pptx, docx, pdf]


def test_update_index_indexes_all_content_types(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    files = _make_one_of_each(tmp_path)

    indexer.update_index(conn, [], scan_iter=iter(files), workers=1)

    rows = {
        r["ext"]: r
        for r in conn.execute("SELECT ext, status, page_count FROM files").fetchall()
    }
    for ext in (".pptx", ".docx", ".pdf"):
        assert ext in rows, f"{ext} 未进索引"
        assert rows[ext]["status"] == "ok", f"{ext} 状态={rows[ext]['status']}"
        assert rows[ext]["page_count"] >= 1, f"{ext} 没解析出内容"


def test_index_single_handles_docx(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    docx = tmp_path / "d.docx"
    fx.make_docx(docx, ["实时索引 文档内容"])

    assert indexer.index_single(conn, str(docx)) is True

    row = conn.execute(
        "SELECT ext, status, page_count FROM files WHERE path=?", (str(docx),)
    ).fetchone()
    assert row["ext"] == ".docx"
    assert row["status"] == "ok"
    assert row["page_count"] >= 1


def test_index_single_rejects_dropped_types(tmp_path):
    """砍掉的 xlsx/txt 不再被实时索引（index_single 返回 False）。"""
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    xlsx = tmp_path / "book.xlsx"
    fx.make_xlsx(xlsx, [["不该被索引"]])
    txt = tmp_path / "note.txt"
    txt.write_text("不该被索引", encoding="utf-8")

    assert indexer.index_single(conn, str(xlsx)) is False
    assert indexer.index_single(conn, str(txt)) is False
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_search_filters_by_ext(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    pptx = tmp_path / "deck.pptx"
    fx.make_pptx(pptx, [{"body": "赋能闭环 关键词"}])
    docx = tmp_path / "doc.docx"
    fx.make_docx(docx, ["赋能闭环 关键词"])
    indexer.update_index(conn, [], scan_iter=iter([pptx, docx]), workers=1)

    # 不过滤 → pptx 和 docx 都命中
    all_exts = {r.ext for r in search.search(conn, "关键词")}
    assert ".pptx" in all_exts and ".docx" in all_exts

    # 只要 pptx
    only_ppt = search.search(conn, "关键词", exts=(".pptx",))
    assert only_ppt
    assert all(r.ext == ".pptx" for r in only_ppt)


def test_update_index_defers_non_ppt_by_type_order(tmp_path, monkeypatch):
    files = []
    for name in ("late.pdf", "doc.docx", "deck.pptx"):
        p = tmp_path / name
        p.write_bytes(b"placeholder")
        files.append(p)

    calls = []

    def fake_index_one(path: str):
        p = Path(path)
        st = p.stat()
        calls.append(p.suffix.lower())
        return {
            "path": str(p),
            "name": p.name,
            "ext": p.suffix.lower(),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "content_hash": f"test:{p.name}",
            "status": "ok",
            "error": "",
            "page_count": 0,
            "pages": [],
        }

    monkeypatch.setattr(indexer, "_index_one", fake_index_one)
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    indexer.update_index(conn, [], scan_iter=iter(files), workers=1)

    # PPT 优先：pptx 先建，再按 DEFERRED 顺序（docx → pdf）补建
    assert calls == [".pptx", ".docx", ".pdf"]


def test_update_index_progress_uses_final_total_for_deferred_docs(tmp_path, monkeypatch):
    files = []
    for name in ("doc.docx", "deck.pptx", "doc.pdf"):
        p = tmp_path / name
        p.write_bytes(b"placeholder")
        files.append(p)

    def fake_index_one(path: str):
        p = Path(path)
        st = p.stat()
        return {
            "path": str(p),
            "name": p.name,
            "ext": p.suffix.lower(),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "content_hash": f"test:{p.name}",
            "status": "ok",
            "error": "",
            "page_count": 0,
            "pages": [],
        }

    progress = []
    monkeypatch.setattr(indexer, "_index_one", fake_index_one)
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    indexer.update_index(
        conn,
        [],
        scan_iter=iter(files),
        workers=1,
        progress_cb=lambda done, total, cur: progress.append((done, total, cur)),
    )

    deferred_progress = [
        (done, total, Path(cur).suffix.lower())
        for done, total, cur in progress
        if Path(cur).suffix.lower() in {".docx", ".pdf"}
    ]
    # 1 pptx + 2 deferred(docx,pdf) = total 3；扫描结束后走真实百分比
    assert deferred_progress == [
        (2, 3, ".docx"),
        (3, 3, ".pdf"),
    ]


def test_type_counts_built_vs_total_by_ext(tmp_path):
    """db.type_counts：按扩展名返回 (已建, 总数)；已建 = status 非 pending。供底部分类型进度用。"""
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    def _f(name, ext, status):
        db.upsert_file(conn, path=str(tmp_path / name), name=name, ext=ext, size=1, mtime=1.0,
                       content_hash="h", page_count=1, status=status, error="", indexed_at=1.0)

    _f("a.pptx", ".pptx", "ok")
    _f("b.pptx", ".pptx", "ok")
    _f("c.pptx", ".pptx", "pending")
    _f("d.docx", ".docx", "pending")
    conn.commit()

    tc = db.type_counts(conn)
    assert tc[".pptx"] == (2, 3)   # 2 已建内容 / 共 3 个 pptx
    assert tc[".docx"] == (0, 1)   # 已登记、内容待补建
    assert ".pdf" not in tc        # 无此类文件


def test_update_index_skips_hung_file_via_timeout(tmp_path, monkeypatch):
    """核心保护：单文件解析卡住 → 超时跳过、不阻塞整批、update_index 正常返回（不永久卡死）。"""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    real = indexer._index_one

    def slow(path: str):
        if "hang" in Path(path).name:
            release.wait(20)   # 模拟卡死的解析（测试结束统一放行）
        return real(path)

    # 用线程池以便进程内 monkeypatch 慢解析；极短超时触发跳过保护
    monkeypatch.setattr(indexer, "_make_executor", lambda n: ThreadPoolExecutor(max_workers=2))
    monkeypatch.setattr(indexer, "PARSE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(indexer, "_index_one", slow)

    fx.make_pptx(tmp_path / "good.pptx", [{"body": "昇腾算力"}])
    fx.make_pptx(tmp_path / "hang.pptx", [{"body": "卡住的文件"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    try:
        t0 = time.monotonic()
        indexer.update_index(
            conn, [], scan_iter=iter([tmp_path / "good.pptx", tmp_path / "hang.pptx"]), workers=4,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 15, f"卡死保护失效，耗时 {elapsed:.1f}s"
        rows = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM files")}
        assert rows.get("good.pptx") == "ok"       # 正常文件照常索引
        assert rows.get("hang.pptx") == "timeout"  # 卡住文件被超时跳过
    finally:
        release.set()
