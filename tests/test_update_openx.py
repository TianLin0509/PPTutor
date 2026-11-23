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


def test_openx_is_first_source_only_when_enabled(monkeypatch):
    base = "https://me.lt-stockpartner.tech/pptutor"
    monkeypatch.setattr(updater, "_is_default_base", lambda b: True)
    monkeypatch.setenv("PPTX_FINDER_OPENX", "0")
    assert [s.label for s in updater.update_sources(base)] == ["github-release", "self-hosted"]
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    srcs = updater.update_sources(base, edition="base")
    assert [s.label for s in srcs] == ["openx", "github-release", "self-hosted"]
    assert srcs[0].name == "公司内源" and srcs[0].manifest_file == "manifest-Base.json"


def _two_manifests():
    local = {"version": "2.1.9", "files": {"PPT-Doctor.exe": {"hash": "a" * 64, "size": 3}}}
    remote = {"version": "2.1.10", "files": {"PPT-Doctor.exe": {"hash": "b" * 64, "size": 3}}}
    return local, remote


def test_check_uses_openx_first_and_hides_it_when_unreachable(monkeypatch):
    local, remote = _two_manifests()
    monkeypatch.setenv("PPTX_FINDER_OPENX", "1")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "local_manifest", lambda: local)

    def fake_fetch(source, timeout=8, response_callback=None):
        if source.label == "openx":
            if fake_fetch.openx_down:
                raise openx_source.OpenxUnavailable("公司内源连不上")
            return remote
        return dict(remote) if source.label == "github-release" else {"version": "2.1.9", "files": {}}
    monkeypatch.setattr(updater, "fetch_manifest_from", fake_fetch)

    fake_fetch.openx_down = False
    rep = updater.check_sources("https://me.lt-stockpartner.tech/pptutor")
    assert rep.best.version == "2.1.10"
    assert [s.label for s in rep.best.sources][0] == "openx"       # 下载先走内源
    assert "openx" in [c.label for c in rep.checks]

    fake_fetch.openx_down = True                                    # 公司外
    rep = updater.check_sources("https://me.lt-stockpartner.tech/pptutor")
    assert rep.best.version == "2.1.10" and rep.best.sources[0].label != "openx"
    assert "openx" not in [c.label for c in rep.checks]            # 不显示成一条失败
    assert not rep.error


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
