"""parser 单元测试：页序还原 / 备注 / SmartArt / 加密 / 损坏。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder import parser as parser_mod
from pptx_finder.parser import parse_pptx, parse_pptx_page


def test_basic_parse_and_notes(tmp_path):
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [
        {"body": "ALPHA 算力方案"},
        {"body": "BETA 昇腾", "notes": "备注里的关键词 NOTEWORD"},
        {"body": "GAMMA"},
    ])
    deck = parse_pptx(str(p))
    assert deck.status == "ok"
    assert deck.page_count == 3
    assert "ALPHA" in deck.pages[0].body
    assert "算力方案" in deck.pages[0].body
    # 备注被抓到，且归属第 2 页
    assert "NOTEWORD" in deck.pages[1].notes
    assert "NOTEWORD" in deck.pages[1].raw_text


def test_parse_single_page_does_not_parse_the_rest_of_the_deck(tmp_path, monkeypatch):
    p = tmp_path / "single-page-read.pptx"
    fx.make_pptx(p, [
        {"body": "第一页"},
        {"body": "只读取这一页"},
        {"body": "第三页"},
    ])
    parsed_pages: list[int] = []
    original = parser_mod._parse_slide

    def tracked(zf, slide_part, page_no):
        parsed_pages.append(page_no)
        return original(zf, slide_part, page_no)

    monkeypatch.setattr(parser_mod, "_parse_slide", tracked)

    page = parse_pptx_page(str(p), 2)

    assert page is not None
    assert page.page_no == 2
    assert page.body == "只读取这一页"
    assert parsed_pages == [2]


def test_page_order_follows_sldidlst(tmp_path):
    """反转 sldIdLst 后，第 1 页应变成原最后一页 —— 证明按放映序而非文件名。"""
    p = tmp_path / "ordered.pptx"
    fx.make_pptx(p, [{"body": "ALPHA"}, {"body": "BETA"}, {"body": "GAMMA"}])
    before = parse_pptx(str(p))
    assert "ALPHA" in before.pages[0].body
    assert "GAMMA" in before.pages[2].body

    fx.reverse_slide_order(p)
    after = parse_pptx(str(p))
    assert "GAMMA" in after.pages[0].body, "应按 sldIdLst 顺序：反转后首页是 GAMMA"
    assert "ALPHA" in after.pages[2].body


def test_smartart_extracted(tmp_path):
    p = tmp_path / "smart.pptx"
    fx.make_pptx(p, [{"body": "封面"}, {"body": "正文"}])
    fx.inject_smartart(p, 1, "SMARTART_架构图_关键词")
    deck = parse_pptx(str(p))
    assert deck.status == "ok"
    assert "SMARTART_架构图_关键词" in deck.pages[0].smartart
    assert "SMARTART_架构图_关键词" in deck.pages[0].raw_text


def test_encrypted_detected(tmp_path):
    p = tmp_path / "enc.pptx"
    fx.write_encrypted_stub(p)
    deck = parse_pptx(str(p))
    assert deck.status == "encrypted"


def test_corrupt_detected(tmp_path):
    p = tmp_path / "bad.pptx"
    fx.write_corrupt_stub(p)
    deck = parse_pptx(str(p))
    assert deck.status == "error"
