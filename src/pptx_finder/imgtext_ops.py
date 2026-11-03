"""Strict flash-compatible deltas. No executable instructions or font sizes."""
from __future__ import annotations

import copy
import math
import re

from .hub_library.library import PptutorError


def invalid(message):
    raise PptutorError("INVALID_OPS", message)


def bbox(value, width, height):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
        invalid("bbox 必须为四个有限数值")
    box = [int(round(v)) for v in value]
    if not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height:
        invalid("bbox 超出图片或宽高不为正")
    return box


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        invalid("color 必须为 #RRGGBB")
    return value.upper()


def apply_ops(seed, ops):
    if not isinstance(ops, dict):
        invalid("ops 必须为对象；无视觉模型请给空对象")
    known_fields = {"d", "x", "fix", "bbox", "b", "attr", "merge", "t", "c", "g", "p", "font", "confirm"}
    if set(ops) - known_fields:
        invalid("不支持的 ops 字段：" + ",".join(sorted(set(ops) - known_fields)))
    width, height = seed["img_w"], seed["img_h"]
    rows = {e["id"]: copy.deepcopy(e) for e in seed["elements"]}

    def key(value):
        match = re.fullmatch(r"s?([1-9][0-9]*)", str(value))
        if not match or "s" + match[1] not in rows:
            invalid(f"未知 seed 行号：{value!r}")
        return "s" + match[1]

    def refs(name):
        value = ops.get(name, [])
        if not isinstance(value, list):
            invalid(f"{name} 必须为数组")
        ids = [key(v) for v in value]
        if len(ids) != len(set(ids)):
            invalid(f"{name} 包含重复行号")
        return set(ids)

    drop, locked, confirmed = refs("d"), refs("x"), refs("confirm")
    if drop & locked or (drop | locked) & confirmed:
        invalid("同一行不能同时删除、锁图或确认转字")
    for field in ("fix", "bbox", "attr", "p"):
        if not isinstance(ops.get(field, {}), dict):
            invalid(f"{field} 必须为对象")
    for ref, text in ops.get("fix", {}).items():
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            invalid("fix 必须为非空文字，最多 20000 字")
        row = rows[key(ref)]
        row["text"] = text
        row["ocr_locked"] = True
        confirmed.add(row["id"])
    for ref, value in ops.get("bbox", {}).items():
        rows[key(ref)]["bbox"] = bbox(value, width, height)
        confirmed.add(key(ref))
    if "b" in ops:
        bold = refs("b")
        for row in rows.values():
            row["bold"] = row["id"] in bold
    for ref, attrs in ops.get("attr", {}).items():
        if not isinstance(attrs, dict):
            invalid("attr 的每行必须为对象")
        for name, value in attrs.items():
            name = {"b": "bold", "c": "color", "a": "align"}.get(name, name)
            if name == "color":
                value = color(value)
            elif name == "bold":
                if not isinstance(value, bool):
                    invalid("bold 必须为布尔值")
            elif name == "align":
                if value not in ("left", "center", "right"):
                    invalid("align 只接受 left/center/right")
            else:
                invalid(f"不支持属性 {name}；字号由 Doctor 度量")
            rows[key(ref)][name] = value
        confirmed.add(key(ref))
    if confirmed & (drop | locked):
        invalid("已修改的行不能同时删除或锁图")
    for rid in confirmed:
        rows[rid]["candidateStatus"] = "confirmed"
    elements = []
    for rid, row in rows.items():
        if rid in drop:
            continue
        if rid in locked:
            row["type"] = "text_in_graphic"
        elements.append(row)
    merged_ids = set()
    if not isinstance(ops.get("merge", []), list):
        invalid("merge 必须为数组")
    for group in ops.get("merge", []):
        if not isinstance(group, list) or len(group) < 2:
            invalid("merge 每组至少两行")
        ids = [key(v) for v in group]
        if len(set(ids)) != len(ids) or set(ids) & (drop | locked | merged_ids):
            invalid("merge 不能引用重复、删除、锁图或已合并的行")
        parts = [rows[rid] for rid in ids]
        merged = copy.deepcopy(parts[0])
        merged["text"] = "\n".join(p["text"] for p in parts)
        merged["bbox"] = [min(p["bbox"][0] for p in parts), min(p["bbox"][1] for p in parts),
                          max(p["bbox"][2] for p in parts), max(p["bbox"][3] for p in parts)]
        merged["lines"] = [{"text": p["text"], "bbox": p["bbox"]} for p in parts]
        merged["candidateStatus"] = "confirmed"
        elements = [e for e in elements if e["id"] not in ids] + [merged]
        merged_ids.update(ids)
    for field, kind in (("t", "text"), ("c", "card"), ("g", "graphic")):
        values = ops.get(field, [])
        if not isinstance(values, list) or len(values) > 500:
            invalid(f"{field} 应为最多 500 项的数组")
        for i, value in enumerate(values, 1):
            lo, hi = {"t": (5, 7), "c": (4, 5), "g": (4, 6)}[field]
            if not isinstance(value, list) or not lo <= len(value) <= hi:
                invalid(f"{field}[{i}] 字段数应为 {lo}–{hi}")
            el = {"id": ("nt" if field == "t" else field) + str(i), "type": kind,
                  "bbox": bbox(value[:4], width, height)}
            if field == "t":
                if not isinstance(value[4], str) or not value[4].strip():
                    invalid("新增文字不能为空")
                if len(value) > 5 and not isinstance(value[5], bool):
                    invalid("新增文字 bold 必须为布尔值")
                el.update(text=value[4], bold=value[5] if len(value) > 5 else False,
                          color=color(value[6]) if len(value) > 6 else None, candidateStatus="confirmed")
            elif field == "c" and len(value) > 4:
                radius = value[4]
                if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 0 <= radius <= 1000:
                    invalid("卡片圆角必须为 0–1000 像素")
                el["radius"] = radius
            elif field == "g":
                if len(value) > 4 and not isinstance(value[4], str):
                    invalid("图案说明必须为文字")
                if len(value) > 5 and not isinstance(value[5], bool):
                    invalid("图案 alpha 必须为布尔值")
                el.update(desc=value[4] if len(value) > 4 else "图案", alpha=value[5] if len(value) > 5 else False)
            elements.append(el)
    by_id = {e["id"]: e for e in elements}
    for child, parent in ops.get("p", {}).items():
        if child not in by_id or parent not in by_id or child == parent:
            invalid("父子关系引用不存在或引用自身")
        if by_id[parent]["type"] not in ("card", "graphic"):
            invalid("父元素必须为卡片或图案")
        by_id[child]["parent"] = parent
    for el in elements:
        seen, current = {el["id"]}, el
        while "parent" in current:
            if current["parent"] in seen:
                invalid("父子关系包含循环")
            seen.add(current["parent"])
            current = by_id[current["parent"]]
        el["bbox"] = bbox(el["bbox"], width, height)
    font = ops.get("font", seed.get("font", "Microsoft YaHei"))
    if not isinstance(font, str) or not 1 <= len(font) <= 128 or any(c in font for c in "\x00\r\n"):
        invalid("font 必须为字体名称")
    locked_texts = [{"id": rid, "text": rows[rid]["text"], "bbox": rows[rid]["bbox"],
                     "reason": "Agent 删除 OCR 候选，原像素保留" if rid in drop else "Agent 指定文字保留在图案中"}
                    for rid in sorted(drop | locked)]
    return {"img_w": width, "img_h": height, "font": font, "elements": elements,
            "lockedTexts": locked_texts, "mode": "agent_assisted" if ops else "image_fallback"}
