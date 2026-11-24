# -*- coding: utf-8 -*-
r"""把「PPT Doctor Material v1」素材包 PPTX 脱敏、过发布硬闸门，打成团队素材组件 + 主包内置精选。

    uv run --frozen python tools/build_materials_component.py <素材包.pptx> --version 1.2.0 \
        --out <完整组件输出目录> [--core-out assets/materials-core] [--render]

流水线（任一硬闸门不为 0 就以退出码 1 失败，不出包）：
  1. 脱敏（纯 XML，不开 PowerPoint）：
     - 删除版权待确认图片（BLOCKED_IMAGES）；只剩这张图的素材页整页剔除；
     - 页面文字里的示意数字（带 % / dB / Mbps 的数，以及 3 位以上的连续数字）改成 XX；
     - 「团队母题·复刻」分类改名「团队母题」；
     - docProps 重写为最小内容：作者、最后修改者、标题、关键词、描述、公司、经理全部清空。
  2. 扫描（scan）：个人标识、黑名单词、待确认图片、示意数字、外链 / 超链接 / 宏 / 嵌入 / 批注 /
     customXml，逐项计数；报告只写计数和页码，不写原文。
  3. 缩略图（--render，需要本机 PowerPoint）：只读、无窗口打开临时副本，整页按 3 倍（4 px/pt）
     分别在白底、黑底各导出一次，由两张图的差求出透明度，再裁到素材本身的范围。
     完整库用 3x 图（封顶 648×486），内置精选用 2x 图（封顶 336×256），统一存成 PNG 调色板。
     不加 --render 时不生成缩略图（界面画名称占位），并在 audit.json 里如实记录。
  4. 打包：
     component.zip  = team-materials.pptx + index.json + thumbs.zip
     component.json = {"version", "kind", "items", "categories", "files": {3 个文件的 hash/size}, "archive"}
     audit.json     = 扫描计数 + 与发布物绑定的 pptx / component.zip 两层 sha256（只留档，不进组件包）
     --core-out     = 内置精选 3 个文件（精选 pptx、index.json、thumbs.zip），用于主安装包

不发布、不上传：发布由田哥审完内容后自己走发布流程。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder.materials import (  # noqa: E402
    INDEX_FILE, INDEX_SCHEMA, PACK_PPTX, THUMBS_FILE, MAX_PACK_ITEMS, category_group,
    part_rels, read_pack_index, slide_order, validate_pptx,
)

_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_FIXED_TIME = (2026, 1, 1, 0, 0, 0)       # zip 内时间戳固定，同样输入 → 同样 sha256

#: 来源已确认可用的图片（sha256 → 说明）。新图片必须先登记来源才能出包。
CONFIRMED_IMAGES = {
    "fd2353c61b3dd5700d2e2a0f769daa3247b62d46d60c3741a42710876da91104": "绿色立体下行箭头",
    "a79aac6f4cdd76050c6ff5ea8a9884fe0ff71cb64b139ef54bb8db56e8a1f323": "公式 b*m",
    "e0544163e992fd4b035cb45ddd5b3c3b98c1d675acd1512687f3ddf8826c05ce": "SINR 公式",
    "1e2129a4f2243fe35a632e58ad45fdff92d6d584256d684b9494e268ebe27c77": "SINR 小区内外公式",
    "d36db72e956a5708345ed404001c9bbb170cbe891bc3c90eb4812df3aaa59c17": "示例文字公式占位",
    "a60448100cd211ce073317ec898ce04538d3a0824ead25b0a279374ea2ccbf36": "红叉",
    "b9182dc4991aa525058bba621307adea582cfcbeb4ce7a53e5a3cd194b6b5a27": "红色弯箭头",
    "be053ff5bed3cc9f50ecd438abd791684c81d7a53495949cd4411da727988f9d": "黄色渐隐下行箭头",
}
#: 来历待确认、确认前不得出包的图片：用到它们的图片对象删除，整页只剩它的素材剔除。
BLOCKED_IMAGES = {
    "8d6e1a1e2c6e82f801a8e5eaa76238c5dfe3160c1b68b54155787548a327651d": "三色立体循环箭头",
    "0660a1d83d8b091660455e149d5f1d01f2cfad8b999ea32a91f066c736997c04": "商务人物头像",
    "87d94f05301e947eda53fe71e03fc97792b5b0f33a54058818a6fe4cec6279ce": "立体高楼",
}
CATEGORY_RENAMES = {"团队母题·复刻": "团队母题"}

# ---- 文字规则 ----
NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)(\s*(?:%|％|dB|Mbps))", re.I)
BIG_NUM = re.compile(r"\d{3,}")
BLACKLIST = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "url": re.compile(r"https?://|www\.", re.I),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "staff_id": re.compile(r"(?<![0-9A-Za-z])\d{8}(?![0-9A-Za-z])"),
    "version_code": re.compile(r"V\d{3}R\d{3}", re.I),
    "company": re.compile(r"华为|huawei|海思|hisilicon|中国移动|中国联通|中国电信|运营商|"
                          r"内部资料|内部公开|机密|绝密|confidential", re.I),
}
A_URI = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _at_pattern(xml: str):
    """当前文档里所有绑定到 DrawingML 的前缀（含默认命名空间）对应的 <t> 文本元素。

    只用于脱敏替换；最终判定由 slide_text() 的 namespace 解析扫描兜底（漏改就判失败）。
    """
    prefixes = set(re.findall(r'xmlns:([A-Za-z_][\w.-]*)="%s"' % re.escape(A_URI), xml))
    alts = [re.escape(x) + ":" for x in sorted(prefixes)]
    if re.search(r'xmlns="%s"' % re.escape(A_URI), xml):
        alts.append("")
    if not alts:
        return None
    pre = "(?:" + "|".join(alts) + ")"
    return re.compile(r"(<%st(?:\s[^>]*)?>)([^<]*)(</%st>)" % (pre, pre))
_SHAPE_TAG = re.compile(r"<p:(?:sp|pic|grpSp|cxnSp|graphicFrame)[\s>]")
_PIC = re.compile(r"<p:pic>.*?</p:pic>", re.S)
_EMBED = re.compile(r'r:embed="(rId\d+)"')
_SLIDE_PART = re.compile(r"^ppt/slides/slide\d+\.xml$")
_NOTES_PART = re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$")
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"

CORE_XML = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:modified>'
            '</cp:coreProperties>')
APP_XML = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
           'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
           '<Application>Microsoft Office PowerPoint</Application><Slides>{n}</Slides><Notes>{n}</Notes>'
           '<HiddenSlides>0</HiddenSlides><AppVersion>16.0000</AppVersion></Properties>')
#: docProps 里只要非空就算个人标识的字段
CORE_PERSONAL = ("creator", "lastModifiedBy", "title", "subject", "keywords", "description",
                 "category", "contentStatus", "identifier")
APP_PERSONAL = ("Company", "Manager", "HyperlinkBase", "TitlesOfParts")


class GateError(SystemExit):
    """硬闸门不过：退出码 1。"""
    def __init__(self, message: str):
        super().__init__(1)
        self.message = message


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- zip 读写
def load_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def write_parts(parts: dict[str, bytes], path: Path):
    order = ["[Content_Types].xml"] + [n for n in parts if n != "[Content_Types].xml"]
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in order:
            info = zipfile.ZipInfo(name, _FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, parts[name], compresslevel=9)
    tmp.replace(path)


def _rels_name(part: str) -> str:
    folder, name = part.rsplit("/", 1) if "/" in part else ("", part)
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"


def _resolve(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parts = source_part.split("/")[:-1]
    for seg in target.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


def _rels_of(parts: dict, part: str) -> list[tuple[str, str, str, str]]:
    """[(rId, 类型, 目标零件, TargetMode)]"""
    data = parts.get(_rels_name(part))
    if data is None:
        return []
    out = []
    for rel in ET.fromstring(data).iter(_REL):
        mode = rel.get("TargetMode", "")
        target = rel.get("Target", "")
        out.append((rel.get("Id", ""), rel.get("Type", ""),
                    target if mode == "External" else _resolve(part, target), mode))
    return out


def _remove_rel_ids(parts: dict, part: str, ids: set[str]):
    name = _rels_name(part)
    if name not in parts or not ids:
        return
    text = parts[name].decode("utf-8")
    for rid in ids:
        text = re.sub(r'<Relationship\b[^>]*\bId="%s"[^>]*/>' % re.escape(rid), "", text)
    parts[name] = text.encode("utf-8")


def prune_unreachable(parts: dict):
    """从包根关系出发走一遍，删掉不再被引用的零件（剔除页的备注、图片等），并同步内容类型表。"""
    seen, stack = set(), [""]
    while stack:
        part = stack.pop()
        rels = (_rels_of(parts, part) if part else
                [(r.get("Id"), r.get("Type"), r.get("Target", "").lstrip("/"), r.get("TargetMode", ""))
                 for r in ET.fromstring(parts["_rels/.rels"]).iter(_REL)])
        for _, _, target, mode in rels:
            if mode != "External" and target in parts and target not in seen:
                seen.add(target)
                stack.append(target)
    for name in list(parts):
        if name == "[Content_Types].xml" or name.endswith(".rels"):
            continue
        if name not in seen:
            del parts[name]
    for name in list(parts):
        if name.endswith(".rels") and name != "_rels/.rels":
            folder, rels_file = name.rsplit("/_rels/", 1)
            if f"{folder}/{rels_file[:-5]}" not in parts:
                del parts[name]
    ct = parts["[Content_Types].xml"].decode("utf-8")

    def keep_override(m):
        return m.group(0) if m.group(1).lstrip("/") in parts else ""
    parts["[Content_Types].xml"] = re.sub(r'<Override\b[^>]*PartName="([^"]+)"[^>]*/>',
                                          keep_override, ct).encode("utf-8")


def drop_slides(parts: dict, slide_parts: set[str]):
    """从演示文稿里摘掉这些页（sldId + 关系），其余引用由 prune_unreachable 清理。"""
    if not slide_parts:
        return
    pres_rels = {target: rid for rid, _, target, _ in _rels_of(parts, "ppt/presentation.xml")}
    rids = {pres_rels[s] for s in slide_parts if s in pres_rels}
    pres = parts["ppt/presentation.xml"].decode("utf-8")
    for rid in rids:
        pres = re.sub(r'<p:sldId\b[^>]*\br:id="%s"[^>]*/>' % re.escape(rid), "", pres)
    parts["ppt/presentation.xml"] = pres.encode("utf-8")
    _remove_rel_ids(parts, "ppt/presentation.xml", rids)
    prune_unreachable(parts)


def _order(parts: dict) -> list[str]:
    with zipfile.ZipFile(_to_zip(parts)) as z:
        return slide_order(z)


def _to_zip(parts: dict) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    buf.seek(0)
    return buf


def slide_text(xml) -> list[str]:
    """一页里按段落拼好的文字：按 namespace URI 解析（不认前缀名），跨 run 拆开的数字也能扫到。"""
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out = []
    for para in root.iter(f"{{{A_URI}}}p"):
        text = "".join(t.text or "" for t in para.iter(f"{{{A_URI}}}t"))
        if text:
            out.append(text)
    return out


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&amp;", "&"))


def numeric_hits(text: str) -> list[str]:
    hits = [m.group(0) for m in NUM_UNIT.finditer(text)]
    rest = NUM_UNIT.sub(" ", text)
    return hits + BIG_NUM.findall(rest)


def redact_numbers(text: str) -> str:
    return BIG_NUM.sub("XX", NUM_UNIT.sub(lambda m: "XX" + m.group(2), text))


# ---------------------------------------------------------------- 脱敏
def sanitize(src: Path, dst: Path, *, allow_numbers=()) -> dict:
    validate_pptx(src)
    parts = load_parts(src)
    before = read_pack_index(src)
    order = _order(parts)
    page_of = {slide: i for i, slide in enumerate(order, 1)}
    report = {"removed_items": [], "pictures_removed": 0, "numbers_redacted": 0,
              "categories_renamed": 0, "docprops_rewritten": True}
    allow = set(allow_numbers)
    media_sha = {n: sha256_bytes(d) for n, d in parts.items() if n.startswith("ppt/media/")}
    empty = set()
    for slide in order:
        xml = parts[slide].decode("utf-8")
        blocked_rids = {rid for rid, kind, target, _ in _rels_of(parts, slide)
                        if kind.endswith("/image") and media_sha.get(target) in BLOCKED_IMAGES}
        if blocked_rids:
            def drop_pic(m):
                if set(_EMBED.findall(m.group(0))) & blocked_rids:
                    report["pictures_removed"] += 1
                    return ""
                return m.group(0)
            xml = _PIC.sub(drop_pic, xml)
            still = set(_EMBED.findall(xml))
            _remove_rel_ids(parts, slide, blocked_rids - still)
        # 示意数字 → XX（只改页面文字，备注里的素材 id 不动）
        def fix_run(m):
            text = m.group(2)
            if any(h not in allow for h in numeric_hits(_unescape(text))):
                new = redact_numbers(text)
                if new != text:
                    report["numbers_redacted"] += len(numeric_hits(_unescape(text)))
                    return m.group(1) + new + m.group(3)
            return m.group(0)
        at = _at_pattern(xml)
        if at is not None:
            xml = at.sub(fix_run, xml)
        parts[slide] = xml.encode("utf-8")
        if not _SHAPE_TAG.search(xml.split("<p:spTree>", 1)[-1].split("</p:nvGrpSpPr>", 1)[-1]):
            empty.add(slide)
    for slide in sorted(empty, key=page_of.get):
        item = before[page_of[slide] - 1]
        report["removed_items"].append({"page": page_of[slide], "id": item["id"], "name": item["name"],
                                        "reason": "只含版权待确认图片"})
    drop_slides(parts, empty)
    for name in list(parts):
        if _NOTES_PART.match(name):
            text = parts[name].decode("utf-8")
            for old, new in CATEGORY_RENAMES.items():
                if old in text:
                    report["categories_renamed"] += text.count(old)
                    text = text.replace(old, new)
            parts[name] = text.encode("utf-8")
    _rewrite_docprops(parts)
    for name in [n for n in parts if n in ("ppt/commentAuthors.xml", "ppt/authors.xml")]:
        pass   # 留给扫描判失败：批注作者属于个人信息，不静默删
    write_parts(parts, dst)
    return report


def _rewrite_docprops(parts: dict):
    n = len(_order(parts))
    parts["docProps/core.xml"] = CORE_XML.encode("utf-8")
    parts["docProps/app.xml"] = APP_XML.format(n=n).encode("utf-8")
    parts.pop("docProps/thumbnail.jpeg", None)   # 缩略图是第一页截图，留着也会带出删改前的内容
    root_rels = parts["_rels/.rels"].decode("utf-8")
    root_rels = re.sub(r'<Relationship\b[^>]*Target="/?docProps/thumbnail\.jpeg"[^>]*/>', "", root_rels)
    parts["_rels/.rels"] = root_rels.encode("utf-8")


def subset(src: Path, dst: Path, keep_ids: set[str]):
    """只留 keep_ids 这些素材（主包内置精选）。"""
    parts = load_parts(src)
    items = read_pack_index(src)
    order = _order(parts)
    drop = {order[x["page"] - 1] for x in items if x["id"] not in keep_ids}
    drop_slides(parts, drop)
    _rewrite_docprops(parts)
    write_parts(parts, dst)


# ---------------------------------------------------------------- 扫描与硬闸门
def scan(path: Path, *, allow_numbers=(), approved_numbers=()) -> dict:
    """逐项计数。报告只写计数、页码和图片说明，不写任何命中原文。"""
    allow = set(allow_numbers) | set(approved_numbers)
    parts = load_parts(path)
    items = read_pack_index(path)
    order = _order(parts)
    counts = Counter()
    pages = {k: set() for k in ("numbers", *BLACKLIST)}
    # docProps
    for name, fields, ns in (("docProps/core.xml", CORE_PERSONAL, None),
                             ("docProps/app.xml", APP_PERSONAL, None)):
        if name not in parts:
            continue
        root = ET.fromstring(parts[name])
        for el in root.iter():
            local = el.tag.rsplit("}", 1)[-1]
            if local in fields and ("".join(el.itertext()).strip()):
                counts["docprops_personal"] += 1
        if BLACKLIST["email"].search(parts[name].decode("utf-8", "replace")):
            counts["docprops_personal"] += 1
    # 文字：页面 + 母版 / 版式 + 备注里的名称和分类
    true_hits, approved = 0, 0
    for page, slide in enumerate(order, 1):
        for text in slide_text(parts[slide]):
            for hit in numeric_hits(text):
                true_hits += 1
                if hit in allow:
                    approved += 1
                else:
                    pages["numbers"].add(page)
            for key, rx in BLACKLIST.items():
                if rx.search(text):
                    counts[f"blacklist_{key}"] += 1
                    pages[key].add(page)
    for name, data in parts.items():
        if re.match(r"^ppt/(slideMasters|slideLayouts)/.*\.xml$", name):
            for text in slide_text(data):
                for key, rx in BLACKLIST.items():
                    if rx.search(text):
                        counts[f"blacklist_{key}"] += 1
    for x in items:
        for key, rx in BLACKLIST.items():
            if rx.search(f"{x['name']} {x['category']}"):
                counts[f"blacklist_{key}"] += 1
                pages[key].add(x["page"])
    # 图片
    images = []
    for name, data in parts.items():
        if name.startswith("ppt/media/"):
            digest = sha256_bytes(data)
            if digest in BLOCKED_IMAGES:
                counts["images_copyright_pending"] += 1
                images.append({"part": name, "status": "blocked", "label": BLOCKED_IMAGES[digest]})
            elif digest in CONFIRMED_IMAGES:
                images.append({"part": name, "status": "confirmed", "label": CONFIRMED_IMAGES[digest]})
            else:
                counts["images_copyright_pending"] += 1
                images.append({"part": name, "status": "unregistered", "sha256": digest})
    # 结构
    for name, data in parts.items():
        low = name.lower()
        if "vbaproject" in low or low.endswith(".bin") and "activex" in low:
            counts["macros"] += 1
        if low.startswith("ppt/embeddings/") or "oleobject" in low:
            counts["embeddings"] += 1
        if (low.startswith("ppt/comments/") or low in ("ppt/commentauthors.xml", "ppt/authors.xml")
                or "modernComment" in name):
            counts["comments"] += 1
        if low.startswith("customxml/"):
            counts["custom_xml"] += 1
        if low.endswith("people.xml"):
            counts["people"] += 1
        if name.endswith(".rels"):
            for rel in ET.fromstring(data).iter(_REL):
                if rel.get("TargetMode") == "External":
                    counts["external_rels"] += 1
                if rel.get("Type", "").endswith("/hyperlink"):
                    counts["hyperlinks"] += 1
    report = {
        "items": len(items),
        "categories": dict(Counter(x["category"] or "未分类" for x in items).most_common()),
        "docprops_personal": counts["docprops_personal"],
        "blacklist_hits": {k: counts[f"blacklist_{k}"] for k in BLACKLIST},
        "blacklist_pages": {k: sorted(v) for k, v in pages.items() if k != "numbers" and v},
        "numeric_hits": {"true_hits": true_hits, "approved_after_redaction": approved,
                         "pending": true_hits - approved, "pending_pages": sorted(pages["numbers"])},
        "images": images,
        "images_copyright_pending": counts["images_copyright_pending"],
        "structure": {k: counts[k] for k in ("external_rels", "hyperlinks", "macros", "embeddings",
                                              "comments", "custom_xml", "people")},
    }
    return report


def gate_failures(report: dict) -> list[str]:
    fails = []
    if report["docprops_personal"]:
        fails.append(f"docProps 个人标识 {report['docprops_personal']} 处")
    for key, n in report["blacklist_hits"].items():
        if n:
            fails.append(f"黑名单「{key}」命中 {n} 处（页 {report['blacklist_pages'].get(key, [])}）")
    if report["images_copyright_pending"]:
        fails.append(f"版权待确认 / 未登记图片 {report['images_copyright_pending']} 张")
    if report["numeric_hits"]["pending"]:
        fails.append(f"未放行示意数字 {report['numeric_hits']['pending']} 条"
                     f"（页 {report['numeric_hits']['pending_pages']}）")
    for key, n in report["structure"].items():
        if n:
            fails.append(f"{key} {n} 处")
    return fails


def enforce_gate(path: Path, label: str, **kw) -> dict:
    report = scan(path, **kw)
    fails = gate_failures(report)
    if fails:
        raise GateError(f"[!] {label} 未过发布硬闸门，拒绝出包：\n    " + "\n    ".join(fails))
    return report


# ---------------------------------------------------------------- 缩略图
PX_PER_PT_3X = 4          # 1x = 96 dpi = 4/3 px/pt；3x = 4 px/pt
FULL_CAP = (648, 486)
CORE_CAP = (336, 256)


def _matte(white_png: Path, black_png: Path):
    """白底、黑底两次导出 → 带透明度的 RGBA，裁到不透明范围。"""
    from PIL import Image, ImageChops, ImageMath
    white = Image.open(white_png).convert("RGB")
    black = Image.open(black_png).convert("RGB")
    alpha = ImageChops.subtract(white, black).convert("L").point(lambda v: 255 - v)
    bands = [ImageMath.unsafe_eval("convert(min(b*255/max(a,1),255),'L')", b=band, a=alpha)
             for band in black.split()]
    image = Image.merge("RGBA", (*bands, alpha))
    box = alpha.point(lambda v: 255 if v > 3 else 0).getbbox()
    return image.crop(box) if box else image


def render_thumbs(pptx: Path, out_dir: Path, *, log=print) -> dict[str, Path]:
    """用本机 PowerPoint 渲染每页 3x 透明 PNG：只开临时副本（只读、无窗口），只关自己开的。"""
    from pptx_finder import ppt_transfer
    items = read_pack_index(pptx)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    with tempfile.TemporaryDirectory(prefix="mat-render-") as tmp:
        snapshot = Path(tmp) / "snapshot.pptx"
        shutil.copyfile(pptx, snapshot)
        with ppt_transfer.temporary_presentations() as session:
            pres = session.open(snapshot)
            if int(pres.Slides.Count) != len(items):
                raise SystemExit("[!] PowerPoint 读到的页数与索引不一致")
            w = int(float(pres.PageSetup.SlideWidth) * PX_PER_PT_3X)
            h = int(float(pres.PageSetup.SlideHeight) * PX_PER_PT_3X)
            for item in items:
                slide = pres.Slides.Item(item["page"])
                slide.FollowMasterBackground = 0
                shots = []
                for tag, rgb in (("w", 0xFFFFFF), ("b", 0x000000)):
                    slide.Background.Fill.Solid()
                    slide.Background.Fill.ForeColor.RGB = rgb
                    shot = Path(tmp) / f"{item['page']}-{tag}.png"
                    slide.Export(str(shot), "PNG", w, h)
                    shots.append(shot)
                target = out_dir / f"{item['id']}.png"
                _matte(*shots).save(target)
                for shot in shots:
                    shot.unlink(missing_ok=True)
                result[item["id"]] = target
                if item["page"] % 50 == 0:
                    log(f"     渲染 {item['page']} / {len(items)}")
    return result


def encode_thumb(src: Path, *, scale: float, cap: tuple[int, int]) -> tuple[bytes, int, int]:
    """缩放（只缩不放）→ 封顶 → PNG 调色板（带透明度）。"""
    from PIL import Image
    image = Image.open(src).convert("RGBA")
    w, h = image.size
    factor = min(scale, cap[0] / w, cap[1] / h) if w and h else 1
    if factor < 1:
        image = image.resize((max(1, round(w * factor)), max(1, round(h * factor))), Image.LANCZOS)
    quant = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    buf = io.BytesIO()
    quant.save(buf, "PNG", optimize=True)
    return buf.getvalue(), image.width, image.height


def _kinds(pptx: Path) -> dict[str, str]:
    parts = load_parts(pptx)
    items = read_pack_index(pptx)
    order = _order(parts)
    out = {}
    for x in items:
        xml = parts[order[x["page"] - 1]].decode("utf-8")
        has_pic = "<p:pic>" in xml
        has_shape = bool(re.search(r"<p:(?:sp|cxnSp)[\s>]", xml))
        out[x["id"]] = "mixed" if has_pic and has_shape else "picture" if has_pic else "shape"
    return out


# ---------------------------------------------------------------- 打包
def write_pack_dir(pptx: Path, version: str, out_dir: Path, pngs: dict[str, Path] | None, *,
                   scale: float, cap: tuple[int, int]) -> dict:
    """out_dir 下写 team-materials.pptx + index.json + thumbs.zip（三件套）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    items = read_pack_index(pptx)
    kinds = _kinds(pptx)
    shutil.copyfile(pptx, out_dir / PACK_PPTX)
    index_items, thumbs = [], {}
    for x in items:
        entry = {"id": x["id"], "name": x["name"], "category": x["category"],
                 "group": category_group(x["category"]), "page": x["page"], "kind": kinds[x["id"]],
                 "w": 0, "h": 0, "thumb": ""}
        if pngs and x["id"] in pngs:
            data, w, h = encode_thumb(pngs[x["id"]], scale=scale, cap=cap)
            name = f"{x['id']}.png"
            thumbs[name] = data
            entry.update(w=w, h=h, thumb=name)
        index_items.append(entry)
    index = {"schema": INDEX_SCHEMA, "version": version, "items": index_items,
             "thumb_scale": scale if pngs else 0, "thumb_cap": list(cap) if pngs else []}
    (out_dir / INDEX_FILE).write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), "utf-8")
    tmp = out_dir / (THUMBS_FILE + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # PNG 已压缩，再压不省
        for name in sorted(thumbs):
            z.writestr(zipfile.ZipInfo(name, _FIXED_TIME), thumbs[name])
    tmp.replace(out_dir / THUMBS_FILE)
    return {"items": len(items), "thumbs": len(thumbs), "thumb_bytes": sum(map(len, thumbs.values()))}


def build_component(pack_dir: Path, version: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    names = (PACK_PPTX, INDEX_FILE, THUMBS_FILE)
    archive = out_dir / "component.zip"
    part = archive.with_name(archive.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in names:
            info = zipfile.ZipInfo(name, _FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (pack_dir / name).read_bytes(), compresslevel=9)
    part.replace(archive)
    items = read_pack_index(pack_dir / PACK_PPTX)
    manifest = {
        "version": version,
        "kind": "materials",
        "items": len(items),
        "categories": dict(Counter(x["category"] or "未分类" for x in items).most_common()),
        "files": {n: {"hash": sha256_file(pack_dir / n), "size": (pack_dir / n).stat().st_size} for n in names},
        "archive": {"name": archive.name, "hash": sha256_file(archive), "size": archive.stat().st_size},
    }
    (out_dir / "component.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    return manifest


def curated_ids(items: list[dict], ratio: float = 0.28) -> set[str]:
    """内置精选：零件 / 组合件每类取前 max(1, round(n×ratio)) 条，整页模板每类 1 条。"""
    by_cat: dict[str, list[dict]] = {}
    for x in items:
        by_cat.setdefault(x["category"], []).append(x)
    keep = set()
    for category, rows in by_cat.items():
        n = 1 if category_group(category) == "整页模板" else max(1, round(len(rows) * ratio))
        keep.update(x["id"] for x in rows[:n])
    return keep


# ---------------------------------------------------------------- 入口
def run(pack: Path, version: str, out: Path, *, core_out: Path | None = None, render: bool = False,
        thumbs_from: Path | None = None, allow_numbers=(), log=print) -> dict:
    if not _VERSION.fullmatch(version):
        raise SystemExit("[!] 版本号要写成 X.Y.Z，例如 1.2.0")
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        source_scan = scan(pack, allow_numbers=allow_numbers)
        clean = work / "sanitized.pptx"
        redaction = sanitize(pack, clean, allow_numbers=allow_numbers)
        full_scan = enforce_gate(clean, "完整素材包", allow_numbers=allow_numbers)
        log(f"[ok] 脱敏通过：{full_scan['items']} 条（剔除 {len(redaction['removed_items'])} 条），"
            f"示意数字改 XX {redaction['numbers_redacted']} 处")
        pngs = None
        if thumbs_from:
            pngs = {p.stem: p for p in Path(thumbs_from).glob("*.png")}
        elif render:
            log("[..] PowerPoint 渲染 3x 缩略图（只读副本、无窗口）")
            pngs = render_thumbs(clean, work / "png3x", log=log)
        full_dir = work / "full"
        full_stats = write_pack_dir(clean, version, full_dir, pngs, scale=1.0, cap=FULL_CAP)
        manifest = build_component(full_dir, version, out)
        core = None
        if core_out is not None:
            keep = curated_ids(read_pack_index(clean))
            core_pptx = work / "core.pptx"
            subset(clean, core_pptx, keep)
            core_scan = enforce_gate(core_pptx, "内置精选包", allow_numbers=allow_numbers)
            core_out.mkdir(parents=True, exist_ok=True)
            core_stats = write_pack_dir(core_pptx, version, core_out, pngs, scale=2 / 3, cap=CORE_CAP)
            (core_out / PACK_PPTX).replace(core_out / "team-materials-core.pptx")
            core = {"items": core_scan["items"], "categories": core_scan["categories"], **core_stats,
                    "files": {n: {"hash": sha256_file(core_out / n), "size": (core_out / n).stat().st_size}
                              for n in ("team-materials-core.pptx", INDEX_FILE, THUMBS_FILE)}}
            core["total_bytes"] = sum(f["size"] for f in core["files"].values())
        audit = {
            "schema": "ppt-doctor-materials-audit/1",
            "version": version,
            "release_gate": "passed",
            "bound_to": {"pptx_sha256": manifest["files"][PACK_PPTX]["hash"],
                         "component_zip_sha256": manifest["archive"]["hash"],
                         "component_zip_bytes": manifest["archive"]["size"]},
            "source": {"sha256": sha256_file(pack), "bytes": pack.stat().st_size,
                       "items": source_scan["items"],
                       "docprops_personal": source_scan["docprops_personal"],
                       "numeric_true_hits": source_scan["numeric_hits"]["true_hits"],
                       "numeric_pages": len(source_scan["numeric_hits"]["pending_pages"]),
                       "images_copyright_pending": source_scan["images_copyright_pending"]},
            "redaction": redaction,
            "scan_after": full_scan,
            "thumbnails": ({"method": "PowerPoint 整页 4 px/pt 白底黑底两次导出求透明度，裁到素材范围",
                            "full": "3x 封顶 648×486 PNG 调色板", "core": "2x 封顶 336×256 PNG 调色板",
                            **full_stats} if pngs else {"method": "未渲染（界面画名称占位）", **full_stats}),
            "core": core,
        }
        (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=1), "utf-8")
        return audit
    finally:
        if not render or thumbs_from:
            shutil.rmtree(work, ignore_errors=True)
        else:   # 渲染很慢：留 3x 原图便于复用（--thumbs-from），其余中间件删掉
            for name in ("sanitized.pptx", "core.pptx", "full"):
                target = work / name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="脱敏并打包团队素材组件（含发布硬闸门）")
    ap.add_argument("pack", type=Path, help="PPT Doctor Material v1 格式的素材包 PPTX")
    ap.add_argument("--version", required=True, help="组件版本号 X.Y.Z")
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "materials-component")
    ap.add_argument("--core-out", type=Path, help="同时生成主包内置精选（写 3 个文件到这个目录）")
    ap.add_argument("--render", action="store_true", help="用本机 PowerPoint 渲染缩略图")
    ap.add_argument("--thumbs-from", type=Path, help="复用上次渲染的 3x 原图目录（<id>.png）")
    ap.add_argument("--allow-numbers", nargs="*", default=(), help="人工放行的示意数字（原样）")
    ap.add_argument("--scan-only", action="store_true", help="只扫描、打印计数，不出包")
    args = ap.parse_args(argv)
    try:
        if args.scan_only:
            report = scan(args.pack, allow_numbers=args.allow_numbers)
            print(json.dumps({k: v for k, v in report.items() if k != "images"}, ensure_ascii=False, indent=1))
            fails = gate_failures(report)
            print("[!] " + "；".join(fails) if fails else "[ok] 发布硬闸门全部为 0")
            return 1 if fails else 0
        audit = run(args.pack, args.version, args.out, core_out=args.core_out, render=args.render,
                    thumbs_from=args.thumbs_from, allow_numbers=args.allow_numbers)
    except GateError as exc:
        print(exc.message)
        return 1
    b = audit["bound_to"]
    print(f"[ok] 团队素材 v{audit['version']}：{audit['scan_after']['items']} 条，"
          f"component.zip {b['component_zip_bytes']:,} B → {args.out}")
    if audit["core"]:
        print(f"[ok] 内置精选 {audit['core']['items']} 条，3 个文件共 {audit['core']['total_bytes']:,} B → {args.core_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
