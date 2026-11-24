"""团队素材包组件：旧版逐页导入（保留兼容）；新版整包直读的下载安装、原子升级与回滚；界面入口。"""
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
from materials_testkit import build_pack_dir, make_pack, rows


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
def _publish(root: Path, out_dir: Path) -> Path:
    """把构建工具产出的 component.json + component.zip 放到模拟备用服务器上。"""
    folder = root / "materials"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("component.json", "component.zip"):
        (folder / name).write_bytes((out_dir / name).read_bytes())
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


def _fresh_process():
    """模拟下一次进程启动（startup_check 每个进程只跑一次）。"""
    team_materials._started = False


def test_install_is_direct_read_and_upgrade_is_atomic_with_deferred_cleanup(mirror, tmp_path, monkeypatch):
    monkeypatch.setattr(MaterialLibrary, "sync_pack", lambda *a, **k: pytest.fail("新版不再逐页导入"))
    _fresh_process()
    assert not components.is_installed("materials")
    out1, _, _ = build_pack_dir(tmp_path, rows(3), "1.2.0")
    _publish(mirror, out1)
    seen = []
    assert components.install("materials", components.fetch_manifest("materials"), status=seen.append) == "1.2.0"
    state = team_materials.read_state()
    assert state["current"]["version"] == "1.2.0" and not state["current"]["confirmed"]
    assert components.is_installed("materials") and components.installed_version("materials") == "1.2.0"
    reader = team_materials.current_reader()
    assert [x.id for x in reader.items] == ["m1", "m2", "m3"] and reader.thumbnail(reader.items[0])
    reader.close()
    # 升级：旧 current 记进 previous，新的未确认
    out2, _, _ = build_pack_dir(tmp_path, rows(5), "1.3.0")
    _publish(mirror, out2)
    components.install("materials", components.fetch_manifest("materials"))
    state = team_materials.read_state()
    assert state["current"]["version"] == "1.3.0" and state["previous"]["version"] == "1.2.0"
    packs = sorted(p.name for p in (team_materials.root() / "packs").iterdir())
    assert len(packs) == 2 and not list((team_materials.root() / "stage").iterdir())
    # 下一次启动校验通过：才确认并清掉 previous
    _fresh_process()
    assert team_materials.startup_check() == "confirmed"
    state = team_materials.read_state()
    assert state["current"]["confirmed"] and state["previous"] is None
    assert len(list((team_materials.root() / "packs").iterdir())) == 1
    assert components.uninstall("materials") > 0
    assert not components.is_installed("materials") and not team_materials.root().exists()


def test_corrupted_current_rolls_back_to_previous_on_start(tmp_path):
    _fresh_process()
    for version, n in (("1.2.0", 2), ("1.3.0", 4)):
        out, _, _ = build_pack_dir(tmp_path, rows(n), version)
        assert team_materials.install_from_file(out / "component.zip")["kind"] == "team"
    state = team_materials.read_state()
    bad = team_materials.root() / "packs" / state["current"]["dir"] / team_materials.PACK_FILE
    bad.write_bytes(b"broken")      # 模拟断电写坏 / 被杀毒软件改动
    _fresh_process()
    assert team_materials.startup_check() == "rolled_back"
    reader = team_materials.current_reader()
    assert reader.version == "1.2.0" and len(reader.items) == 2
    reader.close()


def test_failed_install_keeps_old_library(tmp_path, mirror):
    _fresh_process()
    out1, _, _ = build_pack_dir(tmp_path, rows(2), "1.2.0")
    team_materials.install_from_file(out1 / "component.zip")
    before = team_materials.read_state()
    folder = _publish(mirror, build_pack_dir(tmp_path, rows(3), "1.3.0")[0])
    (folder / "component.zip").write_bytes(b"x" * (folder / "component.zip").stat().st_size)
    with pytest.raises(Exception, match="团队素材包"):
        components.install("materials", components.fetch_manifest("materials"))
    assert team_materials.read_state() == before
    assert not list((team_materials.root() / "stage").iterdir())


def test_index_that_disagrees_with_pack_is_rejected(tmp_path):
    _fresh_process()
    out, _, _ = build_pack_dir(tmp_path, rows(3), "1.2.0")
    stage = tmp_path / "stage"
    with zipfile.ZipFile(out / "component.zip") as z:
        z.extractall(stage)
    index = json.loads((stage / "index.json").read_text("utf-8"))
    index["items"] = index["items"][:2]
    (stage / "index.json").write_text(json.dumps(index), "utf-8")
    with pytest.raises(ValueError, match="不一致"):
        team_materials.activate(stage, "1.2.0")
    assert team_materials.read_state() == {}


def test_legacy_pages_are_cleaned_only_after_next_start(tmp_path, fake_ppt):
    _fresh_process()
    library = MaterialLibrary()
    mine = _own(library)
    library.sync_pack("team", _pack(tmp_path, "v1.pptx", V1), "1.0.0")
    legacy = team_materials.legacy_dir()
    legacy.mkdir(parents=True)
    make_pack(legacy / team_materials.PACK_FILE, rows(3))
    # 旧用户：还没装新版时，团队素材直接读旧原件，缩略图借逐页导入的 preview.png
    reader = team_materials.team_reader()
    assert reader.source == "team" and reader.version == "1.0.0" and len(reader.items) == 3
    out, _, _ = build_pack_dir(tmp_path, rows(4), "1.2.0")
    team_materials.install_from_file(out / "component.zip")
    assert library.pack_record("team")["version"] == "1.0.0" and legacy.exists()   # 当次不清
    _fresh_process()
    assert team_materials.startup_check() == "confirmed"
    assert library.pack_record("team") == {} and not legacy.exists()
    assert [x.id for x in library.list()] == [mine.id]                              # 个人收藏不碰


def test_offline_zip_with_manifest_is_strict_and_never_falls_back(tmp_path):
    """m4 MF-M2-02：旁边有 component.json 就只认清单；篡改后重打 CRC 合法的 zip 也必须拒绝。"""
    _fresh_process()
    out, _, _ = build_pack_dir(tmp_path, rows(3), "1.2.0")
    assert team_materials.install_from_file(out / "component.zip")["version"] == "1.2.0"
    before = team_materials.read_state()
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "component.json").write_bytes((out / "component.json").read_bytes())
    with zipfile.ZipFile(out / "component.zip") as src, zipfile.ZipFile(forged / "component.zip", "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "index.json":
                index = json.loads(data)
                index["version"] = "9.9.9"
                data = json.dumps(index).encode()
            dst.writestr(name, data)
    with pytest.raises(ValueError, match="component.json"):
        team_materials.install_from_file(forged / "component.zip")
    (forged / "component.json").write_text("{bad json", "utf-8")
    with pytest.raises(ValueError, match="合法清单"):
        team_materials.install_from_file(forged / "component.zip")
    assert team_materials.read_state() == before


def test_offline_zip_without_manifest_cannot_block_official_updates(tmp_path):
    _fresh_process()
    out, _, _ = build_pack_dir(tmp_path, rows(3), "9.9.9")
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "同事给的.zip").write_bytes((out / "component.zip").read_bytes())
    assert team_materials.install_from_file(bare / "同事给的.zip")["kind"] == "team"
    assert team_materials.installed_version() == "0.0.0"
    assert components.is_newer("1.2.0", components.installed_version("materials"))


def test_import_plain_material_pptx_is_direct_read(tmp_path):
    _fresh_process()
    pack = make_pack(tmp_path / "同事的形状.pptx", rows(4, "容器与卡片", "x"))
    assert team_materials.install_from_file(pack) == {"kind": "imported", "version": "", "items": 4}
    assert team_materials.install_from_file(pack)["items"] == 4                     # 重复导入不多一份
    readers = team_materials.imported_readers()
    assert len(readers) == 1 and [x.source for x in readers[0].items] == ["imported"] * 4
    team_materials.remove_imported(readers[0])
    assert team_materials.imported_readers() == []
    with pytest.raises(ValueError, match=".pptx"):
        team_materials.install_from_file(tmp_path / "x.txt")


def test_builtin_core_is_fallback_source(tmp_path, monkeypatch):
    _fresh_process()
    _, core, _ = build_pack_dir(tmp_path, rows(8) + rows(2, "整页模板·方案与原理", "w"), "1.2.0", core=True)
    monkeypatch.setattr(materials, "builtin_core_dir", lambda: core)
    reader = team_materials.team_reader()
    assert reader.source == "builtin" and len(reader.items) == 3 and reader.thumbnail(reader.items[0])
    assert not components.is_installed("materials")      # 内置精选不算装了完整库


def test_manifest_without_pack_file_is_refused():
    with pytest.raises(ValueError, match="不是团队素材包"):
        team_materials.install({"version": "1.0.0", "files": {"pptdoctor-ocr.exe": {}}})


def test_materials_catalog_entry():
    assert components.info("materials").name == "团队素材包"
    assert not components.bundled("materials") and not components.MATERIALS.in_full
    assert components.MATERIALS.approx_mb == 5
    assert components.is_newer("1.1.0", "1.0.0") and not components.is_newer("1.0.0", "1.0.0")
    assert not components.is_newer("", "1.0.0")


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


def test_material_tab_fetch_full_library_inline_without_dialog(qtbot, center, tmp_path, monkeypatch):
    from pptx_finder.ui.material_tab import MaterialTabWidget
    _fresh_process()
    _, core, _ = build_pack_dir(tmp_path, rows(8), "1.2.0", core=True)
    monkeypatch.setattr(materials, "builtin_core_dir", lambda: core)
    started, dialogs = [], []
    monkeypatch.setattr(center, "start", started.append)
    monkeypatch.setattr(center, "open_dialog", lambda *a, **k: dialogs.append(a))
    tab = MaterialTabWidget(component_center=center, library=MaterialLibrary(tmp_path / "lib"))
    qtbot.addWidget(tab)
    qtbot.waitUntil(lambda: tab._loaded)
    assert "内置精选 2 条" in tab.pill.text() and tab.fetch_btn.text() == "获取完整库"
    tab.fetch_btn.click()
    assert started == ["materials"] and dialogs == []          # 就地进度，不弹下载窗口
    center._states["materials"].update(phase="downloading", status={"received": 1048576, "total": 3145728})
    center.state_changed.emit("materials")
    assert tab.notice.isVisibleTo(tab) and "1.0 / 3.0 MB" in tab.notice_text.text()
    center._states["materials"].update(phase="error", error="连不上")
    center.state_changed.emit("materials")
    assert "内源暂时连不上" in tab.notice_text.text() and tab.notice_import.isVisibleTo(tab)


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
    assert "v1.1.0" in dlg.msg.text() and "不需要 PowerPoint" in dlg.sub.text()
    center._states["materials"].update(phase="installing", **{"import": [5, 187]})
    dlg.refresh()
    assert "正在导入素材库 5 / 187" in dlg.dl_head.text() and dlg.progress.maximum() == 187
    center._states["materials"].update(phase="error", error="团队素材包下载失败")
    dlg.refresh()
    assert not dlg.btn_manual.isVisible() and "完整版" not in dlg.msg.text()
