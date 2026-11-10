# -*- coding: utf-8 -*-
r"""把一个「PPT Doctor Material v1」素材包 PPTX 打成「团队素材包」组件。

    uv run --frozen python tools/build_materials_component.py <素材包.pptx> --version 1.0.0
    （默认输出到 artifacts/materials-component/：component.json + component.zip）

产物格式与文字识别 / 公式组件相同，客户端用同一套两层 sha256 校验：
    component.zip   只含一个文件 team-materials.pptx（即素材包原件）
    component.json  {"version", "kind": "materials", "items", "categories",
                     "files": {"team-materials.pptx": {"hash", "size"}},
                     "archive": {"name": "component.zip", "hash", "size"}}

发布（田哥审完内容后自己执行；新版本另开新标签，旧标签不动）：
    gh release create materials-v1.0.0 artifacts/materials-component/component.json \
        artifacts/materials-component/component.zip --title "团队素材包 v1.0.0" \
        --notes "PPT Doctor 团队素材包（可选组件）" --latest=false

打包前不开 PowerPoint，只读 PPTX 里的备注 XML 做检查：每页都有素材信息、id 不重复、
名称非空，不含宏 / 嵌入程序 / 外链文件（与客户端导入前的检查一致）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder.materials import MARKER, MAX_PACK_ITEMS, validate_pptx  # noqa: E402
from pptx_finder.team_materials import PACK_FILE  # noqa: E402

_NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
       "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rels(z: zipfile.ZipFile, part: str) -> dict:
    folder, name = part.rsplit("/", 1)
    path = f"{folder}/_rels/{name}.rels"
    if path not in z.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(z.read(path)).iter(_REL):
        target = rel.get("Target", "")
        if not target.startswith("/"):
            target = str(Path(folder, target).as_posix())
            parts = []
            for seg in target.split("/"):
                if seg == "..":
                    parts.pop()
                elif seg and seg != ".":
                    parts.append(seg)
            target = "/".join(parts)
        out[rel.get("Id")] = (rel.get("Type", ""), target.lstrip("/"))
    return out


def read_pack(path: Path) -> list[dict]:
    """按放映顺序读出每页备注里的素材信息；不合格直接报错。"""
    validate_pptx(path)
    items = []
    with zipfile.ZipFile(path) as z:
        pres_rels = _rels(z, "ppt/presentation.xml")
        order = [pres_rels[s.get(f"{{{_NS['r']}}}id")][1]
                 for s in ET.fromstring(z.read("ppt/presentation.xml")).iterfind("p:sldIdLst/p:sldId", _NS)]
        if not 1 <= len(order) <= MAX_PACK_ITEMS:
            raise SystemExit(f"[!] 素材包需要 1–{MAX_PACK_ITEMS} 页，实际 {len(order)} 页")
        for page, slide in enumerate(order, 1):
            notes = [t for kind, t in _rels(z, slide).values() if kind.endswith("/notesSlide")]
            text = ""
            if notes:
                root = ET.fromstring(z.read(notes[0]))
                for sp in root.iter(f"{{{_NS['p']}}}sp"):
                    paras = ["".join(t.text or "" for t in p.iter(f"{{{_NS['a']}}}t"))
                             for p in sp.iter(f"{{{_NS['a']}}}p")]
                    candidate = "\n".join(paras)
                    if candidate.startswith(MARKER):
                        text = candidate
                        break
            if not text:
                raise SystemExit(f"[!] 第 {page} 页备注没有「{MARKER.strip()}」素材信息")
            data = json.loads(text[len(MARKER):])
            if not isinstance(data.get("id"), str) or not data["id"].strip():
                raise SystemExit(f"[!] 第 {page} 页素材缺少 id")
            if not isinstance(data.get("name"), str) or not data["name"].strip():
                raise SystemExit(f"[!] 第 {page} 页素材缺少名称")
            if not isinstance(data.get("category", ""), str):
                raise SystemExit(f"[!] 第 {page} 页素材分类格式不对")
            items.append(data)
    dup = [k for k, n in Counter(x["id"] for x in items).items() if n > 1]
    if dup:
        raise SystemExit(f"[!] 素材 id 重复：{dup[:5]}")
    return items


def build(pack: Path, version: str, out_dir: Path) -> dict:
    if not _VERSION.fullmatch(version):
        raise SystemExit("[!] 版本号要写成 X.Y.Z，例如 1.0.0")
    items = read_pack(pack)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "component.zip"
    part = archive.with_name(archive.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(pack, PACK_FILE)
    part.replace(archive)
    manifest = {
        "version": version,
        "kind": "materials",
        "items": len(items),
        "categories": dict(Counter(x.get("category", "") or "未分类" for x in items).most_common()),
        "files": {PACK_FILE: {"hash": sha256_file(pack), "size": pack.stat().st_size}},
        "archive": {"name": archive.name, "hash": sha256_file(archive), "size": archive.stat().st_size},
    }
    (out_dir / "component.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    return manifest


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="打包团队素材包组件")
    ap.add_argument("pack", type=Path, help="PPT Doctor Material v1 格式的素材包 PPTX")
    ap.add_argument("--version", required=True, help="组件版本号 X.Y.Z")
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "materials-component")
    args = ap.parse_args(argv)
    manifest = build(args.pack, args.version, args.out)
    print(f"[ok] 团队素材包 v{manifest['version']}：{manifest['items']} 个素材，"
          f"{len(manifest['categories'])} 个分类")
    for name, n in manifest["categories"].items():
        print(f"     {n:4d}  {name}")
    print(f"     下载体积 {manifest['archive']['size'] / 1048576:.2f} MB → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
