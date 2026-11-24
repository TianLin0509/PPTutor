"""测试用：用 python-pptx 造真实可打开的小素材包（PPT Doctor Material v1）。"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

from pptx_finder.materials import MARKER

ROOT = Path(__file__).resolve().parents[1]


def png_bytes(color=(10, 89, 247, 255), size=(30, 20)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_pack(path: Path, rows: list[dict], *, author: str = "") -> Path:
    """rows: {"id", "name", "category", "text"?, "picture"?: bytes, "only_picture"?: bool}"""
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Pt(960), Pt(540)
    blank = prs.slide_layouts[6]
    for row in rows:
        slide = prs.slides.add_slide(blank)
        if row.get("picture"):
            slide.shapes.add_picture(io.BytesIO(row["picture"]), Pt(100), Pt(100), Pt(60), Pt(40))
        if not row.get("only_picture"):
            box = slide.shapes.add_textbox(Pt(200), Pt(200), Pt(200), Pt(40))
            box.text_frame.text = row.get("text", "示例文字")
        meta = {k: row[k] for k in ("id", "name", "category") if k in row}
        slide.notes_slide.notes_text_frame.text = MARKER + json.dumps(meta, ensure_ascii=False)
    if author:
        prs.core_properties.author = author
        prs.core_properties.last_modified_by = author
    prs.save(str(path))
    return path


def rows(n: int, category: str = "箭头与流程", prefix: str = "m") -> list[dict]:
    return [{"id": f"{prefix}{i}", "name": f"{category}素材{i}", "category": category} for i in range(1, n + 1)]


def tool():
    spec = importlib.util.spec_from_file_location(
        "build_materials_component", ROOT / "tools" / "build_materials_component.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_renders(folder: Path, ids, size=(120, 60)) -> Path:
    """模拟 PowerPoint 渲染出的 3x 透明 PNG。"""
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    for i, sid in enumerate(ids):
        Image.new("RGBA", size, (i * 20 % 255, 89, 247, 255)).save(folder / f"{sid}.png")
    return folder


def build_pack_dir(tmp: Path, pack_rows: list[dict], version="1.2.0", *, core=False):
    """造包 → 跑构建工具（带假缩略图）→ 返回 (输出目录, 内置精选目录或 None, audit)。"""
    t = tool()
    src = make_pack(tmp / f"src-{version}.pptx", pack_rows)
    renders = fake_renders(tmp / f"renders-{version}", [r["id"] for r in pack_rows])
    out = tmp / f"out-{version}"
    core_dir = tmp / f"core-{version}" if core else None
    audit = t.run(src, version, out, core_out=core_dir, thumbs_from=renders, log=lambda *_: None)
    return out, core_dir, audit
