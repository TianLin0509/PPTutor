"""2026-11-23：公司内源排第一的更新源 + 覆盖安装没换掉主程序时的自愈。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from pptx_finder import openx_source, updater

API = "https://openx.huawei.com/openxApi/project/version/release/query/"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    """按 URL 返回内源接口的 JSON；记下调用次数。"""

    def __init__(self, releases, files, fail=False):
        self.releases = releases          # [(id, version)]
        self.files = files                # id -> [(name, url)]
        self.fail = fail
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        if self.fail:
            raise OSError("getaddrinfo failed")
        if "by_project_id" in url:
            data = {"records": [{"id": i, "version": v, "releaseTime": ""} for i, v in self.releases]}
        else:
            rid = int(url.rsplit("=", 1)[1])
            data = {"binaryPackages": [{"originalFileName": n, "url": u} for n, u in self.files.get(rid, [])]}
        return _Resp(json.dumps({"code": "0", "data": data}).encode())


def _index(opener):
    return openx_source.OpenxIndex(api=API, opener=opener)


F = "https://file.openx.huawei.com/openx-file/"


def test_latest_manifest_comes_from_newest_update_release():
    op = FakeOpener([(1, "v5.6.91"), (2, "ppt-doctor-update-2.1.9"), (3, "ppt-doctor-update-2.1.10"),
                     (4, "ppt-doctor-2.0.7")],
                    {3: [("manifest-Base.json", F + "m10")], 2: [("manifest-Base.json", F + "m9")]})
    url, ver = _index(op).latest_file("manifest-Base.json")
    assert (url, ver) == (F + "m10", "2.1.10")    # 2.1.10 > 2.1.9：按数字比，不按字符串


def test_find_searches_older_releases_and_rejects_foreign_hosts():
    op = FakeOpener([(2, "ppt-doctor-update-2.1.9"), (3, "ppt-doctor-update-2.1.10")],
                    {3: [("aaa", "https://evil.example.com/aaa")], 2: [("AAA", F + "a9"), ("bbb", F + "b9")]})
    idx = _index(op)
    assert idx.find("aaa") == F + "a9"            # 新版本里那个地址不在内源域名下，不认；落到旧版本
    with pytest.raises(openx_source.OpenxUnavailable):
        idx.find("zzz")


def test_unreachable_is_remembered_so_outside_users_are_not_slowed_down():
    op = FakeOpener([], {}, fail=True)
    idx = _index(op)
    with pytest.raises(openx_source.OpenxUnavailable):
        idx.releases()
    n = len(op.calls)
    with pytest.raises(openx_source.OpenxUnavailable):
        idx.latest_file("manifest.json")
    assert len(op.calls) == n                     # 5 分钟内不再真去连


@pytest.fixture
def ui_settings(monkeypatch):
    """每个用例一份干净的 ui.json（外网源开关存在这里）。"""
    store = {}
    from pptx_finder import config
    monkeypatch.setattr(config, "load_ui_settings", lambda: dict(store))
    monkeypatch.setattr(config, "update_ui_settings", lambda **kw: store.update(kw))
    monkeypatch.delenv("PPTX_FINDER_UPDATE_EXTERNAL", raising=False)
    return store


def test_openx_is_the_only_source_by_default(monkeypatch, ui_settings):
    """2026-11-24 田哥定：打包版默认只走内源，GitHub / 备用服务器连问都不问。"""
    base = "https://me.lt-stockpartner.tech/pptutor"
    monkeypatch.setattr(updater, "_is_default_base", lambda b: True)
    monkeypatch.setenv("PPTX_FINDER_OPENX", "0")                      # 内源关掉（开发/单测）：只剩外网源
    assert [s.label for s in updater.update_sources(base)] == ["github-release", "self-hosted"]
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    srcs = updater.update_sources(base, edition="base")
    assert [s.label for s in srcs] == ["openx"]
    assert srcs[0].name == "公司内源" and srcs[0].manifest_file == "manifest-Base.json"
    openx_source.set_external_allowed(True)                            # 用户手动打开外网源
    assert [s.label for s in updater.update_sources(base)] == ["openx", "github-release", "self-hosted"]
    openx_source.set_external_allowed(False)
    assert [s.label for s in updater.update_sources(base)] == ["openx"]


def test_explicit_test_url_is_still_honoured_without_external(monkeypatch, ui_settings):
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    srcs = updater.update_sources("http://127.0.0.1:8765/pptutor")
    assert [s.label for s in srcs] == ["openx", "self-hosted"]         # 显式地址照用，GitHub 不加


def _two_manifests():
    local = {"version": "2.1.9", "files": {"PPT-Doctor.exe": {"hash": "a" * 64, "size": 3}}}
    remote = {"version": "2.1.10", "files": {"PPT-Doctor.exe": {"hash": "b" * 64, "size": 3}}}
    return local, remote


def test_check_only_asks_openx_and_never_falls_back_silently(monkeypatch, ui_settings):
    local, remote = _two_manifests()
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "local_manifest", lambda: local)
    asked = []

    def fake_fetch(source, timeout=8, response_callback=None):
        asked.append(source.label)
        if source.label == "openx":
            if fake_fetch.openx_down:
                raise openx_source.OpenxUnavailable("公司内源连不上")
            return remote
        return dict(remote)
    monkeypatch.setattr(updater, "fetch_manifest_from", fake_fetch)
    base = "https://me.lt-stockpartner.tech/pptutor"

    fake_fetch.openx_down = False
    rep = updater.check_sources(base)
    assert rep.best.version == "2.1.10"
    assert [s.label for s in rep.best.sources] == ["openx"]          # 下载也只走内源
    assert asked == ["openx"]                                         # 外网源连问都没问

    asked.clear()
    fake_fetch.openx_down = True                                      # 内源连不上：报错，不偷偷换外网
    rep = updater.check_sources(base)
    assert rep.best is None and asked == ["openx"]
    assert rep.openx_only_failed and "改用外网更新源" in rep.error
    assert [c.label for c in rep.checks] == ["openx"]                 # 内源这一条显示成失败

    asked.clear()
    openx_source.set_external_allowed(True)                           # 用户点了「改用外网更新源」
    rep = updater.check_sources(base)
    assert rep.best.version == "2.1.10" and rep.best.sources[0].label != "openx"
    assert "openx" not in [c.label for c in rep.checks]               # 这时内源连不上不再列成失败
    assert not rep.error and not rep.openx_only_failed


def test_release_page_is_openx_unless_external(monkeypatch, ui_settings):
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    assert openx_source.release_page(True) == openx_source.PAGE_URL
    openx_source.set_external_allowed(True)
    assert "github.com" in openx_source.release_page(True)


def test_components_only_use_openx_by_default(monkeypatch, ui_settings):
    from pptx_finder import imgtext_ocr
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    monkeypatch.delenv("PPTX_FINDER_UPDATE_URL", raising=False)
    assert imgtext_ocr.component_sources("formula", "https://github.com/x") == [("openx", "openx:formula")]
    openx_source.set_external_allowed(True)
    assert [lbl for lbl, _b in imgtext_ocr.component_sources("formula", "https://github.com/x")] == [
        "openx", "github-release", "self-hosted"]


def test_component_manifest_and_split_archive_from_openx(tmp_path, monkeypatch, ui_settings):
    """公式组件 436 MB 超过内源单附件上限，切成 .000/.001 上传；下载时拼回、整体核 sha256。"""
    from pptx_finder import imgtext_ocr, netfetch
    whole = b"PART-ONE|" * 10 + b"PART-TWO"
    a, b = whole[:50], whole[50:]
    manifest = {"version": "1.0.0", "files": {"x.exe": {"hash": "e" * 64}},
                "archive": {"name": "component.zip", "hash": hashlib.sha256(whole).hexdigest(), "size": len(whole)}}
    op = FakeOpener([(7, "ppt-doctor-formula-1.0.0"), (6, "ppt-doctor-formula-0.9.0"), (5, "ppt-doctor-update-2.1.14")],
                    {7: [("component.json", F + "m"), ("component.zip.000", F + "p0"), ("component.zip.001", F + "p1")],
                     6: [("component.json", F + "old")]})
    monkeypatch.setattr(openx_source, "INDEX", openx_source.MultiIndex([_index(op)]))
    assert openx_source.component_manifest_url("formula") == F + "m"          # 取最新版本的清单
    assert openx_source.component_archive_urls("formula", "1.0.0", "component.zip") == [F + "p0", F + "p1"]
    with pytest.raises(openx_source.OpenxUnavailable):
        openx_source.component_archive_urls("formula", "0.9.0", "component.zip")

    bodies = {F + "p0": a, F + "p1": b, F + "m": json.dumps(manifest).encode()}
    opened = []

    class _R(_Resp):
        headers = {"Content-Type": "application/octet-stream"}

        def geturl(self):
            return self.u

    def fake_urlopen(req, timeout=None):
        u = req.full_url if hasattr(req, "full_url") else str(req)
        opened.append(u)
        r = _R(bodies[u])
        r.u = u
        return r
    monkeypatch.setattr(netfetch.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "component.zip"
    url = imgtext_ocr.archive_url("openx:formula", "1.0.0", "component.zip")
    netfetch.download_with_fallback([("openx", url)], dest, expect_size=len(whole),
                                    expect_sha256=manifest["archive"]["hash"], what="公式组件")
    assert dest.read_bytes() == whole and opened == [F + "p0", F + "p1"]

    bodies[F + "p1"] = b"TAMPERED"                                            # 校验不过：这个源失败，不留半个文件
    with pytest.raises(netfetch.AllSourcesFailed):
        netfetch.download_with_fallback([("openx", url)], tmp_path / "bad.zip", expect_size=None,
                                        expect_sha256=manifest["archive"]["hash"], what="公式组件")
    assert not (tmp_path / "bad.zip").exists() and not (tmp_path / "bad.zip.part").exists()


def test_component_download_redirected_off_openx_is_refused(tmp_path, monkeypatch):
    from pptx_finder import netfetch

    class _R(_Resp):
        headers = {"Content-Type": "application/octet-stream"}

        def geturl(self):
            return "https://evil.example.com/x"
    monkeypatch.setattr(netfetch.urllib.request, "urlopen", lambda u, timeout=None: _R(b"abc"))
    with pytest.raises(netfetch.AllSourcesFailed) as ei:
        netfetch.download_with_fallback([("openx", F + "x")], tmp_path / "x", expect_size=3,
                                        expect_sha256=hashlib.sha256(b"abc").hexdigest())
    assert "内源以外" in ei.value.attempts[0].reason


def test_download_blocks_from_openx(tmp_path, monkeypatch):
    payload = b"NEW"
    h = hashlib.sha256(payload).hexdigest()
    op = FakeOpener([(3, "ppt-doctor-update-2.1.10")], {3: [(h, F + "blk")]})
    idx = _index(op)
    monkeypatch.setattr(openx_source, "INDEX", idx)
    src = updater.openx_source("full")
    assert src.block(h) == F + "blk"
    with pytest.raises(updater.BlockUnavailableError):
        src.block("c" * 64)
    with pytest.raises(updater.BlockUnavailableError):
        src.package("2.1.10")                                       # 完整版整包超过内源单附件上限，没放


def test_manifest_claiming_newer_version_is_recomputed_from_disk(tmp_path):
    """公司电脑实测：exe 被占没换掉，清单却写成了 2.1.9；以正在运行的版本号为准重算。"""
    root = tmp_path / "PPT Doctor"
    (root / "_internal").mkdir(parents=True)
    (root / "PPT-Doctor.exe").write_bytes(b"OLD-EXE")
    (root / "_internal" / "lib.dll").write_bytes(b"NEW-LIB")
    (root / "unins000.exe").write_bytes(b"uninstaller")            # 安装器自己的文件，不收进清单
    claimed = {"version": "2.1.9", "edition": "base", "files": {
        "PPT-Doctor.exe": {"hash": "f" * 64, "size": 9},
        "_internal/lib.dll": {"hash": hashlib.sha256(b"NEW-LIB").hexdigest(), "size": 7},
        "_internal/gone.dll": {"hash": "e" * 64, "size": 1}}}
    out = root / "manifest.json"
    fixed = updater.reconcile_manifest(claimed, root, "2.1.0", write_to=out)
    assert fixed["version"] == "2.1.0" and fixed["edition"] == "base"
    assert fixed["files"]["PPT-Doctor.exe"]["hash"] == hashlib.sha256(b"OLD-EXE").hexdigest()
    assert set(fixed["files"]) == {"PPT-Doctor.exe", "_internal/lib.dll"}
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == "2.1.0"
    # 之后和 2.1.9 的远端清单比，只需要换 exe
    info = updater.compare(fixed, dict(claimed, files={k: v for k, v in claimed["files"].items() if "gone" not in k}))
    assert [c[0] for c in info.changed] == ["PPT-Doctor.exe"]
    # 版本一致时原样返回，不做任何哈希
    same = {"version": "2.1.0", "files": {"x": {"hash": "0", "size": 0}}}
    assert updater.reconcile_manifest(same, root, "2.1.0") is same


def test_singleton_quit_only_for_this_install_dir(monkeypatch, tmp_path):
    from pptx_finder import app as app_mod
    exe = tmp_path / "PPT Doctor" / "PPT-Doctor.exe"
    monkeypatch.setattr(app_mod.sys, "executable", str(exe))
    calls = []

    class Win:
        def force_quit(self):
            calls.append("quit")

    class Sock:
        def __init__(self, data):
            self._d = data

        def readAll(self):
            class B:
                def __init__(s, d):
                    s._d = d

                def data(s):
                    return s._d
            return B(self._d)

    monkeypatch.setattr(app_mod, "_show_window", lambda w: calls.append("show"))
    app_mod._on_singleton_message(Sock(b"quit " + str(tmp_path / "Other").encode() + b"\\"), Win())
    assert calls == []                                               # 别处的安装：不退出也不弹窗
    app_mod._on_singleton_message(Sock(("quit " + str(exe.parent).upper() + "\\").encode()), Win())
    assert calls == ["quit"]
    app_mod._on_singleton_message(Sock(b"show"), Win())
    assert calls == ["quit", "show"]


@pytest.mark.skipif(os.name != "nt", reason="helper 是 PowerShell，仅 Windows")
def test_helper_stops_leftover_process_holding_the_exe(tmp_path):
    """2026-11-11 公司实测：主程序退出后还有同目录的进程占着 PPT-Doctor.exe，换文件失败。"""
    dest = tmp_path / "dist"
    dest.mkdir()
    ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE"
    (dest / "PPT-Doctor.exe").write_bytes(ping.read_bytes())
    proc = subprocess.Popen([str(dest / "PPT-Doctor.exe"), "-n", "120", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        staging = tmp_path / "stg"
        staging.mkdir()
        (staging / "PPT-Doctor.exe").write_bytes(b"NEW-EXE")
        (staging / "manifest.json").write_text('{"version":"9.9.9"}', encoding="utf-8")
        info = updater.UpdateInfo(version="9.9.9", notes="", changed=[("PPT-Doctor.exe", "", 0)],
                                  deleted=[], raw={})
        log = tmp_path / "u.log"
        h = updater.write_helper(staging, dest, info, relaunch="", log_path=log)
        t0 = time.monotonic()
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", h["ps1"], "-MainPid", "0", "-Plan", h["plan_path"]],
                       check=True, capture_output=True, timeout=120,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        text = log.read_text(encoding="utf-8")
        assert "stopping 1 leftover process(es)" in text, text
        assert "swap done -> v9.9.9" in text, text
        assert (dest / "PPT-Doctor.exe").read_bytes() == b"NEW-EXE"
        assert proc.wait(timeout=10) is not None
        assert time.monotonic() - t0 < 60
    finally:
        if proc.poll() is None:
            proc.kill()


def test_unreadable_manifest_reconcile_never_keeps_the_wrong_version(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "99.0.0", "files": {"a": {"hash": "0", "size": 1}}}),
                                            encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(updater, "reconcile_manifest", boom)
    from pptx_finder import __version__
    got = updater.local_manifest()
    assert got["version"] == __version__ and got["files"] == {}


def test_openx_download_redirected_off_site_is_refused():
    class R:
        def geturl(self):
            return "http://evil.example.com/x"

        def close(self):
            pass
    from pptx_finder import netfetch
    with pytest.raises(netfetch.SourceFailure):
        updater._check_final_host(updater.openx_source("full"), R())
    updater._check_final_host(updater.UpdateSource("github-release", "m", "b{hash}"), R())   # 别的源不管


def test_both_projects_are_searched_newest_manifest_wins():
    own = _index(FakeOpener([(9, "ppt-doctor-update-2.1.10")], {9: [("manifest.json", F + "own10"), ("x", F + "x-own")]}))
    hub = _index(FakeOpener([(3, "ppt-doctor-update-2.1.11")], {3: [("manifest.json", F + "hub11"), ("y", F + "y-hub")]}))
    multi = openx_source.MultiIndex([own, hub])
    assert multi.latest_file("manifest.json") == (F + "hub11", "2.1.11")
    assert multi.find("x") == F + "x-own" and multi.find("y") == F + "y-hub"
    down = _index(FakeOpener([], {}, fail=True))
    assert openx_source.MultiIndex([down, hub]).latest_file("manifest.json")[1] == "2.1.11"   # 一个项目连不上不影响另一个
    with pytest.raises(openx_source.OpenxUnavailable):
        openx_source.MultiIndex([down]).find("x")


# ---------- Codex 审查 2026-11-24：内源完整性失败不许回退外网；清单跳转；零个源 ----------
def test_openx_integrity_failure_never_falls_back_to_external(tmp_path, monkeypatch):
    """外网开关打开时，内源包校验不过也要停下报错，不拿 GitHub 的包顶替。"""
    from pptx_finder import netfetch
    good = b"GOOD-ARCHIVE"
    bodies = {F + "z": b"BAD-ARCHIVE!", "https://github.com/z": good, "https://mirror.example/z": good}
    opened = []

    class _R(_Resp):
        headers = {"Content-Type": "application/octet-stream"}

        def geturl(self):
            return self.u

    def fake_urlopen(req, timeout=None):
        u = req.full_url if hasattr(req, "full_url") else str(req)
        opened.append(u)
        r = _R(bodies[u])
        r.u = u
        return r
    monkeypatch.setattr(netfetch.urllib.request, "urlopen", fake_urlopen)
    sha = hashlib.sha256(good).hexdigest()
    with pytest.raises(netfetch.AllSourcesFailed) as ei:
        netfetch.download_with_fallback([("openx", F + "z"), ("github-release", "https://github.com/z")],
                                        tmp_path / "c.zip", expect_size=None, expect_sha256=sha)
    assert opened == [F + "z"] and "不改用外网源" in str(ei.value)
    assert not (tmp_path / "c.zip").exists()

    opened.clear()                                                   # 外网源之间照旧：GitHub 坏了换备用服务器
    bodies["https://github.com/z"] = b"BAD-ARCHIVE!"
    netfetch.download_with_fallback([("github-release", "https://github.com/z"),
                                     ("self-hosted", "https://mirror.example/z")],
                                    tmp_path / "c.zip", expect_size=None, expect_sha256=sha)
    assert (tmp_path / "c.zip").read_bytes() == good


def test_update_download_stops_on_openx_integrity_but_not_on_unreachable(tmp_path, monkeypatch):
    from pptx_finder import netfetch
    tried = []
    gh = updater.UpdateSource("github-release", "https://github.com/m", "https://github.com/b/{hash}")

    def fake_delta(base, info, staging, **kw):
        tried.append(info.source.label)
        if info.source.label == "openx":
            raise fake_delta.err
        Path(staging).mkdir(parents=True, exist_ok=True)
        return staging
    monkeypatch.setattr(updater, "download_delta", fake_delta)
    info = updater.UpdateInfo(version="2.1.15", notes="", changed=[("a.exe", "b" * 64, 3)], deleted=[])
    info.sources = [updater.openx_source("base"), gh]

    fake_delta.err = netfetch.IntegrityFailure("哈希校验失败 a.exe")
    with pytest.raises(netfetch.AllSourcesFailed):
        updater.download_update("", info, tmp_path / "s1")
    assert tried == ["openx"] and not (tmp_path / "s1").exists()

    tried.clear()
    fake_delta.err = openx_source.OpenxUnavailable("公司内源连不上")    # 连不上：用户开了外网源才会走到这里，照常换源
    updater.download_update("", info, tmp_path / "s2")
    assert tried == ["openx", "github-release"] and info.used_source is gh


def test_openx_manifest_redirect_off_site_is_refused_before_request():
    import urllib.request
    from pptx_finder import netfetch
    h = updater._OpenxOnlyRedirect()
    req = urllib.request.Request(F + "manifest")
    with pytest.raises(netfetch.IntegrityFailure):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/manifest.json")
    assert h.redirect_request(req, None, 302, "Found", {}, F + "other") is not None   # 内源内部跳转照常


def test_check_refuses_external_when_openx_manifest_fails_integrity(monkeypatch, ui_settings):
    from pptx_finder import netfetch
    local, remote = _two_manifests()
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "local_manifest", lambda: local)
    openx_source.set_external_allowed(True)

    def fake_fetch(source, timeout=8, response_callback=None):
        if source.label == "openx":
            raise netfetch.IntegrityFailure("公司内源的版本清单被跳转到了别的网站，已拒绝")
        return dict(remote)
    monkeypatch.setattr(updater, "fetch_manifest_from", fake_fetch)
    rep = updater.check_sources("https://me.lt-stockpartner.tech/pptutor")
    assert rep.best is None and "不改用外网源" in rep.error and "跳转" in rep.error
    assert rep.all_failed           # 外网源答了也按失败报：界面走报错，不显示「已是最新」


def test_no_source_enabled_is_an_error_not_up_to_date(monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_OPENX", "0")
    monkeypatch.setenv("PPTX_FINDER_UPDATE_EXTERNAL", "0")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "local_manifest", lambda: {"version": "2.1.15", "files": {}})
    rep = updater.check_sources("https://me.lt-stockpartner.tech/pptutor")
    assert rep.best is None and "没有启用任何更新源" in rep.error and rep.all_failed


def test_component_manifest_integrity_failure_stops_before_external(monkeypatch):
    from pptx_finder import imgtext_ocr, netfetch
    asked = []

    def fake_open(base, timeout):
        asked.append(base)
        raise netfetch.IntegrityFailure("公司内源的组件清单被跳转到了别的网站，已拒绝")
    monkeypatch.setattr(imgtext_ocr, "_open_manifest", fake_open)
    with pytest.raises(imgtext_ocr.OcrUnavailable) as ei:
        imgtext_ocr.fetch_manifest_from_sources(
            [("openx", "openx:ocr"), ("github-release", "https://github.com/x")], 3, "识别组件")
    assert asked == ["openx:ocr"] and "不改用外网源" in str(ei.value)


def test_openx_content_length_mismatch_stops_before_external(tmp_path, monkeypatch):
    """服务器声明的大小就不对（还没下完就能看出来）：同样算内源完整性失败，不换外网。"""
    from pptx_finder import netfetch
    opened = []

    class _R(_Resp):
        def __init__(self, data, u):
            super().__init__(data)
            self.u = u
            self.headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}

        def geturl(self):
            return self.u

    def fake_urlopen(req, timeout=None):
        u = req.full_url if hasattr(req, "full_url") else str(req)
        opened.append(u)
        return _R(b"SHORT", u)
    monkeypatch.setattr(netfetch.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(netfetch.AllSourcesFailed) as ei:
        netfetch.download_with_fallback([("openx", F + "s"), ("github-release", "https://github.com/s")],
                                        tmp_path / "s.zip", expect_size=12, expect_sha256="0" * 64)
    assert opened == [F + "s"] and "文件大小不对" in ei.value.attempts[0].reason


def test_openx_oversize_body_and_bad_manifest_stop_before_external(tmp_path, monkeypatch):
    """实际收到的比清单大、内源清单不是合法 JSON：都算内源完整性失败，不换外网。"""
    from pptx_finder import imgtext_ocr, netfetch
    opened = []

    class _R(_Resp):
        headers = {"Content-Type": "application/octet-stream"}

        def geturl(self):
            return self.u

    def fake_urlopen(req, timeout=None):
        u = req.full_url if hasattr(req, "full_url") else str(req)
        opened.append(u)
        r = _R(b"X" * 64)
        r.u = u
        return r
    monkeypatch.setattr(netfetch.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(netfetch.AllSourcesFailed):
        netfetch.download_with_fallback([("openx", F + "o"), ("github-release", "https://github.com/o")],
                                        tmp_path / "o.zip", expect_size=8, expect_sha256="0" * 64)
    assert opened == [F + "o"]

    asked = []

    class _M(_Resp):
        headers = {"Content-Type": "application/json"}

    def fake_open(base, timeout):
        asked.append(base)
        return _M(b"{not json")
    monkeypatch.setattr(imgtext_ocr, "_open_manifest", fake_open)
    with pytest.raises(imgtext_ocr.OcrUnavailable):
        imgtext_ocr.fetch_manifest_from_sources(
            [("openx", "openx:ocr"), ("github-release", "https://github.com/x")], 3, "识别组件")
    assert asked == ["openx:ocr"]


def test_package_with_bad_crc_is_an_integrity_failure(tmp_path, monkeypatch):
    """压缩包目录完好、成员内容被改（CRC 不符）：解压时才发现，也要算完整性失败。"""
    import zipfile
    from pptx_finder import netfetch
    payload = b"NEW-EXE-CONTENT" * 20
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("PPT-Doctor/a.exe", payload)
    raw = bytearray(buf.getvalue())
    i = raw.index(payload)
    raw[i] ^= 0xFF                                                    # 改一个字节，目录仍可读
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as z:
        assert z.namelist() == ["PPT-Doctor/a.exe"]

    class _R(_Resp):
        headers = {"Content-Type": "application/zip", "Content-Length": str(len(raw))}

        def geturl(self):
            return "https://github.com/p.zip"
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda u, timeout=None: _R(bytes(raw)))
    src = updater.UpdateSource("github-release", "https://github.com/m", "https://github.com/b/{hash}",
                               "https://github.com/p-{version}.zip")
    info = updater.UpdateInfo(version="2.1.15", notes="", deleted=[], source=src,
                              changed=[("a.exe", hashlib.sha256(payload).hexdigest(), len(payload))])
    with pytest.raises(netfetch.IntegrityFailure):
        updater.download_package(info, tmp_path / "st")
    assert not (tmp_path / "st" / "_package.zip").exists()
