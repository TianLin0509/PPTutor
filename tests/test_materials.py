from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from pptx_finder.materials import Material, MaterialLibrary, validate_pptx, _read_metadata, MARKER


def populate(library, item_id="a" * 32, name="红色箭头", category="箭头"):
    item = Material(item_id, name, category, 1)
    folder = library.folder(item.id)
    folder.mkdir()
    (folder / "material.pptx").write_bytes(b"native PPT bytes")
    (folder / "preview.png").write_bytes(b"preview")
    library._write(folder, item)
    return item


def test_collection_is_independent_searchable_and_rename_preserves_native_bytes(tmp_path):
    library = MaterialLibrary(tmp_path)
    item = populate(library)
    assert library.list("红色")[0] == item
    assert library.list(category="框") == []
    library.rename(item.id, "转弯箭头", "流程")
    assert library.list("转弯")[0].category == "流程"
    assert (library.folder(item.id) / "material.pptx").read_bytes() == b"native PPT bytes"
    assert "转弯箭头" in (library.folder(item.id) / "item.json").read_text("utf-8")


def test_delete_only_moves_selected_asset_into_trash(tmp_path):
    library = MaterialLibrary(tmp_path)
    a = populate(library)
    b = populate(library, "b" * 32)
    library.delete([a.id])
    assert library.list() == [b]
    assert len(list((tmp_path / ".trash").glob("*/material.pptx"))) == 1


@pytest.mark.parametrize("value", ["..", "../outside", "C:/users", "a" * 31, "x" * 32])
def test_asset_identifiers_cannot_escape_library(tmp_path, value):
    with pytest.raises(ValueError):
        MaterialLibrary(tmp_path).folder(value)


def test_corrupt_catalog_is_reported_instead_of_appearing_empty(tmp_path):
    library = MaterialLibrary(tmp_path)
    item = populate(library)
    (library.folder(item.id) / "item.json").write_text("broken", "utf-8")
    with pytest.raises(ValueError):
        library.list()


def make_zip(path, extra):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ppt/presentation.xml", "<presentation/>")
        for name, text in extra.items():
            z.writestr(name, text)


@pytest.mark.parametrize("name", ["ppt/vbaProject.bin", "ppt/embeddings/oleObject1.bin"])
def test_shared_packs_with_executable_embeddings_are_rejected(tmp_path, name):
    path = tmp_path / "pack.pptx"
    make_zip(path, {name: "payload"})
    with pytest.raises(ValueError, match="嵌入程序"):
        validate_pptx(path)


def test_linked_materials_rejected_but_hyperlinks_allowed(tmp_path):
    path = tmp_path / "pack.pptx"
    rels = '<Relationships><Relationship Type="http://example/image" TargetMode="External" Target="file:///private/image.png" /></Relationships>'
    make_zip(path, {"ppt/slides/_rels/slide1.xml.rels": rels})
    with pytest.raises(ValueError, match="外部文件"):
        validate_pptx(path)
    make_zip(path, {"ppt/slides/_rels/slide1.xml.rels": rels.replace("/image\"", "/hyperlink\"")})
    validate_pptx(path)


def notes(text):
    shape = SimpleNamespace(HasTextFrame=True, TextFrame=SimpleNamespace(TextRange=SimpleNamespace(Text=text)))
    return SimpleNamespace(NotesPage=SimpleNamespace(Shapes=SimpleNamespace(Count=1, Item=lambda _: shape)))


def test_pack_notes_preserve_unicode_and_receive_new_local_identity():
    text = MARKER + json.dumps({"id": "a" * 32, "name": "公司 Logo", "category": "品牌"}, ensure_ascii=False)
    a, b = _read_metadata(notes(text)), _read_metadata(notes(text.replace("\n", "\r")))
    assert a.name == b.name == "公司 Logo"
    assert a.category == "品牌"
    assert a.id != b.id


def test_ordinary_ppt_is_not_silently_imported_as_material():
    with pytest.raises(ValueError, match="不是 PPT Doctor 素材包"):
        _read_metadata(notes("汇报内容"))


def test_pack_export_requires_a_bounded_nonempty_selection(tmp_path):
    library = MaterialLibrary(tmp_path)
    for selection in ([], ["a" * 32] * 501):
        with pytest.raises(ValueError):
            library.export_pack(selection, tmp_path / "out.pptx")
    assert not (tmp_path / "out.pptx").exists()
