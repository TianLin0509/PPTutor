"""测试夹具生成：用 python-pptx 造正文+备注 pptx；手工注入 SmartArt / 反转页序。"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.util import Inches

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_DIAGRAM_DATA = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData"
)


def make_pptx(path: str | Path, slides: list[dict]) -> str:
    """slides: [{"body": str, "notes": str|None}]。返回路径。"""
    prs = Presentation()
    blank = prs.slide_layouts[6]
    for s in slides:
        slide = prs.slides.add_slide(blank)
        tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
        tb.text_frame.text = s.get("body", "")
        notes = s.get("notes")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    prs.save(str(path))
    return str(path)


def reverse_slide_order(path: str | Path) -> str:
    """反转 presentation.xml 的 sldIdLst 顺序（slide 文件保持不变）。
    用于验证解析器按放映顺序、而非文件名顺序读页。"""
    path = str(path)
    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        data = {n: zin.read(n) for n in names}
    root = etree.fromstring(data["ppt/presentation.xml"])
    lst = root.find(f"{{{P_NS}}}sldIdLst")
    children = list(lst)
    for c in children:
        lst.remove(c)
    for c in reversed(children):
        lst.append(c)
    data["ppt/presentation.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, data[n])
    shutil.move(tmp, path)
    return path


def inject_smartart(path: str | Path, slide_no: int, text: str) -> str:
    """给第 slide_no 页注入一个含 text 的 SmartArt diagram data part + 关系。
    （slide_no 对应 ppt/slides/slideN.xml，未重排前 N==放映序）"""
    path = str(path)
    diagram_part = f"ppt/diagrams/data{slide_no}.xml"
    slide_part = f"ppt/slides/slide{slide_no}.xml"
    rels_part = f"ppt/slides/_rels/slide{slide_no}.xml.rels"

    diagram_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<dgm:dataModel xmlns:dgm="{DGM_NS}" xmlns:a="{A_NS}">'
        "<dgm:ptLst><dgm:pt><dgm:t><a:bodyPr/><a:p><a:r>"
        f"<a:t>{text}</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst>"
        "</dgm:dataModel>"
    ).encode("utf-8")

    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        data = {n: zin.read(n) for n in names}

    # 更新 slide rels：追加一个 diagramData 关系
    if rels_part in data:
        rroot = etree.fromstring(data[rels_part])
    else:
        rroot = etree.fromstring(
            f'<Relationships xmlns="{RELS_NS}"></Relationships>'.encode()
        )
    existing_ids = [r.get("Id") for r in rroot]
    n = 1
    while f"rId{n}" in existing_ids:
        n += 1
    new_id = f"rId{n}"
    rel = etree.SubElement(rroot, f"{{{RELS_NS}}}Relationship")
    rel.set("Id", new_id)
    rel.set("Type", REL_DIAGRAM_DATA)
    rel.set("Target", f"../diagrams/data{slide_no}.xml")
    data[rels_part] = etree.tostring(rroot, xml_declaration=True, encoding="UTF-8", standalone=True)
    data[diagram_part] = diagram_xml

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n2, d in data.items():
            zout.writestr(n2, d)
    shutil.move(tmp, path)
    return path


def write_encrypted_stub(path: str | Path) -> str:
    """写一个 OLE 复合文档头的文件，模拟加密 pptx。"""
    Path(path).write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    return str(path)


def write_corrupt_stub(path: str | Path) -> str:
    Path(path).write_bytes(b"this is not a valid zip or pptx file at all")
    return str(path)
