"""完整包必须离线可用，且拒绝带损坏运行库的安装源。"""
import importlib.util
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pptx_finder import imgtext_ocr


@pytest.fixture
def builder(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "installer_builder", Path(__file__).resolve().parents[1] / "tools/build_installer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parts = [int(x) for x in module.__version__.split(".")]
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        GetFileVersionInfo=lambda *_: {
            "FileVersionMS": parts[0] << 16 | parts[1], "FileVersionLS": parts[2] << 16}))
    return module


def test_builder_rejects_missing_python_standard_library(builder, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "DIST", tmp_path)
    (tmp_path / builder.EXE_NAME).write_bytes(b"MZ")
    assert builder.check_dist() != 0


@pytest.mark.parametrize("broken", [True, False])
def test_builder_rejects_broken_or_incomplete_standard_library(builder, tmp_path, monkeypatch, broken):
    monkeypatch.setattr(builder, "DIST", tmp_path)
    (tmp_path / builder.EXE_NAME).write_bytes(b"MZ")
    runtime = tmp_path / "_internal"
    runtime.mkdir()
    (runtime / "python312.dll").write_bytes(b"MZ")
    archive = runtime / "base_library.zip"
    if broken:
        archive.write_bytes(b"bad zip")
    else:
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("unrelated.pyc", b"no encodings")
    assert builder.check_dist() != 0


def test_full_package_uses_bundled_ocr_without_download(tmp_path, monkeypatch, component):
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: tmp_path / "userdata/ocr")
    monkeypatch.setattr(imgtext_ocr, "data_dir", lambda: tmp_path / "userdata")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    bundle = tmp_path / "_internal/ocr"
    shutil.copytree(component[0], bundle)
    def no_network(*a, **kw):
        pytest.fail("完整包不能联网下载")
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    cached = tmp_path / "userdata/ocr-bundled"
    assert imgtext_ocr.is_installed()
    assert not cached.exists(), "UI 检查组件状态不能触发解包"
    assert imgtext_ocr.command() == [str(cached / imgtext_ocr.SIDE_CAR_EXE)]
    assert imgtext_ocr.installed_version() == "1.0.0"
    assert imgtext_ocr.component_size_bytes() > 0
    assert str(cached) in imgtext_ocr.self_test()
    assert imgtext_ocr.uninstall() is False
    assert (bundle / "component.zip").exists()
    assert (cached / imgtext_ocr.SIDE_CAR_EXE).exists()
    monkeypatch.setattr(imgtext_ocr, "install", lambda *a, **kw: pytest.fail("重复解包"))
    assert imgtext_ocr.command() == [str(cached / imgtext_ocr.SIDE_CAR_EXE)]


def test_full_package_prefers_bundle_over_old_download(tmp_path, monkeypatch, component):
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    cached = tmp_path / "downloaded"
    cached.mkdir()
    (cached / imgtext_ocr.SIDE_CAR_EXE).write_bytes(b"old")
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: cached)
    monkeypatch.setattr(imgtext_ocr, "data_dir", lambda: tmp_path / "userdata")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    bundle = tmp_path / "_internal/ocr"
    shutil.copytree(component[0], bundle)
    target = tmp_path / "userdata/ocr-bundled"
    assert imgtext_ocr.command() == [str(target / imgtext_ocr.SIDE_CAR_EXE)]
    assert (cached / imgtext_ocr.SIDE_CAR_EXE).read_bytes() == b"old"


@pytest.fixture
def component(tmp_path):
    root = tmp_path / "component"
    root.mkdir()
    import io
    library = io.BytesIO()
    with zipfile.ZipFile(library, "w") as z:
        for name in ("__init__", "aliases", "utf_8"):
            z.writestr(f"encodings/{name}.pyc", b"test bytecode")
    files = {"pptdoctor-ocr.exe": b"MZ-test", "_internal/python312.dll": b"MZ-test",
             "_internal/base_library.zip": library.getvalue(), "models/rec.onnx": b"model"}
    archive = root / "component.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for rel, payload in files.items():
            z.writestr(rel, payload)
    manifest = {"version": "1.0.0", "files": {
        rel: {"size": len(payload), "hash": hashlib.sha256(payload).hexdigest()}
        for rel, payload in files.items()}, "archive": {
            "name": archive.name, "size": archive.stat().st_size,
            "hash": hashlib.sha256(archive.read_bytes()).hexdigest()}}
    (root / "component.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root, manifest


def test_bundle_extracts_verified_sidecar_separately(builder, component, tmp_path):
    root, manifest = component
    dist = tmp_path / "dist"
    out = builder.bundle_ocr(root, dist)
    assert out == dist / "_internal/ocr"
    assert (out / "component.zip").read_bytes() == (root / "component.zip").read_bytes()
    assert all(len("PPT-Doctor/" + p.relative_to(dist).as_posix()) <= 85
               for p in dist.rglob("*") if p.is_file())
    assert not (dist / "_internal/python312.dll").exists()
    assert json.loads((out / "component.json").read_text("utf-8")) == manifest
    assert builder.bundle_ocr(root, dist) == out


@pytest.mark.parametrize("damage", ["archive", "file", "missing", "escape"])
def test_bundle_rejects_bad_payload_without_installing(builder, component, tmp_path, damage):
    root, manifest = component
    if damage == "archive":
        manifest["archive"]["hash"] = "0" * 64
    elif damage == "file":
        manifest["files"]["pptdoctor-ocr.exe"]["hash"] = "0" * 64
    elif damage == "missing":
        manifest["files"]["absent.dll"] = {"size": 1, "hash": "0" * 64}
    else:
        manifest["archive"]["name"] = "../component.zip"
    (root / "component.json").write_text(json.dumps(manifest), encoding="utf-8")
    dist = tmp_path / "dist"
    with pytest.raises(ValueError):
        builder.bundle_ocr(root, dist)
    assert not (dist / "_internal/ocr").exists()


def test_standard_package_keeps_using_downloaded_component(tmp_path, monkeypatch):
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    cached = tmp_path / "cached"
    cached.mkdir()
    (cached / imgtext_ocr.SIDE_CAR_EXE).write_bytes(b"old")
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: cached)
    assert imgtext_ocr.command() == [str(cached / imgtext_ocr.SIDE_CAR_EXE)]


def test_corrupt_bundled_archive_fails_without_network_or_partial_cache(tmp_path, monkeypatch, component):
    root, _ = component
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(imgtext_ocr, "data_dir", lambda: tmp_path / "userdata")
    bundle = tmp_path / "_internal/ocr"
    shutil.copytree(root, bundle)
    (bundle / "component.zip").write_bytes(b"damaged")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: pytest.fail("不能下载兜底"))
    with pytest.raises(imgtext_ocr.OcrUnavailable, match="校验失败"):
        imgtext_ocr.command()
    assert not (tmp_path / "userdata/ocr-bundled").exists()


def test_full_package_replaces_cache_from_different_bundle(tmp_path, monkeypatch, component):
    root, manifest = component
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(imgtext_ocr, "data_dir", lambda: tmp_path / "userdata")
    shutil.copytree(root, tmp_path / "_internal/ocr")
    cached = tmp_path / "userdata/ocr-bundled"
    cached.mkdir(parents=True)
    (cached / "component.json").write_text(json.dumps({"archive_hash": "old"}))
    (cached / imgtext_ocr.SIDE_CAR_EXE).write_bytes(b"old-exe")
    imgtext_ocr.command()
    assert (cached / imgtext_ocr.SIDE_CAR_EXE).read_bytes() == b"MZ-test"
    assert json.loads((cached / "component.json").read_text("utf-8"))["archive_hash"] == manifest["archive"]["hash"]
