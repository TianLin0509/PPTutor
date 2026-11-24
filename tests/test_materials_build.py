"""团队素材构建工具：脱敏、发布硬闸门、精选子集、三件套组件与审计绑定。"""
import hashlib
import json
import zipfile

import pytest

from pptx_finder import imgtext_ocr, materials
from materials_testkit import build_pack_dir, fake_renders, make_pack, png_bytes, rows, tool


@pytest.fixture
def t(monkeypatch):
    module = tool()
    blocked, ok = png_bytes((200, 0, 0, 255)), png_bytes((0, 160, 0, 255))
    monkeypatch.setattr(module, "BLOCKED_IMAGES", {hashlib.sha256(blocked).hexdigest(): "测试待确认图"})
    monkeypatch.setattr(module, "CONFIRMED_IMAGES", {hashlib.sha256(ok).hexdigest(): "测试已确认图"})
    module._blocked, module._ok = blocked, ok
    return module


def _source(t, tmp_path):
    data = [
        {"id": "a1", "name": "蓝色箭头", "category": "箭头与流程", "text": "提升 -40%"},
        {"id": "a2", "name": "灰条评级", "category": "团队母题·复刻", "text": "6188 站"},
        {"id": "a3", "name": "立体高楼", "category": "图标与小图", "picture": t._blocked, "only_picture": True},
        {"id": "a4", "name": "红叉", "category": "图标与小图", "picture": t._ok, "only_picture": True},
        {"id": "a5", "name": "混排卡片", "category": "组合件·流程与步骤", "picture": t._blocked, "text": "示例文字"},
        {"id": "a6", "name": "模板页", "category": "整页模板·方案与原理", "text": "示例 25/10/30"},
    ]
    return make_pack(tmp_path / "src.pptx", data, author="someone@example.com")


def test_source_pack_fails_gate_with_counts_only(t, tmp_path):
    report = t.scan(_source(t, tmp_path))
    fails = t.gate_failures(report)
    assert report["docprops_personal"] >= 2 and report["images_copyright_pending"] == 1
    assert report["numeric_hits"]["true_hits"] == 2 and report["numeric_hits"]["pending_pages"] == [1, 2]
    joined = json.dumps(report, ensure_ascii=False) + "".join(fails)
    assert "someone@example.com" not in joined and "6188" not in joined   # 报告不复述原文
    assert any("docProps" in f for f in fails) and any("示意数字" in f for f in fails)


def test_sanitize_removes_blocked_images_redacts_and_passes_gate(t, tmp_path):
    clean = tmp_path / "clean.pptx"
    report = t.sanitize(_source(t, tmp_path), clean)
    assert [x["id"] for x in report["removed_items"]] == ["a3"]      # 只剩待确认图的那页整页剔除
    assert report["pictures_removed"] == 2                            # 混排页只删图片，保留文字
    assert report["numbers_redacted"] == 2 and report["categories_renamed"] == 1
    after = t.scan(clean)
    assert t.gate_failures(after) == []
    items = materials.read_pack_index(clean)
    assert [x["id"] for x in items] == ["a1", "a2", "a4", "a5", "a6"]
    assert [x["page"] for x in items] == [1, 2, 3, 4, 5]
    assert items[1]["category"] == "团队母题"
    with zipfile.ZipFile(clean) as z:
        names = z.namelist()
        core = z.read("docProps/core.xml").decode()
        assert "someone@example.com" not in core and "creator" not in core
        assert "docProps/thumbnail.jpeg" not in names
        assert sum(n.startswith("ppt/media/") for n in names) == 1      # 待确认图连文件一起清掉
        assert sum(n.startswith("ppt/slides/slide") for n in names) == 5
        text = "".join(z.read(n).decode() for n in names if n.startswith("ppt/slides/slide"))
        assert "-XX%" in text and "XX 站" in text and "6188" not in text
    # python-pptx 能正常打开（结构没坏）
    from pptx import Presentation
    assert len(Presentation(str(clean)).slides) == 5


def test_allowed_numbers_are_kept_and_counted_as_approved(t, tmp_path):
    clean = tmp_path / "clean.pptx"
    t.sanitize(_source(t, tmp_path), clean, allow_numbers=["40%"])
    report = t.scan(clean, allow_numbers=["40%"])
    assert report["numeric_hits"] == {"true_hits": 1, "approved_after_redaction": 1, "pending": 0,
                                      "pending_pages": []}


def test_gate_blocks_unregistered_image_and_hyperlink(t, tmp_path):
    from pptx import Presentation
    from pptx.util import Pt
    src = make_pack(tmp_path / "x.pptx", [{"id": "b1", "name": "图", "category": "图标与小图",
                                           "picture": png_bytes((1, 2, 3, 255)), "only_picture": True}])
    prs = Presentation(str(src))
    box = prs.slides[0].shapes.add_textbox(Pt(10), Pt(10), Pt(100), Pt(20))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "链接"
    run.hyperlink.address = "https://example.com"
    prs.save(str(src))
    clean = tmp_path / "c.pptx"
    t.sanitize(src, clean)
    fails = t.gate_failures(t.scan(clean))
    assert any("未登记图片" in f for f in fails)
    assert any("hyperlinks" in f for f in fails) and any("external_rels" in f for f in fails)
    with pytest.raises(t.GateError):
        t.enforce_gate(clean, "测试包")


def test_run_builds_three_file_component_core_and_bound_audit(t, tmp_path):
    data = (rows(10, "箭头与流程", "p") + rows(4, "组合件·流程与步骤", "c") +
            rows(3, "整页模板·方案与原理", "w"))
    out, core, audit = build_pack_dir(tmp_path, data, core=True)
    manifest = json.loads((out / "component.json").read_text("utf-8"))
    assert set(manifest["files"]) == {"team-materials.pptx", "index.json", "thumbs.zip"}
    assert manifest["items"] == 17 and manifest["kind"] == "materials"
    archive = out / "component.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive"]["hash"]
    assert audit["bound_to"]["component_zip_sha256"] == manifest["archive"]["hash"]
    assert audit["bound_to"]["pptx_sha256"] == manifest["files"]["team-materials.pptx"]["hash"]
    assert audit["release_gate"] == "passed" and audit["scan_after"]["docprops_personal"] == 0
    assert not (out / "_work").exists()
    with zipfile.ZipFile(archive) as z:
        assert sorted(z.namelist()) == ["index.json", "team-materials.pptx", "thumbs.zip"]
    # 精选：零件 / 组合件每类 round(n×0.28)，整页模板每类 1
    assert audit["core"]["items"] == 3 + 1 + 1
    assert sorted(p.name for p in core.iterdir()) == ["index.json", "team-materials-core.pptx", "thumbs.zip"]
    index = json.loads((core / "index.json").read_text("utf-8"))
    assert all(x["thumb"] and x["w"] <= 336 and x["h"] <= 256 for x in index["items"])
    full_index = json.loads(zipfile.ZipFile(archive).read("index.json"))
    assert full_index["schema"] == materials.INDEX_SCHEMA and full_index["version"] == "1.2.0"
    assert {x["group"] for x in full_index["items"]} == {"零件", "组合件", "整页模板"}
    # 同样输入 → 同样字节（zip 时间戳固定）
    again = t.run(tmp_path / "src-1.2.0.pptx", "1.2.0", tmp_path / "again",
                  thumbs_from=tmp_path / "renders-1.2.0", log=lambda *_: None)
    assert again["bound_to"]["component_zip_sha256"] == manifest["archive"]["hash"]


def test_new_component_still_installs_with_old_client_checks(tmp_path):
    """旧版客户端只认 files 清单 + 两层 sha256：三件套照样能装，pptx 原样可读。"""
    out, _, _ = build_pack_dir(tmp_path, rows(3))
    manifest = json.loads((out / "component.json").read_text("utf-8"))
    target = tmp_path / "installed"
    assert imgtext_ocr.install(manifest, archive_path=out / "component.zip", target_dir=target,
                               what="团队素材包") == "1.2.0"
    assert [x["id"] for x in materials.read_pack_index(target / "team-materials.pptx")] == ["m1", "m2", "m3"]


def test_thumbnail_encoding_caps_and_uses_palette(t, tmp_path):
    from PIL import Image
    src = fake_renders(tmp_path / "r", ["big"], size=(1600, 400)) / "big.png"
    data, w, h = t.encode_thumb(src, scale=1.0, cap=t.FULL_CAP)
    assert (w, h) == (648, 162)
    image = Image.open(__import__("io").BytesIO(data))
    assert image.mode == "P" and image.size == (648, 162)
    data, w, h = t.encode_thumb(src, scale=2 / 3, cap=t.CORE_CAP)
    assert w <= 336 and h <= 256


def test_curated_ids_rule():
    t = tool()
    items = ([{"id": f"a{i}", "category": "箭头与流程"} for i in range(91)] +
             [{"id": f"b{i}", "category": "整页模板·盘点与总结"} for i in range(4)] +
             [{"id": f"c{i}", "category": "通信图元"} for i in range(2)])
    keep = t.curated_ids(items)
    assert len([k for k in keep if k.startswith("a")]) == 25
    assert keep >= {"a0", "b0", "c0"} and "b1" not in keep and "c1" not in keep


def test_scan_only_cli_exit_codes(t, tmp_path):
    assert t.main([str(_source(t, tmp_path)), "--version", "1.2.0", "--scan-only"]) == 1
    clean = tmp_path / "clean.pptx"
    t.sanitize(_source(t, tmp_path), clean)
    assert t.main([str(clean), "--version", "1.2.0", "--scan-only"]) == 0
    with pytest.raises(SystemExit, match="X.Y.Z"):
        t.run(clean, "v1", tmp_path / "o")


def test_scan_resolves_drawingml_by_namespace_not_prefix(t, tmp_path):
    """m4 MF-M2-01：把 a: 换成别的合法前缀（或默认命名空间），扫描照样拦得住。"""
    clean = tmp_path / "clean.pptx"
    t.sanitize(_source(t, tmp_path), clean)
    assert t.gate_failures(t.scan(clean)) == []
    parts = t.load_parts(clean)
    slide = next(n for n in parts if n.startswith("ppt/slides/slide"))
    xml = parts[slide].decode("utf-8")
    uri = t.A_URI
    for variant in ("alias", "default"):
        if variant == "alias":
            forged = xml.replace(f'xmlns:a="{uri}"', f'xmlns:dm="{uri}"')
            forged = forged.replace("<a:", "<dm:").replace("</a:", "</dm:")
            forged = forged.replace("<dm:t>示例", "<dm:t>联系 fake.person@example.com 示例", 1)
            if "fake.person" not in forged:
                forged = forged.replace("</dm:t>", " fake.person@example.com 6188</dm:t>", 1)
        else:
            body = ('<p:txBody xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                    f'<p xmlns="{uri}"><r><t>fake.person@example.com</t></r><r><t>61</t></r>'
                    '<r><t>88</t></r></p></p:txBody>')
            forged = xml.replace("</p:cSld>", f"<!--x-->{body}</p:cSld>", 1)
        parts[slide] = forged.encode("utf-8")
        bad = tmp_path / f"{variant}.pptx"
        t.write_parts(parts, bad)
        report = t.scan(bad)
        assert report["blacklist_hits"]["email"] >= 1, variant
        assert report["numeric_hits"]["pending"] >= 1, variant     # 跨 run 的 61|88 也算 6188
        with pytest.raises(t.GateError):
            t.enforce_gate(bad, variant)
        # 脱敏也认别名前缀：数字被改掉
        fixed = tmp_path / f"{variant}-fixed.pptx"
        if variant == "alias":
            t.sanitize(bad, fixed)
            assert t.scan(fixed)["numeric_hits"]["pending"] == 0
