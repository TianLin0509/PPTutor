"""PIL preview and editable OOXML assembly without python-pptx in the base app."""
from __future__ import annotations

import io
import os
import re
import zipfile
from collections import Counter
from statistics import median
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from .config import resource_path


def measure_ink(image, box):
    """Use the inside majority, not an outside ring that can cross a card edge.

    Group nearby gradient/JPEG colors before counting. White-on-red and
    black-on-white follow the same rule; no assumption that text is dark.
    """
    box = [max(0, box[0] - 2), max(0, box[1] - 2),
           min(image.width, box[2] + 2), min(image.height, box[3] + 2)]
    crop = image.crop(box).convert("RGB")
    pixels = list(crop.get_flattened_data())
    if not pixels:
        return None
    buckets = Counter(tuple(c // 16 for c in p) for p in pixels)
    dominant = buckets.most_common(1)[0][0]
    background_pixels = [p for p in pixels if tuple(c // 16 for c in p) == dominant]
    background = tuple(round(median(p[c] for p in background_pixels)) for c in range(3))
    distances = [sum((p[c] - background[c]) ** 2 for c in range(3)) for p in pixels]
    far = sorted(distances)[max(0, len(distances) - 1 - len(distances) // 100)]
    if far < 900:
        return None
    core = [p for p, distance in zip(pixels, distances) if distance >= far * 0.65]
    ink = tuple(round(median(p[c] for p in core)) for c in range(3))
    foreground = [(i % crop.width, i // crop.width) for i, distance in enumerate(distances) if distance > far * 0.3]
    tight = [box[0] + min(x for x, _ in foreground), box[1] + min(y for _, y in foreground),
             box[0] + max(x for x, _ in foreground) + 1, box[1] + max(y for _, y in foreground) + 1]
    return tight, ink, background


def font_path(family="Microsoft YaHei", bold=False):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    names = {"Microsoft YaHei": "msyhbd.ttc" if bold else "msyh.ttc",
             "微软雅黑": "msyhbd.ttc" if bold else "msyh.ttc",
             "Arial": "arialbd.ttf" if bold else "arial.ttf"}
    if family in names and (fonts / names[family]).is_file():
        return fonts / names[family], family
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name.lower().split(" (")[0] == family.lower() and (fonts / value).is_file():
                    return fonts / value, family
    for filename in ("msyh.ttc", "msyh.ttf"):
        if (fonts / filename).is_file():
            return fonts / filename, "Microsoft YaHei"
    raise ValueError("未找到微软雅黑字体，请安装字体后重试")


def _fit(text, path, width, height):
    low, high = 1, max(4, int(height * 4))
    while low < high:
        mid = (low + high + 1) // 2
        font = ImageFont.truetype(str(path), mid)
        b = font.getbbox(text or "字")
        if b[2] - b[0] <= width and b[3] - b[1] <= height:
            low = mid
        else:
            high = mid - 1
    return ImageFont.truetype(str(path), low)


def layout(document):
    output, reviews = [], []
    for element in document["elements"]:
        if element["type"] != "text":
            continue
        e = dict(element)
        path, family = font_path(document["font"], e.get("bold", False))
        if family != document["font"]:
            reviews.append({"id": e["id"], "reason": f"字体 {document['font']} 不可用，已回退 {family}"})
        x0, y0, x1, y1 = e["bbox"]
        vertical = len(e["text"]) > 1 and x1 - x0 < (y1 - y0) * 0.6 and "\n" not in e["text"]
        lines = e.get("lines")
        if not lines:
            strings = list(e["text"]) if vertical else e["text"].split("\n")
            pitch = (y1 - y0) / max(1, len(strings))
            lines = [{"text": text, "bbox": [x0, y0 + i * pitch, x1, y0 + (i + 1) * pitch]}
                     for i, text in enumerate(strings)]
        size = min(_fit(line["text"], path, line["bbox"][2] - line["bbox"][0],
                        line["bbox"][3] - line["bbox"][1]).size for line in lines)
        font = ImageFont.truetype(str(path), size)
        e.update(fontFamily=family, fontFile=str(path), fontPx=size, vertical=vertical, lines=lines)
        output.append(e)
    return output, reviews


def preview(source, document, cutout, destination):
    image = Image.open(cutout["base"]).convert("RGB")
    for graphic in cutout["graphics"]:
        sprite = Image.open(graphic["asset"])
        image.paste(sprite, tuple(graphic["bbox"][:2]), sprite if sprite.mode == "RGBA" else None)
    draw = ImageDraw.Draw(image)
    for e in document["layout"]:
        font = ImageFont.truetype(e["fontFile"], e["fontPx"])
        for line in e["lines"]:
            x0, y0, x1, _ = line["bbox"]
            b = font.getbbox(line["text"])
            width = b[2] - b[0]
            align = e.get("align", "left")
            x = (x0 + x1 - width) / 2 if align == "center" else x1 - width if align == "right" else x0
            draw.text((x - b[0], y0 - b[1]), line["text"], font=font, fill=e.get("color") or "#202020")
    image.save(destination)
    return image


def pixel_gate(source, rebuilt, document, cutout):
    source = Image.open(source).convert("RGB")
    difference = ImageChops.difference(source, rebuilt)
    # Text rendering differences are expected; compare non-text pixels separately.
    mask = Image.new("L", source.size, 255)
    mask_draw = ImageDraw.Draw(mask)
    for e in document["layout"]:
        for line in e["lines"]:
            x0, y0, x1, y1 = line["bbox"]
            mask_draw.rectangle((max(0, x0 - 2), max(0, y0 - 2), x1 + 2, y1 + 2), fill=0)
    stats = ImageStat.Stat(difference, mask)
    nontext = sum(stats.mean) / 3 if stats.count[0] else 0.0
    issues, checks = [], []
    for e in document["layout"]:
        b = tuple(e["bbox"])
        original, generated = source.crop(b), rebuilt.crop(b)
        # A large loss of local contrast detects invisible/covered text; this is
        # a preview heuristic, not a claim of semantic OCR correctness.
        old = sum(ImageStat.Stat(original).stddev)
        new = sum(ImageStat.Stat(generated).stddev)
        ratio = new / old if old else 1.0
        checks.append({"id": e["id"], "kind": "text_contrast", "ratio": round(ratio, 4)})
        if old > 15 and ratio < 0.2:
            issues.append({"id": e["id"], "reason": "文字对比度明显丢失", "severity": "lost"})
        if e["fontPx"] < 3:
            issues.append({"id": e["id"], "reason": "字体缩小到 3 像素以下", "severity": "review"})
    if nontext > 3:
        issues.append({"reason": "非文字区差异偏大", "severity": "review", "meanAbsoluteDifference": round(nontext, 3)})
    return {"passed": not issues, "checks": checks, "issues": issues,
            "nonTextMeanAbsoluteDifference": round(nontext, 4), "previewKind": "PIL approximation",
            "meaning": "像素与布局初筛，非 PowerPoint 回图或文字准确率验收"}


def _picture(index, rid, name, box, scale):
    x0, y0, x1, y1 = box
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="{index}" name={quoteattr(name)}/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="{round(x0*scale)}" y="{round(y0*scale)}"/>'
        f'<a:ext cx="{round((x1-x0)*scale)}" cy="{round((y1-y0)*scale)}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')


def _text(index, e, scale):
    x0, y0, x1, y1 = e["bbox"]
    font = ImageFont.truetype(e["fontFile"], e["fontPx"])
    ascent, descent = font.getmetrics()
    # DrawingML top anchors the font cell, PIL measures the actual ink.
    ink_top = font.getbbox(e["lines"][0]["text"])[1]
    top = y0 - ink_top
    pt_per_pixel = scale / 12700
    size = max(100, round(e["fontPx"] * pt_per_pixel * 100))
    family = quoteattr(e["fontFamily"])
    rgb = (e.get("color") or "#202020").lstrip("#")
    bold = "1" if e.get("bold") else "0"
    align = {"left": "l", "center": "ctr", "right": "r"}.get(e.get("align"), "l")
    paragraphs = []
    for i, line in enumerate(e["lines"]):
        pitch = (e["lines"][i + 1]["bbox"][1] - line["bbox"][1]) if i + 1 < len(e["lines"]) else ascent + descent
        spacing = max(100, round(pitch * pt_per_pixel * 100))
        paragraphs.append(f'<a:p><a:pPr algn="{align}"><a:lnSpc><a:spcPts val="{spacing}"/></a:lnSpc>'
            '<a:spcBef><a:spcPts val="0"/></a:spcBef><a:spcAft><a:spcPts val="0"/></a:spcAft></a:pPr>'
            f'<a:r><a:rPr lang="zh-CN" altLang="en-US" sz="{size}" b="{bold}">'
            f'<a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill><a:latin typeface={family}/>'
            f'<a:ea typeface={family}/><a:cs typeface={family}/></a:rPr><a:t>{escape(line["text"])}</a:t></a:r>'
            f'<a:endParaRPr sz="{size}"/></a:p>')
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{index}" name={quoteattr(e["id"])}/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{round(x0*scale)}" y="{round(top*scale)}"/>'
        f'<a:ext cx="{round((x1-x0+2)*scale)}" cy="{round((y1-top+descent)*scale)}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>'
        '<p:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" anchor="t"><a:noAutofit/></a:bodyPr>'
        '<a:lstStyle/>' + ''.join(paragraphs) + '</p:txBody></p:sp>')


def assemble(document, cutout, destination):
    width, height = document["img_w"], document["img_h"]
    cx = 12192000  # 13 1/3 inches; preserve source aspect ratio.
    scale, cy = cx / width, round(cx * height / width)
    media = [("base", cutout["base"], [0, 0, width, height])]
    media += [(g["id"], g["asset"], g["bbox"]) for g in cutout["graphics"]]
    pictures = [_picture(i + 2, f"rId{i+2}", name, box, scale) for i, (name, _, box) in enumerate(media)]
    texts = [_text(i + len(media) + 2, e, scale) for i, e in enumerate(document["layout"])]
    from .imgtext import _slide_xml
    # _slide_xml supplies a base picture; use our picture list instead.
    slide = _slide_xml(pictures[1:] + texts, cx, cy).replace("rIdImg", "rId2")
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout7.xml"/>')
    for i, _ in enumerate(media):
        rels += f'<Relationship Id="rId{i+2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/assist{i}.png"/>'
    rels += '</Relationships>'
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive open protects user outputs even if another process creates one.
    with destination.open("xb") as file, zipfile.ZipFile(file, "w", zipfile.ZIP_DEFLATED) as out, zipfile.ZipFile(resource_path("assets", "blank_16x9.pptx")) as template:
        for item in template.infolist():
            data = template.read(item.filename)
            if item.filename == "ppt/slides/slide1.xml":
                data = slide.encode("utf-8")
            elif item.filename == "ppt/slides/_rels/slide1.xml.rels":
                data = rels.encode("utf-8")
            elif item.filename == "ppt/presentation.xml":
                data = re.sub(r"<p:sldSz[^>]*/>", f'<p:sldSz cx="{cx}" cy="{cy}"/>', data.decode("utf-8")).encode("utf-8")
            elif item.filename == "[Content_Types].xml" and b'Extension="png"' not in data:
                data = data.replace(b"<Default Extension=", b'<Default Extension="png" ContentType="image/png"/><Default Extension=', 1)
            out.writestr(item, data)
        for i, (_, path, _) in enumerate(media):
            out.write(path, f"ppt/media/assist{i}.png")
    return destination
