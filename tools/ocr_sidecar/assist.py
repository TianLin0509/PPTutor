"""Image-only enhanced imgtext work, kept beside OCR's existing OpenCV runtime.

Adapted from img2ppt-lite flash: glyph masks + TELEA, semantic cutouts, and
contour fallback. No dependency on the installed skill or the GUI process.
"""
from pathlib import Path

import cv2
import numpy as np


def _read(path):
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片：{path}")
    return image


def _write(path, image):
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("PNG 编码失败")
    buffer.tofile(str(path))


def _mode(pixels):
    if not len(pixels):
        return np.array([255, 255, 255], dtype=np.uint8)
    pixels = pixels[::max(1, len(pixels) // 20000)]
    bins = (pixels.astype(np.int32) // 8)
    keys = bins[:, 0] * 1024 + bins[:, 1] * 32 + bins[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    return np.median(pixels[keys == values[counts.argmax()]], axis=0).astype(np.uint8)


def _box(value, width, height):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("需要四个坐标")
    x0, y0, x1, y1 = [int(v) for v in value]
    if not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
        raise ValueError("坐标超出图片")
    return x0, y0, x1, y1


def cutout(request):
    cv2.setNumThreads(1)
    source = _read(request["image"])
    height, width = source.shape[:2]
    output = Path(request["outputDir"])
    output.mkdir(parents=True, exist_ok=True)
    elements = request["elements"]
    mask = np.zeros((height, width), np.uint8)
    review = []
    prepared = source.copy()
    for el in elements:
        if el["type"] != "text":
            continue
        for line in el.get("lines") or [el]:
            x0, y0, x1, y1 = _box(line["bbox"], width, height)
            x0, y0, x1, y1 = max(0, x0 - 2), max(0, y0 - 2), min(width, x1 + 2), min(height, y1 + 2)
            crop = source[y0:y1, x0:x1]
            candidates = crop.reshape(-1, 3)
            if el.get("color"):
                rgb = el["color"].lstrip("#")
                ink_color = np.array([int(rgb[i:i+2], 16) for i in (4, 2, 0)], np.int16)
                nonink = np.max(np.abs(candidates.astype(np.int16) - ink_color), axis=1) > 45
                if nonink.any():
                    candidates = candidates[nonink]
            bg = _mode(candidates).astype(np.int16)
            distance = np.max(np.abs(crop.astype(np.int16) - bg), axis=2)
            ink = (distance > 18).astype(np.uint8) * 255
            if ink.mean() > 190:
                review.append({"id": el["id"], "reason": "文字区纹理复杂，补背景需复核"})
            # Thin AA strokes need a one-pixel perimeter. Mask stays inside box.
            ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))
            mask[y0:y1, x0:x1] |= ink
            prepared[y0:y1, x0:x1][ink > 0] = bg.astype(np.uint8)
    clean = cv2.inpaint(prepared, mask, 3, cv2.INPAINT_TELEA) if mask.any() else source.copy()
    _write(output / "clean-text.png", clean)
    _write(output / "text-mask.png", mask)
    graphics = [dict(e) for e in elements if e["type"] in ("card", "graphic")]
    if request.get("autoGraphics"):
        bg = _mode(clean.reshape(-1, 3)).astype(np.int16)
        foreground = (np.max(np.abs(clean.astype(np.int16) - bg), axis=2) > 30).astype(np.uint8) * 255
        foreground[cv2.dilate(mask, np.ones((7, 7), np.uint8)) > 0] = 0
        joined = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w >= 10 and h >= 10 and w * h >= 100 and w * h < width * height * 0.9:
                boxes.append([max(0, x - 2), max(0, y - 2), min(width, x + w + 2), min(height, y + h + 2)])
        boxes.sort(key=lambda b: (b[1], b[0]))
        for i, box in enumerate(boxes[:200], 1):
            el = {"id": f"auto{i}", "type": "graphic", "bbox": box, "desc": "图像算法候选"}
            graphics.append(el)
            review.append({"id": el["id"], "reason": "图像算法自动分区，尚未经 Agent 看图确认", "bbox": box})
        if len(boxes) > 200:
            review.append({"reason": f"检测到 {len(boxes)} 个图案区域，仅切分前 200 个，其余保留在底图"})
    # Preserve bitmap-only OCR rows above movable cards/graphics as independent
    # carriers, so adding a card cannot hide an intentionally locked formula.
    for el in elements:
        if el["type"] == "text_in_graphic":
            graphics.append({"id": "locked-" + el["id"], "type": "graphic", "bbox": el["bbox"],
                             "desc": "保留图中文字", "lockedCarrier": True})
    graphics.sort(key=lambda e: (bool(e.get("lockedCarrier")), e["type"] != "card"))
    base = clean.copy()
    page_bg = _mode(clean.reshape(-1, 3))
    for el in graphics:
        x0, y0, x1, y1 = _box(el["bbox"], width, height)
        # Erase the carrier from the base; do not duplicate it behind the sprite.
        base[y0:y1, x0:x1] = page_bg
    for el in graphics:
        x0, y0, x1, y1 = _box(el["bbox"], width, height)
        sprite = clean[y0:y1, x0:x1].copy()
        if el.get("alpha"):
            # Preserve unclassified pixels instead of guessing background alpha.
            review.append({"id": el["id"], "reason": "alpha 请求保留为不透明贴图，需人工核对透明边缘"})
        destination = output / f"{el['id']}.png"
        _write(destination, sprite)
        el["asset"] = str(destination)
    _write(output / "base.png", base)
    return {"base": str(output / "base.png"), "clean": str(output / "clean-text.png"),
            "mask": str(output / "text-mask.png"), "graphics": graphics, "reviewItems": review}
