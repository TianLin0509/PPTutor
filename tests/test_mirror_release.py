"""发布镜像工具：只验证「收集哪些文件、按什么顺序、哈希是否自洽」，不连服务器。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("mirror_release", ROOT / "tools" / "mirror_release.py")
mirror = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402
sys.modules["mirror_release"] = mirror  # dataclass 需要能按模块名找到自己
_spec.loader.exec_module(mirror)


def _layout(tmp_path: Path, version="2.0.0", tamper=False) -> Path:
    src = tmp_path / "assets"
    (src / "ocr").mkdir(parents=True)
    block = b"new exe bytes"
    h = hashlib.sha256(block).hexdigest()
    (src / h).write_bytes(b"tampered" if tamper else block)
    for name in mirror.MANIFESTS:
        (src / name).write_text(json.dumps({"version": version, "files": {}}), "utf-8")
    (src / f"PPT-Doctor-v{version}-Base.zip").write_bytes(b"zip")
    (src / "PPT-Doctor-Setup-v2.0.0-Full.exe").write_bytes(b"not for updater")
    (src / "ocr" / "component.zip").write_bytes(b"ocr zip")
    (src / "ocr" / "component.json").write_text("{}", "utf-8")
    return src


def test_collect_orders_manifests_last_and_skips_installers(tmp_path):
    items = mirror.collect_from_dir("2.0.0", _layout(tmp_path), components=True)
    remotes = {i.remote for i in items}
    assert "manifest-Base.json" in remotes and "PPT-Doctor-v2.0.0-Base.zip" in remotes
    assert any(r.startswith("files/") for r in remotes)
    assert "ocr/component.zip" in remotes and "ocr/component.json" in remotes
    assert not any("Setup" in r for r in remotes)
    last = {i.remote for i in items if i.last}
    assert last == set(mirror.MANIFESTS) | {"ocr/component.json"}
    mirror.check_consistency(items)


def test_block_name_must_match_its_content(tmp_path):
    items = mirror.collect_from_dir("2.0.0", _layout(tmp_path, tamper=True), components=False)
    with pytest.raises(SystemExit):
        mirror.check_consistency(items)
