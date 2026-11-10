"""团队素材包组件：按 id 去重导入、整包升级、只动组件自己的素材；下载安装；打包工具；界面入口。"""
from contextlib import contextmanager
import functools
import hashlib
import http.server
import json
from pathlib import Path
import threading
import zipfile

import pytest
from PySide6.QtCore import Qt

from pptx_finder import components, materials, team_materials
from pptx_finder.materials import MARKER, Material, MaterialLibrary, pack_item_id


@pytest.fixture(autouse=True)
def own_data_dir(tmp_path, monkeypatch):
    """每条用例独立的数据目录：组件安装记录不在用例之间串。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))


# ---------- 假的 PowerPoint：素材包每页 = 备注里的 JSON ----------
class _Notes:
    def __init__(self, text):
        shape = type("S", (), {"HasTextFrame": True})()
        shape.TextFrame = type("T", (), {})()
        shape.TextFrame.TextRange = type("R", (), {"Text": text})()
        self.Shapes = type("Sh", (), {"Count": 1, "Item": staticmethod(lambda _i: shape)})()


class _Slide:
    def __init__(self, raw):
        self.raw = raw
        self.NotesPage = _Notes(MARKER + json.dumps(raw, ensure_ascii=False))


class _Slides:
    def __init__(self, slides):
        self.slides = slides

    @property
    def Count(self):
        return len(self.slides)

    def Item(self, i):
        return self.slides[i - 1]

    def InsertFromFile(self, path, index, first, last):
        self.slides.extend(_Slide(r) for r in _PACKS[path][first - 1:last])
        return last - first + 1


class _Pres:
    def __init__(self, slides):
        self.Slides = _Slides(slides)


_PACKS: dict[str, list] = {}


class _Session:
    def open(self, path):
        return _Pres([_Slide(r) for r in _PACKS[str(path)]])

    def new(self):
        return _Pres([])

    def close(self, pres):
        pass


@pytest.fixture
def fake_ppt(monkeypatch):
    @contextmanager
    def session():
        yield _Session()

    def save(pres, path):
        path.write_text(json.dumps(pres.Slides.Item(1).raw, ensure_ascii=False), "utf-8")

    monkeypatch.setattr(materials.ppt_transfer, "temporary_presentations", session)
    monkeypatch.setattr(materials, "validate_pptx", lambda path: None)
    monkeypatch.setattr(materials, "_metadata", lambda slide, item: None)
    monkeypatch.setattr(materials, "_preview", lambda slide, target: target.write_bytes(b"png"))
    monkeypatch.setattr(materials, "_save_deck", save)
    # 拷贝到暂存区的快照沿用原件的内容
    real_copy = materials.shutil.copyfile

    def copy(src, dst):
        real_copy(src, dst)
        if str(src) in _PACKS:
            _PACKS[str(dst)] = _PACKS[str(src)]
        return dst
    monkeypatch.setattr(materials.shutil, "copyfile", copy)
    yield
    _PACKS.clear()


def _pack(tmp_path, name, rows):
    path = tmp_path / name
    path.write_bytes(b"pptx")
    _PACKS[str(path)] = [{"id": i, "name": n, "category": c} for i, n, c in rows]
    return path


def _own(library, name="我的箭头"):
    item = Material("f" * 32, name, "自己的", 9e9)
    folder = library.folder(item.id)
    folder.mkdir()
    (folder / "material.pptx").write_bytes(b"mine")
    library._write(folder, item)
    return item


V1 = [("m1", "蓝色箭头", "箭头"), ("m2", "圆角卡片", "容器"), ("m3", "基站图标", "图标")]


def test_sync_is_idempotent_keeps_pack_order_and_never_touches_user_items(tmp_path, fake_ppt):
    library = MaterialLibrary(tmp_path / "lib")
    mine = _own(library)
    pack = _pack(tmp_path, "v1.pptx", V1)
    stats = library.sync_pack("team", pack, "1.0.0")
    assert stats == {"total": 3, "added": 3, "updated": 0, "removed": 0}
    names = [x.name for x in library.list() if x.id != mine.id]
    assert names == ["蓝色箭头", "圆角卡片", "基站图标"]          # 保持素材包里的顺序
    assert library.pack_record("team") == {"version": "1.0.0",
                                           "ids": [pack_item_id("team", i) for i, _n, _c in V1]}
    stats = library.sync_pack("team", pack, "1.0.0")              # 再装一次
    assert stats["added"] == 0 and stats["updated"] == 3
    assert len(library.list()) == 4                               # 不重复
    assert library.list(category="箭头")[0].name == "蓝色箭头"
    assert (library.folder(mine.id) / "material.pptx").read_bytes() == b"mine"
    assert not list((tmp_path / "lib").glob(".sync-*")) and not list((tmp_path / "lib").glob(".discard-*"))


def test_upgrade_replaces_old_pack_items_but_keeps_user_items(tmp_path, fake_ppt):
    library = MaterialLibrary(tmp_path / "lib")
    library.sync_pack("team", _pack(tmp_path, "v1.pptx", V1), "1.0.0")
    mine = _own(library)
    v2 = [("m1", "蓝色箭头（新版）", "箭头与流程"), ("m3", "基站图标", "图标"), ("m4", "光带", "装饰")]
    stats = library.sync_pack("team", _pack(tmp_path, "v2.pptx", v2), "1.1.0")
    assert stats == {"total": 3, "added": 1, "updated": 2, "removed": 1}
    by_name = {x.name: x for x in library.list()}
    assert set(by_name) == {"蓝色箭头（新版）", "基站图标", "光带", "我的箭头"}
    assert by_name["蓝色箭头（新版）"].category == "箭头与流程"
    assert not library.folder(pack_item_id("team", "m2")).exists()
    assert library.pack_record("team")["version"] == "1.1.0"
    assert library.list("我的")[0] == mine
    assert library.remove_pack("team") == 3
    assert library.list() == [mine] and library.pack_record("team") == {}


@pytest.mark.parametrize("rows, match", [
    ([("m1", "a", ""), ("m1", "b", "")], "id 重复"),
    ([("m1", "a", ""), ("", "b", "")], "缺少 id"),
])
def test_bad_pack_publishes_nothing(tmp_path, fake_ppt, rows, match):
    library = MaterialLibrary(tmp_path / "lib")
    library.sync_pack("team", _pack(tmp_path, "v1.pptx", V1), "1.0.0")
    before = library.list()
    with pytest.raises(ValueError, match=match):
        library.sync_pack("team", _pack(tmp_path, "bad.pptx", rows), "2.0.0")
    assert library.list() == before and library.pack_record("team")["version"] == "1.0.0"


def test_cancel_before_publish_leaves_library_unchanged(tmp_path, fake_ppt):
    library = MaterialLibrary(tmp_path / "lib")
    seen = []
    with pytest.raises(InterruptedError):
        library.sync_pack("team", _pack(tmp_path, "v1.pptx", V1), "1.0.0",
                          progress=lambda d, t: seen.append(d), cancel=lambda: len(seen) >= 2)
    assert library.list() == [] and library.pack_record("team") == {}


# ---------- 下载安装：本地 http 服务模拟备用服务器 ----------
def _publish(root: Path, version: str, payload: bytes) -> Path:
    folder = root / "materials"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / "component.zip", "w") as z:
        z.writestr(team_materials.PACK_FILE, payload)
    data = (folder / "component.zip").read_bytes()
    (folder / "component.json").write_text(json.dumps({
        "version": version, "kind": "materials",
        "files": {team_materials.PACK_FILE: {"hash": hashlib.sha256(payload).hexdigest(), "size": len(payload)}},
        "archive": {"name": "component.zip", "hash": hashlib.sha256(data).hexdigest(), "size": len(data)}}), "utf-8")
    return folder


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    root.mkdir()
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PPTX_FINDER_UPDATE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PPTX_FINDER_UPDATE_NO_GITHUB", "1")
    yield root
    server.shutdown()
    server.server_close()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_install_downloads_verifies_and_imports_then_upgrades(mirror, monkeypatch):
    calls = []

    def fake_sync(self, pack, source, version, *, progress=None, cancel=None):
        calls.append((pack, Path(source).read_bytes(), version))
        if progress:
            progress(1, 1)
        self._write_pack_record(pack, {"version": version, "ids": []})
        return {}
    monkeypatch.setattr(MaterialLibrary, "sync_pack", fake_sync)
    assert not components.is_installed("materials")
    _publish(mirror, "1.0.0", b"pack-v1")
    seen = []
    assert components.install("materials", components.fetch_manifest("materials"), status=seen.append) == "1.0.0"
    assert calls == [("team", b"pack-v1", "1.0.0")]
    assert components.is_installed("materials") and components.installed_version("materials") == "1.0.0"
    assert {"import": [1, 1]} in seen
    assert (team_materials.component_dir() / team_materials.PACK_FILE).read_bytes() == b"pack-v1"
    _publish(mirror, "1.1.0", b"pack-v2")
    manifest = components.fetch_manifest("materials")
    assert components.is_newer(manifest["version"], components.installed_version("materials"))
    components.install("materials", manifest)
    assert components.installed_version("materials") == "1.1.0"
    assert (team_materials.component_dir() / team_materials.PACK_FILE).read_bytes() == b"pack-v2"
    assert components.uninstall("materials") > 0
    assert not components.is_installed("materials") and not team_materials.component_dir().exists()


def test_tampered_archive_is_rejected_before_import(mirror, monkeypatch):
    monkeypatch.setattr(MaterialLibrary, "sync_pack", lambda *a, **k: pytest.fail("校验失败不应导入"))
    folder = _publish(mirror, "1.0.0", b"pack-v1")
    (folder / "component.zip").write_bytes(b"x" * (folder / "component.zip").stat().st_size)
    with pytest.raises(Exception, match="团队素材包"):
        components.install("materials", components.fetch_manifest("materials"))
    assert not components.is_installed("materials")


def test_manifest_without_pack_file_is_refused():
    with pytest.raises(ValueError, match="不是团队素材包"):
        team_materials.install({"version": "1.0.0", "files": {"pptdoctor-ocr.exe": {}}})


def test_materials_catalog_entry():
    assert components.info("materials").name == "团队素材包"
    assert not components.bundled("materials") and not components.MATERIALS.in_full
    assert components.is_newer("1.1.0", "1.0.0") and not components.is_newer("1.0.0", "1.0.0")
    assert not components.is_newer("", "1.0.0")


# ---------- 打包工具 ----------
def _minimal_pptx(path: Path, rows):
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    REL = "http://schemas.openxmlformats.org/package/2006/relationships"
    with zipfile.ZipFile(path, "w") as z:
        ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(rows)))
        z.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>')
        rels = "".join(f'<Relationship Id="rId{i + 1}" Type="{R}/slide" Target="slides/slide{i + 1}.xml"/>' for i in range(len(rows)))
        z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{REL}">{rels}</Relationships>')
        for i, raw in enumerate(rows, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="{P}"/>')
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels",
                       f'<Relationships xmlns="{REL}"><Relationship Id="rId9" Type="{R}/notesSlide" Target="../notesSlides/notesSlide{i}.xml"/></Relationships>')
            text = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in
                           (MARKER.strip(), json.dumps(raw, ensure_ascii=False).replace("&", "&amp;").replace("<", "&lt;")))
            z.writestr(f"ppt/notesSlides/notesSlide{i}.xml",
                       f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>{text}</p:txBody></p:sp></p:spTree></p:cSld></p:notes>')


def _tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_materials_component", Path(__file__).resolve().parents[1] / "tools" / "build_materials_component.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_tool_writes_verifiable_component(tmp_path):
    pack = tmp_path / "pack.pptx"
    _minimal_pptx(pack, [{"id": "m1", "name": "箭头", "category": "箭头与流程"},
                         {"id": "m2", "name": "卡片", "category": "容器与卡片"},
                         {"id": "m3", "name": "图标", "category": "箭头与流程"}])
    manifest = _tool().build(pack, "1.0.0", tmp_path / "out")
    assert manifest["items"] == 3 and manifest["categories"] == {"箭头与流程": 2, "容器与卡片": 1}
    archive = tmp_path / "out" / "component.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive"]["hash"]
    with zipfile.ZipFile(archive) as z:
        assert z.namelist() == [team_materials.PACK_FILE]
        assert z.read(team_materials.PACK_FILE) == pack.read_bytes()
    on_disk = json.loads((tmp_path / "out" / "component.json").read_text("utf-8"))
    assert on_disk["files"][team_materials.PACK_FILE]["hash"] == hashlib.sha256(pack.read_bytes()).hexdigest()


@pytest.mark.parametrize("rows, match", [
    ([{"id": "m1", "name": "a"}, {"id": "m1", "name": "b"}], "重复"),
    ([{"id": "m1", "name": ""}], "名称"),
])
def test_build_tool_rejects_bad_packs(tmp_path, rows, match):
    pack = tmp_path / "pack.pptx"
    _minimal_pptx(pack, rows)
    with pytest.raises(SystemExit, match=match):
        _tool().build(pack, "1.0.0", tmp_path / "out")
    with pytest.raises(SystemExit, match="X.Y.Z"):
        _tool().build(pack, "v1", tmp_path / "out")


# ---------- 界面：素材库就地下载、组件页「有更新」 ----------
@pytest.fixture
def center(qapp):
    from pptx_finder.ui import component_center as cc
    cc.reset_for_tests()
    c = cc.component_center()
    yield c
    for dlg in list(c._dialogs.values()):
        try:
            dlg.close()
        except RuntimeError:
            pass
    cc.reset_for_tests()


def _items(library, rows):
    for i, (name, category) in enumerate(rows):
        item = Material(f"{i:032x}", name, category, i)
        folder = library.folder(item.id)
        folder.mkdir()
        library._write(folder, item)


def test_material_window_offers_pack_download_in_place(qtbot, center, tmp_path, monkeypatch):
    from pptx_finder.ui.material_window import MaterialWindow
    state = {"installed": False}
    monkeypatch.setattr(components, "is_installed", lambda key: state["installed"])
    calls = []
    monkeypatch.setattr(center, "request", lambda key, parent=None, **kw: calls.append(key))
    library = MaterialLibrary(tmp_path / "lib")
    win = MaterialWindow(library=library)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: not win._tasks)
    assert win.pack_bar.isVisibleTo(win) and win.pack_btn.text() == "下载团队素材包"
    assert "按分类筛选" in win.pack_label.text()
    win.pack_btn.click()
    assert calls == ["materials"]
    # 装好后：横幅消失、列表刷新、分类下拉带数量并能筛选
    _items(library, [("箭头 A", "箭头与流程"), ("箭头 B", "箭头与流程"), ("卡片", "容器与卡片")])
    state["installed"] = True
    center.installed.emit("materials")
    center.state_changed.emit("materials")
    qtbot.waitUntil(lambda: not win._tasks and win.list_widget.count() == 3)
    assert not win.pack_bar.isVisibleTo(win)
    labels = [win.categories.itemText(i) for i in range(win.categories.count())]
    assert labels == ["全部分类", "容器与卡片（1）", "箭头与流程（2）"]
    win.categories.setCurrentIndex(2)
    assert win.list_widget.count() == 2


def test_material_window_shows_import_progress(qtbot, center, tmp_path, monkeypatch):
    from pptx_finder.ui.material_window import MaterialWindow
    monkeypatch.setattr(components, "is_installed", lambda key: False)
    win = MaterialWindow(library=MaterialLibrary(tmp_path / "lib"))
    qtbot.addWidget(win)
    center._states["materials"].update(phase="installing", **{"import": [37, 187]})
    center.state_changed.emit("materials")
    assert "37 / 187" in win.pack_label.text() and win.pack_btn.text() == "查看进度"


def test_settings_row_shows_update_and_starts_upgrade(qtbot, center, monkeypatch):
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager

    monkeypatch.setattr(components, "is_installed", lambda key: key == "materials")
    monkeypatch.setattr(components, "bundled", lambda key: False)
    monkeypatch.setattr(components, "installed_version", lambda key: "1.0.0" if key == "materials" else "")
    monkeypatch.setattr(components, "fetch_manifest", lambda key: {"version": "1.1.0"})
    dlg = SettingsDialog(VersionManager())
    qtbot.addWidget(dlg)
    dlg.show_tab("组件")
    row = dlg._component_rows["materials"]
    qtbot.waitUntil(lambda: "有更新 v1.1.0" in row["status"].text())
    assert "已安装 v1.0.0" in row["status"].text() and row["download"].text() == "更新"
    assert row["remove"].isVisibleTo(dlg)
    started, opened = [], []
    monkeypatch.setattr(center, "start", started.append)
    monkeypatch.setattr(center, "open_dialog", lambda key, parent=None: opened.append(key))
    row["download"].click()
    assert started == ["materials"] and opened == ["materials"]


def test_dialog_offers_update_and_shows_import_progress(qtbot, center, monkeypatch):
    from pptx_finder.ui import component_center as cc
    monkeypatch.setattr(components, "is_installed", lambda key: True)
    monkeypatch.setattr(components, "installed_version", lambda key: "1.0.0")
    center._states["materials"]["latest"] = "1.1.0"
    dlg = cc.ComponentDialog(center, "materials")
    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
    qtbot.addWidget(dlg)
    assert dlg.phase_rendered == "upgradable" and dlg.btn_primary.text() == "更新"
    assert "v1.1.0" in dlg.msg.text() and "自动导入素材库" in dlg.sub.text()
    center._states["materials"].update(phase="installing", **{"import": [5, 187]})
    dlg.refresh()
    assert "正在导入素材库 5 / 187" in dlg.dl_head.text() and dlg.progress.maximum() == 187
    center._states["materials"].update(phase="error", error="团队素材包下载失败")
    dlg.refresh()
    assert not dlg.btn_manual.isVisible() and "完整版" not in dlg.msg.text()
