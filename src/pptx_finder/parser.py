"""解析 .pptx：直读 zip+XML。

关键正确性点（见 spec §3）：
- 页序按 ppt/presentation.xml 的 <p:sldIdLst> 顺序 + presentation.xml.rels 映射还原，
  绝不按 slideN.xml 的文件名 N 排序（用户重排/删页后会全错）。
- 抓正文(slideN.xml) + 备注(notesSlideN.xml) + SmartArt(diagrams/dataN.xml)。
- 加密(OLE 复合文档) / 损坏 不抛异常，置 status，保证批量索引不中断。
"""
from __future__ import annotations

import logging
import posixpath
import zipfile

from lxml import etree

from .config import ext_path
from .models import ParsedDeck, SlidePage

log = logging.getLogger(__name__)

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

A_T = f"{{{A_NS}}}t"
P_SLDIDLST = f"{{{P_NS}}}sldIdLst"
P_SLDID = f"{{{P_NS}}}sldId"
R_ID = f"{{{R_NS}}}id"

REL_NOTES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
)
REL_DIAGRAM_DATA = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData"
)

OLE_MAGIC = b"\xd0\xcf\x11\xe0"


def _texts(xml_bytes: bytes) -> list[str]:
    """取出 XML 中所有 <a:t> 文本（去空白）。"""
    if not xml_bytes:
        return []
    root = etree.fromstring(xml_bytes)
    return [el.text for el in root.iter(A_T) if el.text and el.text.strip()]


def _rels_path(part_path: str) -> str:
    d = posixpath.dirname(part_path)
    b = posixpath.basename(part_path)
    return posixpath.join(d, "_rels", b + ".rels")


def _resolve(base_part: str, target: str) -> str:
    """rel target（相对 base_part 目录）→ zip 内绝对 part 路径。"""
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = posixpath.dirname(base_part)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _read_rels(zf: zipfile.ZipFile, part_path: str) -> dict[str, tuple[str, str]]:
    """读 part 的 .rels → {rId: (type, resolved_target)}。无则空 dict。"""
    out: dict[str, tuple[str, str]] = {}
    try:
        data = zf.read(_rels_path(part_path))
    except KeyError:
        return out
    root = etree.fromstring(data)
    for rel in root:
        rid = rel.get("Id")
        rtype = rel.get("Type")
        target = rel.get("Target")
        if rel.get("TargetMode") == "External" or not rid or not target:
            continue
        out[rid] = (rtype, _resolve(part_path, target))
    return out


def parse_pptx(path: str) -> ParsedDeck:
    """解析一个 .pptx，返回 ParsedDeck。任何异常都转成 status 而非抛出。"""
    deck = ParsedDeck(path=path)
    try:
        with open(ext_path(path), "rb") as f:
            head = f.read(8)
    except OSError as e:
        deck.status = "error"
        deck.error = f"open failed: {e}"
        return deck

    if head[:4] == OLE_MAGIC:
        deck.status = "encrypted"
        return deck

    try:
        with zipfile.ZipFile(ext_path(path)) as zf:
            return _parse_zip(zf, deck)
    except zipfile.BadZipFile:
        deck.status = "error"
        deck.error = "bad zip / not a pptx"
        return deck
    except Exception as e:  # noqa: BLE001 兜底：单文件失败不能中断批量索引
        deck.status = "error"
        deck.error = f"{type(e).__name__}: {e}"
        return deck


def _parse_zip(zf: zipfile.ZipFile, deck: ParsedDeck) -> ParsedDeck:
    pres = "ppt/presentation.xml"
    try:
        pres_xml = zf.read(pres)
    except KeyError:
        deck.status = "error"
        deck.error = "no presentation.xml"
        return deck

    pres_root = etree.fromstring(pres_xml)
    lst = pres_root.find(P_SLDIDLST)
    if lst is None:
        deck.page_count = 0
        return deck

    rels = _read_rels(zf, pres)
    order_rids = [sld.get(R_ID) for sld in lst.findall(P_SLDID)]

    pages: list[SlidePage] = []
    for i, rid in enumerate(order_rids, start=1):
        slide_part = rels[rid][1] if rid and rid in rels else None
        if slide_part:
            try:
                pages.append(_parse_slide(zf, slide_part, i))
            except Exception as e:  # noqa: BLE001 单页失败不毁整份
                log.warning("slide parse failed page=%s part=%s: %s", i, slide_part, e)
                pages.append(SlidePage(page_no=i))
        else:
            pages.append(SlidePage(page_no=i))

    deck.pages = pages
    deck.page_count = len(pages)
    return deck


def _parse_slide(zf: zipfile.ZipFile, slide_part: str, page_no: int) -> SlidePage:
    body = "\n".join(_texts(zf.read(slide_part)))
    notes_parts: list[str] = []
    smart_parts: list[str] = []

    for rtype, target in _read_rels(zf, slide_part).values():
        if rtype == REL_NOTES:
            try:
                notes_parts.extend(_texts(zf.read(target)))
            except KeyError:
                pass
        elif rtype == REL_DIAGRAM_DATA:
            try:
                smart_parts.extend(_texts(zf.read(target)))
            except KeyError:
                pass

    return SlidePage(
        page_no=page_no,
        body=body,
        notes="\n".join(notes_parts),
        smartart="\n".join(smart_parts),
    )
