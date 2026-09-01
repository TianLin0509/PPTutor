# -*- coding: utf-8 -*-
"""自动更新不能只有一个源，而且必须挑**版本最高**的源。

背景（2026-09-01 实测，全部有 HTTP 证据）：

    https://me.lt-stockpartner.tech/pptutor/manifest.json   200，版本 = 1.2.7
    当前发布版本                                              1.5.5

`compare()` 只在远端版本更高时才返回更新。于是：
  - >=1.2.7 的用户**永远收不到更新**，而且没有任何报错，是静默死的；
  - <1.2.7 的用户会被"更新"到 1.2.7，然后卡在那里。

这很可能就是"很多用户在用老版本"的机制性原因之一。

同一天还测出：「图片转文字」的识别组件早就是双源——主源 `/pptutor/ocr/component.json`
返回 404（从来没上传过），实际一直靠 GitHub Release 回落在服务用户。也就是说
GitHub Release 这条通道已经被真实用户（含公司内网机器）验证过可达。

所以这里钉三件事：
  1. 一定有 GitHub Release 这个第二源；
  2. 选源按**版本最高**，不是"第一个连得上"（否则一台不更新的服务器就能拖死全部）；
  3. 块取不到时回落整包（落后好几版的用户需要的块不会随最新一版发布）。
"""
from __future__ import annotations

import json
import urllib.error
import zipfile

import pytest

from pptx_finder import updater


BASE = "https://example.invalid/pptutor"


def _manifest(version: str, files: dict) -> dict:
    return {"version": version, "notes": "", "files": {
        name: {"hash": h, "size": size} for name, (h, size) in files.items()}}


LOCAL = _manifest("1.5.0", {"app.exe": ("a" * 64, 10), "_internal/x.dll": ("b" * 64, 20)})


# ---- 源的构成 ----

def test_there_is_always_a_github_source_even_without_a_base_url():
    labels = [s.label for s in updater.update_sources("")]
    assert "github-release" in labels


def test_self_hosted_comes_first_but_github_is_always_present():
    sources = updater.update_sources(BASE)
    assert [s.label for s in sources] == ["self-hosted", "github-release"]


def test_source_urls_have_the_expected_shape():
    self_hosted, github = updater.update_sources(BASE)
    assert self_hosted.manifest_url == BASE + "/manifest.json"
    assert self_hosted.block("f" * 64) == BASE + "/files/" + "f" * 64
    # GitHub 的资产是平铺的，没有 files/ 子目录
    assert github.manifest_url.endswith("/latest/download/manifest.json")
    assert github.block("f" * 64).endswith("/latest/download/" + "f" * 64)
    assert "/files/" not in github.block("f" * 64)
    assert github.package("1.5.5").endswith("/PPT-Doctor-v1.5.5.zip")


def test_only_github_offers_a_whole_package_fallback():
    self_hosted, github = updater.update_sources(BASE)
    assert self_hosted.package("1.5.5") == ""
    assert github.package("1.5.5")


# ---- 选源：版本最高，而不是第一个连得上 ----

def _patch_sources(monkeypatch, per_source: dict):
    """per_source: {label: manifest 或 Exception}"""
    def fake_fetch(source, timeout=None, response_callback=None):
        got = per_source[source.label]
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(updater, "fetch_manifest_from", fake_fetch)
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "local_manifest", lambda: LOCAL)


def test_a_stale_self_hosted_source_no_longer_blocks_updates(monkeypatch):
    """正是线上的真实状况：自建源停在 1.2.7，GitHub 上是 1.5.5。"""
    _patch_sources(monkeypatch, {
        "self-hosted": _manifest("1.2.7", {"app.exe": ("c" * 64, 10)}),
        "github-release": _manifest("1.5.5", {"app.exe": ("d" * 64, 10)}),
    })
    info = updater.check_for_update(BASE)
    assert info is not None, "自建源过期就收不到更新 —— 正是要修的那个 bug"
    assert info.version == "1.5.5"
    assert info.source.label == "github-release"


def test_the_newest_source_wins_even_when_it_is_listed_first(monkeypatch):
    _patch_sources(monkeypatch, {
        "self-hosted": _manifest("2.0.0", {"app.exe": ("c" * 64, 10)}),
        "github-release": _manifest("1.5.5", {"app.exe": ("d" * 64, 10)}),
    })
    info = updater.check_for_update(BASE)
    assert info.version == "2.0.0"
    assert info.source.label == "self-hosted"


def test_one_unreachable_source_does_not_break_the_check(monkeypatch):
    _patch_sources(monkeypatch, {
        "self-hosted": OSError("DNS 挂了"),
        "github-release": _manifest("1.5.5", {"app.exe": ("d" * 64, 10)}),
    })
    info = updater.check_for_update(BASE)
    assert info is not None and info.version == "1.5.5"


def test_all_sources_unreachable_is_silent(monkeypatch):
    """检查失败不该打扰用户。"""
    _patch_sources(monkeypatch, {
        "self-hosted": OSError("x"), "github-release": OSError("y")})
    assert updater.check_for_update(BASE) is None


def test_no_update_when_every_source_is_older(monkeypatch):
    _patch_sources(monkeypatch, {
        "self-hosted": _manifest("1.2.7", {"app.exe": ("c" * 64, 10)}),
        "github-release": _manifest("1.4.0", {"app.exe": ("d" * 64, 10)}),
    })
    assert updater.check_for_update(BASE) is None


# ---- 下载：块取不到就整包 ----

def test_a_missing_block_raises_the_fallback_signal(monkeypatch, tmp_path):
    info = updater.UpdateInfo(
        version="1.5.5", notes="", changed=[("app.exe", "d" * 64, 10)], deleted=[],
        raw={"version": "1.5.5", "files": {}},
        source=updater.update_sources(BASE)[1])

    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    with pytest.raises(updater.BlockUnavailableError):
        updater.download_delta(BASE, info, tmp_path / "staging")


def test_a_real_http_error_is_not_swallowed_as_fallback(monkeypatch, tmp_path):
    """500 是服务端故障，不该被当成「换整包」——那会掩盖真问题。"""
    info = updater.UpdateInfo(
        version="1.5.5", notes="", changed=[("app.exe", "d" * 64, 10)], deleted=[],
        raw={}, source=updater.update_sources(BASE)[1])

    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "Server Error", None, None)

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        updater.download_delta(BASE, info, tmp_path / "staging")


def test_package_fallback_extracts_and_verifies(monkeypatch, tmp_path):
    import hashlib
    import io

    payload = b"new exe bytes"
    digest = hashlib.sha256(payload).hexdigest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PPT-Doctor/app.exe", payload)
        z.writestr("PPT-Doctor/_internal/unrelated.dll", b"not needed")
    blob = buf.getvalue()

    info = updater.UpdateInfo(
        version="1.5.5", notes="", changed=[("app.exe", digest, len(payload))],
        deleted=[], raw={"version": "1.5.5", "files": {}},
        source=updater.update_sources(BASE)[1])

    class _Resp:
        headers = {"Content-Length": str(len(blob))}

        def __init__(self):
            self._b = io.BytesIO(blob)

        def read(self, n=-1):
            return self._b.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda url, timeout=None: _Resp())
    staging = tmp_path / "staging"
    updater.download_package(info, staging)

    assert (staging / "app.exe").read_bytes() == payload
    # 只解本次要换的文件，不该把整包铺开
    assert not (staging / "_internal" / "unrelated.dll").exists()
    assert json.loads((staging / "manifest.json").read_text(encoding="utf-8"))["version"] == "1.5.5"
    assert not (staging / "_package.zip").exists(), "临时 zip 没清掉"


def test_package_fallback_rejects_a_tampered_payload(monkeypatch, tmp_path):
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PPT-Doctor/app.exe", b"tampered")
    blob = buf.getvalue()

    info = updater.UpdateInfo(
        version="1.5.5", notes="", changed=[("app.exe", "d" * 64, 8)], deleted=[],
        raw={}, source=updater.update_sources(BASE)[1])

    class _Resp:
        headers: dict = {}

        def __init__(self):
            self._b = io.BytesIO(blob)

        def read(self, n=-1):
            return self._b.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda url, timeout=None: _Resp())
    with pytest.raises(ValueError, match="哈希校验失败"):
        updater.download_package(info, tmp_path / "staging")


def test_package_fallback_cannot_escape_staging():
    r"""zip-slip：整包里若有 `../` 条目，绝不能写到 staging 之外。"""
    info = updater.UpdateInfo(
        version="1.5.5", notes="",
        changed=[("../../evil.dll", "d" * 64, 8)], deleted=[], raw={},
        source=updater.update_sources(BASE)[1])
    with pytest.raises(updater.UnsafeManifestError):
        updater.download_package(info, "staging")


def test_ui_calls_the_fallback_aware_entry_point():
    """回归锁：别有人改回只走 download_delta。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "pptx_finder" / "ui"
           / "update_ui.py").read_text(encoding="utf-8")
    assert "updater.download_update(" in src
    assert "updater.download_delta(" not in src
