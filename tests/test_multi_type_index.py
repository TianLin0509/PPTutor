"""多类型内容索引：update_index / index_single 认 pptx+docx+xlsx+txt+pdf。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder import db, indexer, search


def _make_one_of_each(tmp_path):
    pptx = tmp_path / "deck.pptx"
    fx.make_pptx(pptx, [{"body": "PPT 赋能闭环"}])
    docx = tmp_path / "doc.docx"
    fx.make_docx(docx, ["Word 抓手落地"])
    xlsx = tmp_path / "book.xlsx"
    fx.make_xlsx(xlsx, [["Excel 营收同比"]])
    txt = tmp_path / "note.txt"
    txt.write_text("TXT 现金流充裕", encoding="utf-8")
    pdf = tmp_path / "doc.pdf"
    fx.make_pdf(pdf, ["PdfRevenueAlpha content"])
    return [pptx, docx, xlsx, txt, pdf]


def test_update_index_indexes_all_content_types(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    files = _make_one_of_each(tmp_path)

    indexer.update_index(conn, [], scan_iter=iter(files), workers=1)

    rows = {
        r["ext"]: r
        for r in conn.execute("SELECT ext, status, page_count FROM files").fetchall()
    }
    for ext in (".pptx", ".docx", ".xlsx", ".txt", ".pdf"):
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
