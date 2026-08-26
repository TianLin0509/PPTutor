"""识别组件（OCR 侧车）的契约与安装链路。

不下载真组件：侧车用一个几行的假脚本顶替，安装用内存里的假清单与假响应。
要守住的是三件事——没装时报得清楚、协议对得上、装不成功绝不留半套。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from pptx_finder import imgtext_ocr, updater

FAKE_SIDECAR = '''
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("--request", required=True)
p.add_argument("--response", required=True)
a = p.parse_args()
req = json.load(open(a.request, encoding="utf-8"))
out = {"ok": True, "version": "test", "results": {
    path: [{"text": "\\u6d4b\\u8bd5", "box": [1, 2, 3, 4], "score": 0.9}]
    for path in req["images"]}}
json.dump(out, open(a.response, "w", encoding="utf-8"), ensure_ascii=False)
'''

BROKEN_SIDECAR = 'import sys; sys.stderr.write("boom"); sys.exit(3)\n'


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    def install(source: str):
        script = tmp_path / "fake_sidecar.py"
        script.write_text(source, encoding="utf-8")
        import sys
        monkeypatch.setenv("PPTUTOR_OCR_CMD",
                           json.dumps([sys.executable, str(script)]))
        return script
    return install


def test_reports_not_installed_without_component(monkeypatch, tmp_path):
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: tmp_path / "nope")
    assert imgtext_ocr.is_installed() is False
    assert "未安装" in imgtext_ocr.self_test()
    with pytest.raises(imgtext_ocr.OcrUnavailable):
        imgtext_ocr.recognize_one(tmp_path / "x.png")


def test_recognize_round_trips_through_sidecar(sidecar, tmp_path):
    sidecar(FAKE_SIDECAR)
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    rows = imgtext_ocr.recognize_one(image)
    assert rows == [{"text": "测试", "box": (1, 2, 3, 4), "score": 0.9}]


def test_recognize_batches_multiple_images_in_one_call(sidecar, tmp_path):
    """一次调用处理多张，模型加载只付一次——批量转换靠这个不退化。"""
    sidecar(FAKE_SIDECAR)
    paths = []
    for name in ("a.png", "b.png", "c.png"):
        p = tmp_path / name
        p.write_bytes(b"x")
        paths.append(p)
    result = imgtext_ocr.recognize(paths)
    assert len(result) == 3
    assert all(len(rows) == 1 for rows in result.values())


def test_sidecar_failure_surfaces_as_unavailable_not_empty_result(sidecar, tmp_path):
    """侧车挂了要说「组件不可用」，不能装作「这张图没有文字」。"""
    sidecar(BROKEN_SIDECAR)
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    with pytest.raises(imgtext_ocr.OcrUnavailable) as excinfo:
        imgtext_ocr.recognize_one(image)
    assert "3" in str(excinfo.value)


# ---------- 安装 ----------
def _make_component(tmp_path):
    payload = {"pptdoctor-ocr.exe": b"MZ-fake-exe", "models/det.onnx": b"onnx-bytes"}
    archive = tmp_path / "component.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for rel, data in payload.items():
            z.writestr(rel, data)
    manifest = {
        "version": "9.9.9",
        "files": {rel: {"hash": hashlib.sha256(d).hexdigest(), "size": len(d)}
                  for rel, d in payload.items()},
        "archive": {"name": "component.zip",
                    "hash": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "size": archive.stat().st_size},
    }
    return manifest, archive


@pytest.fixture
def local_component(tmp_path, monkeypatch):
    manifest, archive = _make_component(tmp_path)
    target = tmp_path / "installed"
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: target)
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)

    def fake_urlopen(url, *_args, **_kwargs):
        assert str(url).endswith("component.zip")
        return io.BytesIO(archive.read_bytes())

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return manifest, archive, target


def test_install_writes_component_and_records_version(local_component):
    manifest, _archive, target = local_component
    version = imgtext_ocr.install(manifest)
    assert version == "9.9.9"
    assert (target / "pptdoctor-ocr.exe").read_bytes() == b"MZ-fake-exe"
    assert (target / "models" / "det.onnx").is_file()
    assert imgtext_ocr.is_installed()


def test_install_rejects_tampered_archive_and_keeps_previous(local_component):
    """校验不过时必须整批放弃，且已装好的旧组件一个字节不动。"""
    manifest, _archive, target = local_component
    imgtext_ocr.install(manifest)
    before = (target / "pptdoctor-ocr.exe").read_bytes()

    tampered = dict(manifest)
    tampered["archive"] = dict(manifest["archive"], hash="0" * 64)
    with pytest.raises(imgtext_ocr.OcrUnavailable):
        imgtext_ocr.install(tampered)
    assert (target / "pptdoctor-ocr.exe").read_bytes() == before


def test_install_rejects_escaping_paths(local_component):
    """zip 解压的路径穿越是老掉牙但依然有效的攻击面，必须在解压前就拒。"""
    manifest, _archive, _target = local_component
    evil = dict(manifest)
    evil["files"] = dict(manifest["files"])
    evil["files"]["../../evil.dll"] = {"hash": "a" * 64, "size": 1}
    with pytest.raises(updater.UnsafeManifestError):
        imgtext_ocr.install(evil)


def test_install_rejects_non_hex_hashes(local_component):
    manifest, _archive, _target = local_component
    evil = dict(manifest)
    evil["files"] = dict(manifest["files"])
    evil["files"]["pptdoctor-ocr.exe"] = {"hash": "../../secret", "size": 1}
    with pytest.raises(imgtext_ocr.OcrUnavailable):
        imgtext_ocr.install(evil)


def test_install_rejects_archive_entries_outside_manifest(local_component, tmp_path,
                                                          monkeypatch):
    """包里出现清单没声明的文件 -> 拒绝。防止「清单干净、包里夹带」。"""
    manifest, _archive, _target = local_component
    sneaky = tmp_path / "sneaky.zip"
    with zipfile.ZipFile(sneaky, "w") as z:
        z.writestr("pptdoctor-ocr.exe", b"MZ-fake-exe")
        z.writestr("models/det.onnx", b"onnx-bytes")
        z.writestr("extra.dll", b"surprise")
    manifest = dict(manifest)
    manifest["archive"] = dict(manifest["archive"],
                               hash=hashlib.sha256(sneaky.read_bytes()).hexdigest(),
                               size=sneaky.stat().st_size)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *_a, **_k: io.BytesIO(sneaky.read_bytes()))
    with pytest.raises(imgtext_ocr.OcrUnavailable):
        imgtext_ocr.install(manifest)


def test_install_can_be_cancelled_without_leaving_partial_component(local_component):
    manifest, _archive, target = local_component
    with pytest.raises(InterruptedError):
        imgtext_ocr.install(manifest, cancel=lambda: True)
    assert not target.exists()
