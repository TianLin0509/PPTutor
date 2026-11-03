import copy
import json
import zipfile

import pytest
from PIL import Image, ImageDraw, ImageFont

from pptx_finder import imgtext_assist, imgtext_ocr
from pptx_finder.imgtext_assist_render import font_path
from pptx_finder.imgtext_ops import apply_ops
from pptx_finder.hub_library.library import PptutorError


@pytest.fixture
def candidate_seed():
    return {"img_w": 800, "img_h": 450, "font": "Microsoft YaHei", "elements": [
        {"id": "s1", "type": "text", "bbox": [20, 20, 250, 60], "text": "无线算法", "color": "#202020", "bold": False},
        {"id": "s2", "type": "text", "bbox": [20, 80, 250, 120], "text": "低置信文字", "color": "#202020", "bold": False,
         "candidateStatus": "needs_confirmation", "reviewReasons": ["低置信 OCR"]}]}


@pytest.mark.parametrize("ops", [{"d": [99]}, {"x": ["s3"]}, {"attr": {"1": {"size": 50}}},
    {"bbox": {"1": [-1, 0, 50, 50]}}, {"merge": [[1, 2], [1, 2]]}, {"merge": [[1, 1]]},
    {"c": [[1, 1, 30, 30, 4, "#FF0000"]]}, {"p": {"s1": "missing"}},
    {"d": [1], "fix": {"1": "冲突"}}, {"t": [[0, 0, 20, 20, ""]]},
    {"whatever": []}, {"bbox": {"1": [0, 0, float("nan"), 50]}}])
def test_invalid_ops_rejected(candidate_seed, ops):
    with pytest.raises(PptutorError) as caught:
        apply_ops(candidate_seed, ops)
    assert caught.value.code == "INVALID_OPS"


def test_deltas_keep_seed_and_apply_corrections(candidate_seed):
    saved = copy.deepcopy(candidate_seed)
    value = apply_ops(candidate_seed, {"fix": {"2": "已确认文字"}, "b": [1],
        "g": [[300, 100, 500, 300, "流程图"]], "t": [[20, 150, 230, 190, "漏检新增文字"]]})
    assert candidate_seed == saved
    assert value["elements"][1]["text"] == "已确认文字"
    assert value["elements"][1]["candidateStatus"] == "confirmed"
    assert value["elements"][0]["bold"] is True
    assert len(value["elements"]) == 4


def test_locked_text_inventory(candidate_seed):
    value = apply_ops(candidate_seed, {"x": [1], "d": [2]})
    assert len(value["lockedTexts"]) == 2
    assert value["elements"][0]["type"] == "text_in_graphic"


def test_seed_white_text_on_red_is_not_red(tmp_path, monkeypatch, qapp):
    source = tmp_path / "red.png"
    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path(bold=True)[0]), 36)
    box = draw.textbbox((100, 60), "模态演进", font=font)
    draw.rectangle((90, box[1] - 3, box[2] + 10, box[3] + 3), fill="#B50605")
    draw.text((100, 60), "模态演进", font=font, fill="white")
    image.save(source)
    monkeypatch.setattr(imgtext_ocr, "recognize_one", lambda _: [
        {"text": "模态演进", "box": list(draw.textbbox((100, 60), "模态演进", font=font)), "score": 0.99}])
    imgtext_assist.seed(source, tmp_path / "work")
    value = json.loads((tmp_path / "work/elements.seed.json").read_text("utf-8"))
    colour = value["elements"][0]["color"]
    assert all(int(colour[i:i+2], 16) > 220 for i in (1, 3, 5)), colour


@pytest.fixture
def seeded(tmp_path, monkeypatch, qapp):
    source = tmp_path / "original.png"
    image = Image.new("RGB", (800, 450), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path()[0]), 32)
    draw.text((20, 20), "无线算法", fill="black", font=font)
    draw.text((20, 80), "低置信文字", fill="black", font=font)
    image.save(source)
    rows = [{"text": "无线算法", "box": [20, 25, 160, 65], "score": 0.99},
            {"text": "低置信文字", "box": [20, 85, 200, 125], "score": 0.2},
            {"text": "竖排", "box": [500, 100, 515, 200], "score": 0.8}]
    monkeypatch.setattr(imgtext_ocr, "recognize_one", lambda _: rows)
    work = tmp_path / "work"
    result = imgtext_assist.seed(source, work)
    return work, result


def test_seed_keeps_low_confidence_and_vertical(seeded):
    work, result = seeded
    value = json.loads((work / "elements.seed.json").read_text("utf-8"))
    assert result["candidateCount"] == 3
    assert value["elements"][1]["candidateStatus"] == "needs_confirmation"
    assert value["elements"][2]["text"] == "竖排"
    assert (work / "seed-preview.png").is_file()


def test_source_and_seed_tampering_rejected(seeded):
    work, result = seeded
    with pytest.raises(PptutorError) as caught:
        imgtext_assist.build(work, {}, seed_hash="old")
    assert caught.value.code == "SEED_CHANGED"
    (work / "source.png").write_bytes(b"changed")
    with pytest.raises(PptutorError) as caught:
        imgtext_assist.build(work, {})
    assert caught.value.code == "SEED_CHANGED"


def test_build_native_text_font_triple_and_revision(seeded, monkeypatch):
    work, _ = seeded

    def fake_cutout(source, destination, elements, **kwargs):
        destination.mkdir(parents=True)
        for name in ("base", "clean", "mask"):
            Image.new("RGB", (800, 450), "white").save(destination / (name + ".png"))
        return {"base": str(destination / "base.png"), "clean": str(destination / "clean.png"),
                "mask": str(destination / "mask.png"), "graphics": [], "reviewItems": []}

    monkeypatch.setattr(imgtext_ocr, "assist_cutout", fake_cutout)
    first = imgtext_assist.build(work, {"fix": {"2": "确认文字"}, "x": [3]})
    assert first["lockedTexts"][0]["text"] == "竖排"
    with zipfile.ZipFile(first["pptx"]) as archive:
        xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
    assert "确认文字" in xml and "<a:latin" in xml and "<a:ea" in xml and "<a:cs" in xml
    assert first["previewKind"] == "PIL approximation"
    second = imgtext_assist.build(work, {})
    assert first["pptx"] != second["pptx"] and second["mode"] == "image_fallback"
    assert second["reviewItems"]
