from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

import fixtures_gen as fx
from pptx_finder import slim

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\xe2&\xb5\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _rewrite_zip(path: Path, mutate) -> None:
    with zipfile.ZipFile(path) as zin:
        data = {n: zin.read(n) for n in zin.namelist()}
    mutate(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, blob in data.items():
            zout.writestr(name, blob)
    tmp.replace(path)


def _ensure_png_content_type(data: dict[str, bytes]) -> None:
    root = etree.fromstring(data["[Content_Types].xml"])
    has_png = any(
        child.tag == f"{{{CT_NS}}}Default"
        and (child.get("Extension") or "").lower() == "png"
        for child in root
    )
    if not has_png:
        node = etree.SubElement(root, f"{{{CT_NS}}}Default")
        node.set("Extension", "png")
        node.set("ContentType", "image/png")
    data["[Content_Types].xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _inject_duplicate_media(path: Path) -> None:
    def mutate(data: dict[str, bytes]) -> None:
        _ensure_png_content_type(data)
        data["ppt/media/imageA.png"] = _PNG
        data["ppt/media/imageB.png"] = _PNG
        rels_name = "ppt/slides/_rels/slide1.xml.rels"
        root = etree.fromstring(data[rels_name])
        for idx, target in enumerate(("../media/imageA.png", "../media/imageB.png"), start=90):
            rel = etree.SubElement(root, f"{{{RELS_NS}}}Relationship")
            rel.set("Id", f"rIdSlim{idx}")
            rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
            rel.set("Target", target)
        data[rels_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    _rewrite_zip(path, mutate)


def _inject_orphan_and_junk(path: Path) -> None:
    def mutate(data: dict[str, bytes]) -> None:
        _ensure_png_content_type(data)
        data["ppt/media/orphan.png"] = _PNG * 4
        data["__MACOSX/._deck"] = b"junk"

    _rewrite_zip(path, mutate)


def _inject_media_relationship(
    path: Path,
    part_name: str,
    target: str,
    blob: bytes = _PNG,
    rel_id: str = "rIdSlimMedia",
) -> None:
    def mutate(data: dict[str, bytes]) -> None:
        _ensure_png_content_type(data)
        data[part_name] = blob
        rels_name = "ppt/slides/_rels/slide1.xml.rels"
        root = etree.fromstring(data[rels_name])
        rel = etree.SubElement(root, f"{{{RELS_NS}}}Relationship")
        rel.set("Id", rel_id)
        rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
        rel.set("Target", target)
        data[rels_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    _rewrite_zip(path, mutate)


def test_analyze_detects_duplicate_media_orphans_and_unused_layouts(tmp_path):
    deck = tmp_path / "deck.pptx"
    fx.make_pptx(deck, [{"body": "瘦身体检"}])
    _inject_duplicate_media(deck)
    _inject_orphan_and_junk(deck)

    report = slim.analyze_pptx(str(deck))

    assert report.original_size > 0
    assert report.duplicate_media_groups
    assert report.duplicate_media_groups[0].duplicate_parts == ("ppt/media/imageB.png",)
    assert "ppt/media/orphan.png" in report.orphan_parts
    assert "__MACOSX/._deck" in report.junk_parts
    assert report.unused_layouts
    assert any(bucket.label == "图片" for bucket in report.buckets)
    assert report.low_risk_reclaimable > 0


def test_slim_pptx_creates_copy_and_retargets_duplicate_media(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "瘦身副本"}])
    _inject_duplicate_media(deck)
    _inject_orphan_and_junk(deck)

    result = slim.slim_pptx(str(deck), str(out))

    assert result.ok is True
    assert result.output_path == str(out.resolve())
    assert result.deduped_media == 1
    assert "ppt/media/imageB.png" in result.removed_parts
    assert "ppt/media/orphan.png" in result.removed_parts
    assert out.exists() and out.stat().st_size > 0
    assert deck.exists()

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "ppt/media/imageA.png" in names
        assert "ppt/media/imageB.png" not in names
        assert "ppt/media/orphan.png" not in names
        assert "__MACOSX/._deck" not in names
        rels = etree.fromstring(zf.read("ppt/slides/_rels/slide1.xml.rels"))
        targets = [rel.get("Target") for rel in rels.findall(f"{{{RELS_NS}}}Relationship")]
        assert "../media/imageB.png" not in targets
        assert targets.count("../media/imageA.png") >= 2

    slimmed = slim.analyze_pptx(str(out))
    assert not slimmed.duplicate_media_groups
    assert "ppt/media/orphan.png" not in slimmed.orphan_parts


def test_slim_prefers_referenced_duplicate_media_over_orphan_keep(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "duplicate orphan safety"}])

    def mutate(data: dict[str, bytes]) -> None:
        _ensure_png_content_type(data)
        data["ppt/media/aaa-orphan.png"] = _PNG
        data["ppt/media/zzz-used.png"] = _PNG
        rels_name = "ppt/slides/_rels/slide1.xml.rels"
        root = etree.fromstring(data[rels_name])
        rel = etree.SubElement(root, f"{{{RELS_NS}}}Relationship")
        rel.set("Id", "rIdSlimUsed")
        rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
        rel.set("Target", "../media/zzz-used.png")
        data[rels_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    _rewrite_zip(deck, mutate)

    report = slim.analyze_pptx(str(deck))

    assert "ppt/media/aaa-orphan.png" in report.orphan_parts
    assert report.duplicate_media_groups[0].keep_part == "ppt/media/zzz-used.png"

    result = slim.slim_pptx(str(deck), str(out))

    assert result.ok is True
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "ppt/media/zzz-used.png" in names
        assert "ppt/media/aaa-orphan.png" not in names
        rels = etree.fromstring(zf.read("ppt/slides/_rels/slide1.xml.rels"))
        targets = [rel.get("Target") for rel in rels.findall(f"{{{RELS_NS}}}Relationship")]
        assert "../media/zzz-used.png" in targets
        assert "../media/aaa-orphan.png" not in targets


def test_percent_encoded_relationship_target_is_not_deleted_as_orphan(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "encoded target safety"}])
    _inject_media_relationship(deck, "ppt/media/my image.png", "../media/my%20image.png")

    report = slim.analyze_pptx(str(deck))

    assert "ppt/media/my image.png" not in report.orphan_parts

    result = slim.slim_pptx(str(deck), str(out))

    assert result.ok is True
    with zipfile.ZipFile(out) as zf:
        assert "ppt/media/my image.png" in set(zf.namelist())
        rels = etree.fromstring(zf.read("ppt/slides/_rels/slide1.xml.rels"))
        targets = [rel.get("Target") for rel in rels.findall(f"{{{RELS_NS}}}Relationship")]
        assert "../media/my%20image.png" in targets


def test_orphan_removal_skipped_when_a_rels_fails_to_parse(tmp_path):
    """损坏的 .rels 解析失败 → 可达性不可信 → 绝不把它引用的部件当 orphan 删除。"""
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "corrupt rels safety"}])
    _inject_media_relationship(deck, "ppt/media/keepme.png", "../media/keepme.png", rel_id="rIdKeep")

    def corrupt(data: dict[str, bytes]) -> None:
        data["ppt/slides/_rels/slide1.xml.rels"] = b"<Relationships not-valid <<<"

    _rewrite_zip(deck, corrupt)

    report = slim.analyze_pptx(str(deck))

    # 关系图不可信：不报告任何 orphan（宁可不瘦，也不能误删被引用的图）
    assert report.orphan_parts == ()
    assert report.reachable_complete is False

    result = slim.slim_pptx(str(deck), str(out))

    assert result.ok is True
    assert "ppt/media/keepme.png" not in result.removed_parts
    with zipfile.ZipFile(out) as zf:
        assert "ppt/media/keepme.png" in set(zf.namelist())


def test_slim_pptx_refuses_to_overwrite_existing_output(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "existing.pptx"
    fx.make_pptx(deck, [{"body": "existing output safety"}])
    out.write_bytes(b"do not clobber")

    try:
        slim.slim_pptx(str(deck), str(out))
    except FileExistsError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected FileExistsError")

    assert out.read_bytes() == b"do not clobber"


def test_media_payloads_are_streamed_for_hashing_and_copy(tmp_path, monkeypatch):
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "stream media"}])
    large_blob = b"x" * (2 * 1024 * 1024 + 17)
    _inject_media_relationship(deck, "ppt/media/large.png", "../media/large.png", large_blob)

    original_read = zipfile.ZipFile.read

    def guarded_read(self, name, *args, **kwargs):
        actual = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if actual == "ppt/media/large.png":
            raise AssertionError("large media payload should be streamed")
        return original_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded_read)

    report = slim.analyze_pptx(str(deck))
    result = slim.slim_pptx(str(deck), str(out))

    assert report.original_size > 0
    assert result.ok is True
    with zipfile.ZipFile(out) as zf:
        with zf.open("ppt/media/large.png") as fh:
            assert fh.read() == large_blob


def test_stored_media_is_not_recompressed_and_does_not_grow(tmp_path):
    """真实 PPT 里图片/视频以 STORED 存放；瘦身重打包必须保留 STORED（不 level9 重压），且不增大体积。"""
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "store media"}])
    blob = bytes((i * 1103515245 + 12345) & 0xFF for i in range(60000))  # 伪随机~不可压缩

    # 以 STORED 写入媒体（模拟 PowerPoint 对已压缩媒体的存法），并加引用关系
    with zipfile.ZipFile(deck) as zin:
        data = {n: zin.read(n) for n in zin.namelist()}
    root = etree.fromstring(data["[Content_Types].xml"])
    node = etree.SubElement(root, f"{{{CT_NS}}}Default")
    node.set("Extension", "jpg")
    node.set("ContentType", "image/jpeg")
    data["[Content_Types].xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    rels_name = "ppt/slides/_rels/slide1.xml.rels"
    rroot = etree.fromstring(data[rels_name])
    rel = etree.SubElement(rroot, f"{{{RELS_NS}}}Relationship")
    rel.set("Id", "rIdPhoto")
    rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
    rel.set("Target", "../media/photo.jpg")
    data[rels_name] = etree.tostring(rroot, xml_declaration=True, encoding="UTF-8", standalone=True)
    tmp = deck.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, b in data.items():
            zout.writestr(name, b)
        zi = zipfile.ZipInfo("ppt/media/photo.jpg")
        zi.compress_type = zipfile.ZIP_STORED
        zout.writestr(zi, blob)
    tmp.replace(deck)
    with zipfile.ZipFile(deck) as zf:
        assert zf.getinfo("ppt/media/photo.jpg").compress_type == zipfile.ZIP_STORED

    result = slim.slim_pptx(str(deck), str(out))

    assert result.ok is True
    with zipfile.ZipFile(out) as zf:
        photo = zf.getinfo("ppt/media/photo.jpg")
        assert photo.compress_type == zipfile.ZIP_STORED          # 没被 level9 重压
        assert zf.read("ppt/media/photo.jpg") == blob              # 字节无损
        assert zf.getinfo("ppt/slides/slide1.xml").compress_type == zipfile.ZIP_DEFLATED  # XML 仍压缩
    assert out.stat().st_size <= deck.stat().st_size              # 不会越瘦越胖


def test_slim_pptx_overwrites_existing_output_when_allowed(tmp_path):
    """用户在系统另存对话框已确认覆盖 → overwrite=True 时应替换已存在文件，而非报 FileExistsError。"""
    deck = tmp_path / "deck.pptx"
    out = tmp_path / "out.pptx"
    fx.make_pptx(deck, [{"body": "overwrite allowed"}])
    out.write_bytes(b"old stale content")

    result = slim.slim_pptx(str(deck), str(out), overwrite=True)

    assert result.ok is True
    assert zipfile.is_zipfile(out)  # 旧内容已被替换为有效 pptx
    with zipfile.ZipFile(out) as zf:
        assert "[Content_Types].xml" in set(zf.namelist())
    # 仍绝不允许覆盖源文件（即使 overwrite=True）
    try:
        slim.slim_pptx(str(deck), str(deck), overwrite=True)
    except ValueError as exc:
        assert "must not overwrite" in str(exc)
    else:
        raise AssertionError("expected ValueError for source overwrite")


def test_slim_pptx_refuses_to_overwrite_source(tmp_path):
    deck = tmp_path / "deck.pptx"
    fx.make_pptx(deck, [{"body": "不要覆盖源文件"}])

    try:
        slim.slim_pptx(str(deck), str(deck))
    except ValueError as exc:
        assert "must not overwrite" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_default_output_path_avoids_existing_files(tmp_path):
    deck = tmp_path / "deck.pptx"
    first = tmp_path / "deck.slim.pptx"
    fx.make_pptx(deck, [{"body": "输出命名"}])
    first.write_bytes(b"x")

    assert slim.default_output_path(str(deck)).endswith("deck.slim-2.pptx")
