"""PPTX slimming helpers.

This module only reads/writes the Open XML package. It does not automate
PowerPoint, so it is safe to run while the user is editing a deck.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from lxml import etree

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
REL_SLIDE_LAYOUT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
REL_SLIDE_MASTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"

_JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
_JUNK_PREFIXES = ("__macosx/",)
_MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf",
    ".mp4", ".mov", ".wmv", ".avi", ".mp3", ".wav", ".m4a",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf"}
_VIDEO_EXTS = {".mp4", ".mov", ".wmv", ".avi"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a"}


@dataclass(frozen=True)
class SizeBucket:
    label: str
    count: int
    compressed_bytes: int
    raw_bytes: int


@dataclass(frozen=True)
class DuplicateMediaGroup:
    keep_part: str
    duplicate_parts: tuple[str, ...]
    compressed_reclaimable: int
    raw_reclaimable: int

    @property
    def copies(self) -> int:
        return 1 + len(self.duplicate_parts)


@dataclass(frozen=True)
class SlimReport:
    path: str
    original_size: int
    package_parts: int
    package_compressed_bytes: int
    buckets: tuple[SizeBucket, ...]
    duplicate_media_groups: tuple[DuplicateMediaGroup, ...]
    duplicate_media_reclaimable: int
    orphan_parts: tuple[str, ...]
    orphan_reclaimable: int
    junk_parts: tuple[str, ...]
    junk_reclaimable: int
    unused_layouts: tuple[str, ...]
    unused_masters: tuple[str, ...]
    high_risk_notes: tuple[str, ...] = field(default_factory=tuple)
    reachable_complete: bool = True

    @property
    def low_risk_reclaimable(self) -> int:
        return self.duplicate_media_reclaimable + self.orphan_reclaimable + self.junk_reclaimable


@dataclass(frozen=True)
class SlimOptions:
    repackage: bool = True
    remove_junk: bool = True
    remove_orphans: bool = True
    dedupe_media: bool = True


@dataclass(frozen=True)
class SlimResult:
    ok: bool
    source_path: str
    output_path: str
    original_size: int
    slim_size: int
    saved_bytes: int
    removed_parts: tuple[str, ...]
    deduped_media: int
    actions: tuple[str, ...]
    error: str = ""


def human_bytes(n: int) -> str:
    f = float(max(0, int(n or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{int(f)} B" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def default_output_path(path: str) -> str:
    p = Path(path)
    stem = p.stem
    parent = p.parent
    candidate = parent / f"{stem}.slim{p.suffix or '.pptx'}"
    if not candidate.exists():
        return str(candidate)
    i = 2
    while True:
        candidate = parent / f"{stem}.slim-{i}{p.suffix or '.pptx'}"
        if not candidate.exists():
            return str(candidate)
        i += 1


def analyze_pptx(path: str) -> SlimReport:
    path = os.path.abspath(path)
    if not zipfile.is_zipfile(path):
        raise ValueError("not a valid pptx zip package")
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        by_name = {i.filename: i for i in infos}
        reachable, reachable_complete = _reachable_parts(zf)
        junk = tuple(sorted(n for n in by_name if _is_junk_part(n)))
        # 关系图不完整（有 .rels 解析失败）时，无法可靠判定"无引用"——宁可不报 orphan，
        # 也绝不能把实际被引用、只是读不出引用的部件误删（数据丢失防线）。
        orphans = tuple(sorted(
            n for n in by_name
            if _is_content_part(n)
            and n not in reachable
            and not _is_junk_part(n)
            and not n.lower().startswith("docprops/thumbnail")
        )) if reachable_complete else ()
        duplicate_groups = tuple(_duplicate_media_groups(zf, by_name, reachable))
        duplicate_reclaim = sum(g.compressed_reclaimable for g in duplicate_groups)
        unused_layouts, unused_masters = _unused_layouts_and_masters(zf)
        high_risk = _high_risk_notes(by_name)
        buckets = tuple(_size_buckets(infos))
        return SlimReport(
            path=path,
            original_size=os.path.getsize(path),
            package_parts=len(infos),
            package_compressed_bytes=sum(int(i.compress_size or 0) for i in infos),
            buckets=buckets,
            duplicate_media_groups=duplicate_groups,
            duplicate_media_reclaimable=duplicate_reclaim,
            orphan_parts=orphans,
            orphan_reclaimable=sum(int(by_name[n].compress_size or 0) for n in orphans),
            junk_parts=junk,
            junk_reclaimable=sum(int(by_name[n].compress_size or 0) for n in junk),
            unused_layouts=unused_layouts,
            unused_masters=unused_masters,
            high_risk_notes=high_risk,
            reachable_complete=reachable_complete,
        )


def slim_pptx(path: str, output_path: str | None = None,
              options: SlimOptions | None = None, *, overwrite: bool = False) -> SlimResult:
    options = options or SlimOptions()
    source = os.path.abspath(path)
    dest = os.path.abspath(output_path or default_output_path(source))
    if os.path.normcase(source) == os.path.normcase(dest):
        raise ValueError("slim output must not overwrite the source file")
    # overwrite=True 用于"用户已在系统另存对话框确认覆盖"的场景；自动默认命名路径仍保持拒绝。
    if not overwrite and os.path.exists(dest):
        raise FileExistsError("slim output already exists")
    report = analyze_pptx(source)
    remove_parts: set[str] = set()
    actions: list[str] = []
    if options.remove_junk and report.junk_parts:
        remove_parts.update(report.junk_parts)
        actions.append(f"清理包内垃圾 {len(report.junk_parts)} 个")
    if options.remove_orphans and report.orphan_parts:
        remove_parts.update(report.orphan_parts)
        actions.append(f"清理无引用部件 {len(report.orphan_parts)} 个")

    duplicate_map: dict[str, str] = {}
    if options.dedupe_media and report.duplicate_media_groups:
        for group in report.duplicate_media_groups:
            candidates = (group.keep_part, *group.duplicate_parts)
            keep = next((part for part in candidates if part not in remove_parts), "")
            if not keep:
                continue
            for part in candidates:
                if part == keep:
                    continue
                if part not in remove_parts:
                    duplicate_map[part] = keep
                remove_parts.add(part)
        if duplicate_map:
            actions.append(f"合并完全重复媒体 {len(duplicate_map)} 个")
    if options.repackage:
        actions.append("重新打包 PPTX")

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pptdoctor-slim-", suffix=".pptx", dir=os.path.dirname(dest) or None)
    os.close(fd)
    try:
        with zipfile.ZipFile(source) as zin, zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zout:
            rel_updates = _relationship_updates(zin, duplicate_map) if duplicate_map else {}
            for info in zin.infolist():
                name = info.filename
                if info.is_dir():
                    continue
                if name in remove_parts or _rels_owner_removed(name, remove_parts):
                    continue
                if name == "[Content_Types].xml":
                    data = _clean_content_types(zin.read(name), remove_parts)
                elif name in rel_updates:
                    data = rel_updates[name]
                    zout.writestr(name, data)
                    continue
                else:
                    _copy_zip_member(zin, zout, info)
                    continue
                zout.writestr(name, data)
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        return SlimResult(
            ok=False,
            source_path=source,
            output_path=dest,
            original_size=report.original_size,
            slim_size=0,
            saved_bytes=0,
            removed_parts=tuple(sorted(remove_parts)),
            deduped_media=len(duplicate_map),
            actions=tuple(actions),
            error=f"{type(exc).__name__}: {exc}",
        )
    slim_size = os.path.getsize(dest)
    return SlimResult(
        ok=True,
        source_path=source,
        output_path=dest,
        original_size=report.original_size,
        slim_size=slim_size,
        saved_bytes=max(0, report.original_size - slim_size),
        removed_parts=tuple(sorted(remove_parts)),
        deduped_media=len(duplicate_map),
        actions=tuple(actions),
    )


def _size_buckets(infos: list[zipfile.ZipInfo]) -> list[SizeBucket]:
    acc: dict[str, list[int]] = {}
    for info in infos:
        label = _bucket_for(info.filename)
        vals = acc.setdefault(label, [0, 0, 0])
        vals[0] += 1
        vals[1] += int(info.compress_size or 0)
        vals[2] += int(info.file_size or 0)
    return [
        SizeBucket(label, count=v[0], compressed_bytes=v[1], raw_bytes=v[2])
        for label, v in sorted(acc.items(), key=lambda item: item[1][1], reverse=True)
    ]


def _bucket_for(name: str) -> str:
    low = name.lower()
    ext = posixpath.splitext(low)[1]
    if low.startswith("ppt/media/"):
        if ext in _IMAGE_EXTS:
            return "图片"
        if ext in _VIDEO_EXTS:
            return "视频"
        if ext in _AUDIO_EXTS:
            return "音频"
        return "媒体"
    if low.startswith("ppt/slidemasters/") or low.startswith("ppt/slidelayouts/"):
        return "母版/版式"
    if low.startswith("ppt/embeddings/"):
        return "嵌入对象"
    if low.startswith("ppt/notesslides/") or low.startswith("ppt/comments/"):
        return "备注/评论"
    if low.startswith("ppt/slides/"):
        return "幻灯片 XML"
    return "其他"


def _duplicate_media_groups(
    zf: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
    preferred_keep_parts: set[str] | None = None,
) -> list[DuplicateMediaGroup]:
    preferred_keep_parts = preferred_keep_parts or set()
    groups: dict[tuple[str, str], list[str]] = {}
    for name in by_name:
        if not _is_media_part(name):
            continue
        h = _hash_zip_member(zf, name)
        ext = posixpath.splitext(name.lower())[1]
        groups.setdefault((h, ext), []).append(name)
    out: list[DuplicateMediaGroup] = []
    for names in groups.values():
        if len(names) < 2:
            continue
        names = sorted(names)
        keep = next((name for name in names if name in preferred_keep_parts), names[0])
        dups = tuple(name for name in names if name != keep)
        out.append(DuplicateMediaGroup(
            keep_part=keep,
            duplicate_parts=dups,
            compressed_reclaimable=sum(int(by_name[n].compress_size or 0) for n in dups),
            raw_reclaimable=sum(int(by_name[n].file_size or 0) for n in dups),
        ))
    out.sort(key=lambda g: g.compressed_reclaimable, reverse=True)
    return out


def _hash_zip_member(zf: zipfile.ZipFile, name: str) -> str:
    h = hashlib.sha256()
    with zf.open(name, "r") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_zip_member(zin: zipfile.ZipFile, zout: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    out_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    # 保留源包原本的压缩方式：PowerPoint 已把图片/视频/音频按 STORED 存放，重新 deflate 既压不动
    # 又耗 CPU（大视频尤甚），还可能因 deflate 封装开销略增体积。保留即避免无谓重压、永不增大。
    out_info.compress_type = info.compress_type
    out_info.external_attr = info.external_attr
    out_info.comment = info.comment
    out_info.extra = info.extra
    with zin.open(info, "r") as src, zout.open(out_info, "w") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


def _reachable_parts(zf: zipfile.ZipFile) -> tuple[set[str], bool]:
    """返回 (可达部件集合, 关系图是否完整)。

    complete=False 表示有 .rels 读取/解析失败 → 可达性结果不可信，调用方不得据此删除 orphan。
    """
    names = set(zf.namelist())
    reachable: set[str] = set()
    seen_rels: set[str] = set()
    queue: list[str] = []
    root_targets, complete = _rels_targets(zf, "_rels/.rels", "")
    for target in root_targets:
        if target in names:
            queue.append(target)
    while queue:
        part = queue.pop()
        if part in reachable:
            continue
        reachable.add(part)
        rels = _rels_part_for(part)
        if rels in seen_rels or rels not in names:
            continue
        seen_rels.add(rels)
        targets, ok = _rels_targets(zf, rels, part)
        complete = complete and ok
        for target in targets:
            if target in names and target not in reachable:
                queue.append(target)
    return reachable, complete


def _rels_targets(zf: zipfile.ZipFile, rels_name: str, source_part: str) -> tuple[list[str], bool]:
    """返回 (内部目标部件列表, 是否成功读取并解析)。

    第二个值为 False 表示该 .rels 读取/解析失败——此时返回的空列表不代表"无引用"，
    调用方据此不应把潜在目标判为 orphan。
    """
    try:
        root = etree.fromstring(zf.read(rels_name))
    except Exception:  # noqa: BLE001
        return [], False
    out: list[str] = []
    for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
        if (rel.get("TargetMode") or "").lower() == "external":
            continue
        target = rel.get("Target") or ""
        if not target:
            continue
        resolved = _resolve_target(source_part, target)
        if resolved:
            out.append(resolved)
    return out, True


def _relationship_updates(zf: zipfile.ZipFile, duplicate_map: dict[str, str]) -> dict[str, bytes]:
    updates: dict[str, bytes] = {}
    for name in zf.namelist():
        if not name.endswith(".rels"):
            continue
        source_part = _source_part_from_rels(name)
        try:
            root = etree.fromstring(zf.read(name))
        except Exception:  # noqa: BLE001
            continue
        changed = False
        for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
            if (rel.get("TargetMode") or "").lower() == "external":
                continue
            target = rel.get("Target") or ""
            resolved = _resolve_target(source_part, target)
            keep = duplicate_map.get(resolved)
            if keep:
                rel.set("Target", _relative_target(source_part, keep))
                changed = True
        if changed:
            updates[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return updates


def _unused_layouts_and_masters(zf: zipfile.ZipFile) -> tuple[tuple[str, ...], tuple[str, ...]]:
    names = set(zf.namelist())
    all_layouts = {n for n in names if n.lower().startswith("ppt/slidelayouts/slidelayout") and n.endswith(".xml")}
    all_masters = {n for n in names if n.lower().startswith("ppt/slidemasters/slidemaster") and n.endswith(".xml")}
    presentation_rels = "ppt/_rels/presentation.xml.rels"
    slide_parts = [
        t for t in _rels_targets_by_type(zf, presentation_rels, "ppt/presentation.xml").get(REL_SLIDE, [])
        if t in names
    ]
    used_layouts: set[str] = set()
    used_masters: set[str] = set()
    for slide in slide_parts:
        layout = next(iter(_rels_targets_by_type(zf, _rels_part_for(slide), slide).get(REL_SLIDE_LAYOUT, [])), "")
        if layout:
            used_layouts.add(layout)
            master = next(iter(_rels_targets_by_type(zf, _rels_part_for(layout), layout).get(REL_SLIDE_MASTER, [])), "")
            if master:
                used_masters.add(master)
    return tuple(sorted(all_layouts - used_layouts)), tuple(sorted(all_masters - used_masters))


def _rels_targets_by_type(zf: zipfile.ZipFile, rels_name: str, source_part: str) -> dict[str, list[str]]:
    try:
        root = etree.fromstring(zf.read(rels_name))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[str]] = {}
    for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
        if (rel.get("TargetMode") or "").lower() == "external":
            continue
        typ = rel.get("Type") or ""
        target = _resolve_target(source_part, rel.get("Target") or "")
        if typ and target:
            out.setdefault(typ, []).append(target)
    return out


def _clean_content_types(data: bytes, removed_parts: set[str]) -> bytes:
    if not removed_parts:
        return data
    try:
        root = etree.fromstring(data)
    except Exception:  # noqa: BLE001
        return data
    removed = {_normalize_part_name(p) for p in removed_parts}
    for child in list(root):
        if child.tag == f"{{{CT_NS}}}Override" and _normalize_part_name(child.get("PartName") or "") in removed:
            root.remove(child)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _high_risk_notes(by_name: dict[str, zipfile.ZipInfo]) -> tuple[str, ...]:
    notes: list[str] = []
    if any(n.lower().startswith("ppt/embeddings/") for n in by_name):
        notes.append("包含嵌入对象/OLE，默认不清理")
    if any(n.lower().startswith("ppt/notesSlides/".lower()) for n in by_name):
        notes.append("包含备注页，默认不清理")
    if any(posixpath.splitext(n.lower())[1] in _VIDEO_EXTS | _AUDIO_EXTS for n in by_name):
        notes.append("包含音视频，默认不转码")
    return tuple(notes)


def _rels_part_for(part: str) -> str:
    d = posixpath.dirname(part)
    b = posixpath.basename(part)
    return posixpath.join(d, "_rels", b + ".rels") if d else posixpath.join("_rels", b + ".rels")


def _source_part_from_rels(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return ""
    d = posixpath.dirname(rels_name)
    b = posixpath.basename(rels_name)
    if not d.endswith("_rels") or not b.endswith(".rels"):
        return ""
    owner_dir = posixpath.dirname(d)
    owner = b[:-5]
    return posixpath.join(owner_dir, owner) if owner_dir else owner


def _resolve_target(source_part: str, target: str) -> str:
    if not target:
        return ""
    target = target.split("#", 1)[0]
    target = unquote(target)
    if target.startswith("/"):
        resolved = target.lstrip("/")
    else:
        base = posixpath.dirname(source_part)
        resolved = posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)
    if resolved == "." or resolved.startswith("../"):
        return ""
    return resolved.replace("\\", "/")


def _relative_target(source_part: str, target_part: str) -> str:
    base = posixpath.dirname(source_part)
    if not base:
        rel = target_part
    else:
        rel = posixpath.relpath(target_part, base)
    return quote(rel.replace("\\", "/"), safe="/!$&'()*+,;=:@._-~")


def _normalize_part_name(name: str) -> str:
    normalized = unquote(name).lstrip("/")
    return normalized.replace("\\", "/")


def _rels_owner_removed(rels_name: str, removed_parts: set[str]) -> bool:
    if not rels_name.endswith(".rels"):
        return False
    owner = _source_part_from_rels(rels_name)
    return bool(owner and owner in removed_parts)


def _is_media_part(name: str) -> bool:
    return name.lower().startswith("ppt/media/") and posixpath.splitext(name.lower())[1] in _MEDIA_EXTS


def _is_junk_part(name: str) -> bool:
    low = name.lower()
    return posixpath.basename(low) in _JUNK_NAMES or any(low.startswith(p) for p in _JUNK_PREFIXES)


def _is_content_part(name: str) -> bool:
    low = name.lower()
    return (
        not low.endswith("/")
        and low != "[content_types].xml"
        and not low.endswith(".rels")
        and "/_rels/" not in low
    )
