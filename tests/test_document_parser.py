"""多格式解析器注册表：parse_document 按扩展名分发，pptx 收编、未知类型不抛异常。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder import document_parser


def test_parse_document_dispatches_pptx(tmp_path):
    deck = tmp_path / "d.pptx"
    fx.make_pptx(deck, [{"body": "赋能闭环抓手"}])

    res = document_parser.parse_document(str(deck))

    assert res.status == "ok"
    assert res.page_count == 1
    assert "赋能闭环抓手" in res.pages[0].raw_text


def test_parse_document_unsupported_ext_returns_status_not_raises(tmp_path):
    f = tmp_path / "x.xyz"
    f.write_text("hello", encoding="utf-8")

    res = document_parser.parse_document(str(f))

    assert res.status == "unsupported"
    assert res.pages == []
    assert res.page_count == 0


def test_supported_parse_exts_includes_pptx():
    assert ".pptx" in document_parser.supported_parse_exts()


# ---------- txt ----------

def test_parse_document_txt_utf8(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("第一行 赋能\n第二行 抓手", encoding="utf-8")

    res = document_parser.parse_document(str(f))

    assert res.status == "ok"
    assert res.page_count >= 1
    full = "\n".join(p.raw_text for p in res.pages)
    assert "赋能" in full and "抓手" in full


def test_parse_document_txt_gbk_encoding(tmp_path):
    f = tmp_path / "gbk.txt"
    f.write_bytes("中文内容 GBK 编码".encode("gbk"))

    res = document_parser.parse_document(str(f))

    assert res.status == "ok"
    assert "中文内容" in "\n".join(p.raw_text for p in res.pages)


def test_parse_document_txt_chunks_large_file_in_order(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"line{i}" for i in range(130)), encoding="utf-8")

    res = document_parser.parse_document(str(f))

    assert res.page_count >= 2  # 130 行被切成多个行块单元
    assert "line0" in res.pages[0].raw_text
    assert "line129" in res.pages[-1].raw_text


def test_parse_document_txt_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n  \n", encoding="utf-8")

    res = document_parser.parse_document(str(f))

    assert res.status == "ok"
    assert res.page_count == 0


# ---------- docx ----------

def test_parse_document_docx_paragraphs(tmp_path):
    f = tmp_path / "doc.docx"
    fx.make_docx(f, ["第一段 赋能闭环", "第二段 抓手落地"])

    res = document_parser.parse_document(str(f))

    assert res.status == "ok"
    assert res.page_count == 2
    assert "赋能闭环" in res.pages[0].raw_text
    assert "抓手落地" in res.pages[1].raw_text


def test_parse_document_docx_concatenates_runs_in_paragraph(tmp_path):
    f = tmp_path / "runs.docx"
    fx.make_docx(f, [["小明", "硕士"]])  # 一段两 run，模拟 Word 把一句拆成多段 w:t

    res = document_parser.parse_document(str(f))

    assert "小明硕士" in res.pages[0].raw_text  # 段内无缝拼接，不在词中插断


def test_parse_document_docx_bad_zip_returns_error(tmp_path):
    f = tmp_path / "bad.docx"
    f.write_bytes(b"not a zip at all")

    res = document_parser.parse_document(str(f))

    assert res.status == "error"


# ---------- xlsx ----------

def test_parse_document_xlsx_cells_grouped_by_sheet(tmp_path):
    f = tmp_path / "book.xlsx"
    fx.make_xlsx(f, [["营收 同比", "利润"], ["现金流 充裕"]])  # 2 张表

    res = document_parser.parse_document(str(f))

    assert res.status == "ok"
    assert res.page_count == 2  # 2 张表 = 2 个单元
    s = "\n".join(p.raw_text for p in res.pages)
    assert "营收" in s and "利润" in s and "现金流" in s


def test_parse_document_xlsx_bad_zip_returns_error(tmp_path):
    f = tmp_path / "bad.xlsx"
    f.write_bytes(b"nope")

    res = document_parser.parse_document(str(f))

    assert res.status == "error"


# ---------- pdf ----------

def test_parse_document_pdf_pages(tmp_path):
    f = tmp_path / "doc.pdf"
    fx.make_pdf(f, ["FirstPageRevenue alpha", "SecondPageProfit bravo"])

    res = document_parser.parse_document(str(f))

    assert res.status == "ok"
    assert res.page_count == 2
    assert "FirstPageRevenue" in res.pages[0].raw_text
    assert "SecondPageProfit" in res.pages[1].raw_text


def test_parse_document_pdf_scanned_no_text_flagged(tmp_path):
    f = tmp_path / "scanned.pdf"
    fx.make_pdf(f, [""])  # 有页但无可抽取文本 → 模拟扫描版（图片型）

    res = document_parser.parse_document(str(f))

    assert res.status == "scanned"
    assert res.page_count == 0


def test_parse_document_pdf_corrupt_returns_error(tmp_path):
    f = tmp_path / "bad.pdf"
    f.write_bytes(b"this is not a pdf at all")

    res = document_parser.parse_document(str(f))

    assert res.status == "error"
