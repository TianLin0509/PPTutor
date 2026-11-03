"""OCR candidates -> validated flash deltas -> movable graphics + editable text."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .hub_library.library import PptutorError
from .imgtext_ops import apply_ops, bbox

_APP = None


def ensure_qt():
    global _APP
    from PySide6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _hash_file(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _seed_hash(value):
    value = {k: v for k, v in value.items() if k != "seedHash"}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _json(path, value):
    from .hub_cli import write_json
    write_json(Path(path), value)


def seed(image, workdir, *, progress=lambda *a, **kw: None):
    from . import imgtext, imgtext_ocr
    from .imgtext_assist_render import font_path, measure_ink
    start = time.monotonic()
    image, workdir = Path(image), Path(workdir)
    if not image.is_file():
        raise PptutorError("SOURCE_UNAVAILABLE", "找不到源图片")
    if workdir.exists() and any(workdir.iterdir()):
        raise PptutorError("OUTPUT_EXISTS", "seed 工作目录必须为空，避免覆盖已有产物")
    workdir.mkdir(parents=True, exist_ok=True)
    source = workdir / "source.png"
    with Image.open(image) as original:
        if original.width * original.height > 40_000_000:
            raise PptutorError("INVALID_REQUEST", "图片超过 4000 万像素，请使用原始 PPT 页截图")
        original.convert("RGB").save(source)
    ensure_qt()
    progress("ocr", "正在识别全部文字候选")
    rows = imgtext_ocr.recognize_one(source)
    if not rows:
        raise PptutorError("OCR_EMPTY", "OCR 没有识别到文字，保留源图，请检查图片或改用手工标注")
    qimage = imgtext.load_rgb(source)
    width, height = qimage.width(), qimage.height()
    pixels = imgtext._Pixels(qimage)
    pil_image = Image.open(source).convert("RGB")
    result = {"apiVersion": 1, "img_w": width, "img_h": height,
              "font": "Microsoft YaHei", "sourceHash": _hash_file(source),
              "originalSourceHash": _hash_file(image), "elements": []}
    reviews = []
    pt_per_px = 13.3333 * 72 / width
    for index, row in enumerate(rows, 1):
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        box = bbox([max(0, row["box"][0]), max(0, row["box"][1]),
                    min(width, row["box"][2]), min(height, row["box"][3])], width, height)
        tight, ink, background, stroke, coverage = imgtext.measure_run(pixels, box)
        measured = measure_ink(pil_image, box)
        if measured is not None:
            tight, ink, background = measured
        reasons = []
        score = float(row.get("score", 1.0))
        if score < imgtext.DEFAULT_MIN_SCORE:
            reasons.append("低置信 OCR，待确认")
        if tight is None:
            reasons.append("未量到稳定墨迹，待确认")
            tight, ink, background = box, (32, 32, 32), (255, 255, 255)
        h, w = tight[3] - tight[1], tight[2] - tight[0]
        if coverage > imgtext.MAX_INK_COVERAGE:
            reasons.append("文字区可能含色块或图案")
        if len(text) >= 2 and w < h * imgtext.MIN_ASPECT_RATIO:
            reasons.append("可能是竖排或误并文字")
        size = imgtext.fit_point_size(text, h, w, pt_per_px)
        if size < imgtext.DEFAULT_MIN_PT:
            reasons.append("小字，待确认")
        run = imgtext.TextRun(text=text, box=tight, ink=ink, background=background,
                              point_size=size, score=score)
        if imgtext.background_jitter(pixels, run) > imgtext.MAX_BACKGROUND_JITTER:
            reasons.append("纹理背景，补背景需复核")
        if imgtext.has_math(text):
            reasons.append("含公式符号，需确认转字还是保留贴图")
        el = {"id": f"s{index}", "type": "text", "bbox": list(tight), "text": text,
              "bold": False, "color": "#%02X%02X%02X" % ink,
              "seed_confidence": score, "candidateStatus": "needs_confirmation" if reasons else "ready",
              "reviewReasons": reasons}
        result["elements"].append(el)
        reviews.extend({"id": el["id"], "reason": reason, "text": text} for reason in reasons)
    result["seedHash"] = _seed_hash(result)
    _json(workdir / "elements.seed.json", result)
    _json(workdir / "ocr-rows.json", {"rows": rows})
    lines = [f"# seed {width}x{height}；{len(result['elements'])} 行；所有坐标为原图像素",
             "# 只输出 ops 差异；字号自动度量；needs_confirmation 行请 confirm/fix/x。"]
    preview = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(preview)
    path, _ = font_path()
    font = ImageFont.truetype(str(path), max(12, width // 120))
    for el in result["elements"]:
        lines.append(f"{el['id']} {el['bbox']} {el['candidateStatus']} {el['text']}")
        colour = "#D07500" if el["reviewReasons"] else "#3478F6"
        draw.rectangle(el["bbox"], outline=colour, width=2)
        draw.text((el["bbox"][0], max(0, el["bbox"][1] - font.size)), el["id"], fill=colour, font=font)
    (workdir / "seed.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    preview.save(workdir / "seed-preview.png")
    return {"workdir": str(workdir), "seed": str(workdir / "elements.seed.json"),
            "seedText": str(workdir / "seed.txt"), "preview": str(workdir / "seed-preview.png"),
            "sourceHash": result["sourceHash"], "seedHash": result["seedHash"],
            "candidateCount": len(result["elements"]), "reviewItems": reviews,
            "timing": {"seedSeconds": round(time.monotonic() - start, 3)}}


def build(workdir, ops, *, seed_hash=None, output=None, progress=lambda *a, **kw: None):
    from . import imgtext_assist_render as render, imgtext_ocr
    start = time.monotonic()
    workdir = Path(workdir)
    seed_path, source = workdir / "elements.seed.json", workdir / "source.png"
    if not seed_path.is_file() or not source.is_file():
        raise PptutorError("SEED_CHANGED", "缺少 seed 或源图，请重新生成初稿")
    value = json.loads(seed_path.read_text("utf-8"))
    if value.get("apiVersion") != 1 or _seed_hash(value) != value.get("seedHash"):
        raise PptutorError("SEED_CHANGED", "seed 内容已变化，请重新生成初稿")
    if seed_hash is not None and seed_hash != value["seedHash"]:
        raise PptutorError("SEED_CHANGED", "ops 对应另一份初稿")
    if _hash_file(source) != value["sourceHash"]:
        raise PptutorError("SEED_CHANGED", "源图已变化，请重新生成初稿")
    document = apply_ops(value, ops)
    if output and Path(output).exists():
        raise PptutorError("OUTPUT_EXISTS", "输出已存在，请选择新文件名")
    # Reserve an exclusive revision directory. Concurrent requests cannot share
    # temporary files or overwrite the previous attempt.
    revision = None
    for index in range(1, 4):
        candidate = workdir / f"revision-{index:02d}"
        try:
            candidate.mkdir()
            revision = candidate
            break
        except FileExistsError:
            continue
    if revision is None:
        raise PptutorError("REVISION_LIMIT", "本初稿已尝试初次装配和两轮修补，请创建新工作目录")
    destination = Path(output) if output else revision / "editable.pptx"
    _json(revision / "ops.json", ops)
    reviews = [{"id": e["id"], "text": e.get("text", ""), "reason": reason}
               for e in document["elements"] if e.get("candidateStatus") == "needs_confirmation"
               for reason in e.get("reviewReasons", [])]
    if not ops:
        reviews.insert(0, {"reason": "未经过 Agent 看图；OCR 候选与自动图案分区均需复核"})
    progress("cutout", "正在补背景并切分可拖动图案")
    cutout = imgtext_ocr.assist_cutout(source, revision / "assets", document["elements"], auto_graphics=not bool(ops))
    reviews.extend(cutout["reviewItems"])
    document["layout"], font_reviews = render.layout(document)
    reviews.extend(font_reviews)
    progress("assemble", "正在生成可编辑文字和图案")
    render.assemble(document, cutout, destination)
    preview_path = revision / "preview.png"
    preview = render.preview(source, document, cutout, preview_path)
    progress("check", "正在检查文字和非文字区域")
    gate = render.pixel_gate(source, preview, document, cutout)
    reviews.extend(gate["issues"])
    _json(revision / "elements.json", document)
    _json(revision / "pixel-gate.json", gate)
    result = {"pptx": str(destination), "preview": str(preview_path),
              "elements": str(revision / "elements.json"), "pixel_gate": gate,
              "lockedTexts": document["lockedTexts"], "reviewItems": reviews, "mode": document["mode"],
              "previewKind": "PIL approximation", "revision": index,
              "timing": {"buildSeconds": round(time.monotonic() - start, 3)}}
    _json(revision / "result.json", result)
    return result
