"""镜像基线发布：本地产物/模拟传输验证，不访问真实镜像或 GitHub。"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_mirror_release import mirror


def assets(tmp_path, version="2.1.0"):
    root = tmp_path / version
    root.mkdir()
    for edition, name in zip(("full", "base", "ocr"), mirror.MANIFESTS):
        payloads = {
            "PPT-Doctor.exe": ("exe " + version).encode(),
            "_internal/runtime.dll": b"unchanged runtime",
            "_internal/edition.json": json.dumps({"edition": edition}).encode(),
            "hub-api.json": b'{"apiVersion":1}',
        }
        if tuple(map(int, version.split("."))) < (2, 1, 0):
            del payloads["hub-api.json"]
        manifest = {"version": version, "edition": edition,
                    "files": {p: {"hash": hashlib.sha256(b).hexdigest(), "size": len(b)}
                              for p, b in payloads.items()}}
        (root / name).write_text(json.dumps(manifest), "utf-8")
        suffix = "" if edition == "full" else "-" + edition.title()
        with zipfile.ZipFile(root / f"PPT-Doctor-v{version}{suffix}.zip", "w") as z:
            z.writestr("PPT-Doctor/manifest.json", json.dumps(manifest))
            for path, data in payloads.items():
                z.writestr("PPT-Doctor/" + path, data)
        # 模拟 GitHub 只发最近两版差异：runtime 块没随本版发布。
        for path, data in payloads.items():
            if "runtime" not in path:
                (root / hashlib.sha256(data).hexdigest()).write_bytes(data)
    return root


def full(version="2.0.4"):
    return {"version": version, "path": f"PPT-Doctor-v{version}.zip", "sha256": "a" * 64}


def test_incremental_collection_does_not_upload_any_full_archive(tmp_path):
    items = mirror.collect_from_dir("2.1.0", assets(tmp_path), False)
    assert not any(i.remote.endswith(".zip") for i in items)
    assert {i.remote for i in items if i.last} == set(mirror.MANIFESTS)


def test_explicit_refresh_collects_only_full_edition(tmp_path):
    items = mirror.collect_from_dir("2.1.0", assets(tmp_path), False, refresh_full=True)
    assert [i.remote for i in items if i.remote.endswith(".zip")] == ["PPT-Doctor-v2.1.0.zip"]


def test_mirror_manifest_is_a_copy_and_other_editions_stay_byte_identical(tmp_path):
    src = assets(tmp_path)
    before = {n: (src / n).read_bytes() for n in mirror.MANIFESTS}
    items = mirror.collect_from_dir("2.1.0", src, False)
    result = mirror.make_mirror_manifest(items, full(), tmp_path / "mirror")
    by_name = {i.remote: i for i in result}
    doc = json.loads(by_name["manifest.json"].local.read_text("utf-8"))
    assert doc["version"] == "2.1.0" and doc["full"] == full()
    assert doc["files"] == json.loads(before["manifest.json"])["files"]
    assert by_name["manifest.json"].sha256 == mirror._sha256(by_name["manifest.json"].local)
    assert all((src / n).read_bytes() == data for n, data in before.items())
    for name in mirror.MANIFESTS[1:]:
        assert by_name[name].local == src / name
        assert "full" not in json.loads(by_name[name].local.read_text("utf-8"))


@pytest.mark.parametrize("field,value", [
    ("version", "2.1"), ("version", "2.0.4/../"),
    ("path", "../PPT-Doctor-v2.0.4.zip"),
    ("path", "https://evil.test/PPT-Doctor-v2.0.4.zip"),
    ("path", "/PPT-Doctor-v2.0.4.zip"),
    ("path", "PPT-Doctor-v2.0.4.zip?x=1"),
    ("path", "PPT-Doctor-v2.0.4-Base.zip"),
    ("path", "x/%2e%2e/PPT-Doctor-v2.0.4.zip"),
    ("path", "base.v2/PPT-Doctor-v2.0.4.zip"),
    ("path", "old base/PPT-Doctor-v2.0.4.zip"),
    ("path", "releases/base.v2/PPT-Doctor-v2.0.4.zip"),
    ("path", "releases/old base/PPT-Doctor-v2.0.4.zip"),
    ("sha256", "bad"),
])
def test_invalid_full_metadata_is_rejected(field, value):
    data = dict(full(), **{field: value})
    with pytest.raises(SystemExit):
        mirror.validate_full(data)


def test_all_three_manifests_are_required(tmp_path):
    items = mirror.collect_from_dir("2.1.0", assets(tmp_path), False)
    with pytest.raises(SystemExit, match="清单"):
        mirror.check_consistency([i for i in items if i.remote != "manifest-Ocr.json"])


@pytest.mark.parametrize("prefix", ["", "baseline/", "base-v2_1/", "RELEASES/v2_1/"])
def test_full_path_accepts_hub_compatible_root_and_directories(prefix):
    data = full()
    data["path"] = prefix + data["path"]
    assert mirror.validate_full(data) == data


def test_base_archive_cannot_be_used_as_full_baseline(tmp_path):
    src = assets(tmp_path)
    with pytest.raises(SystemExit, match="安装版"):
        mirror.inspect_package(src / "PPT-Doctor-v2.1.0-Base.zip", "2.1.0", "full")


def test_missing_old_runtime_block_is_recovered_and_hash_checked(tmp_path):
    src = assets(tmp_path)
    manifest = json.loads((src / "manifest.json").read_text("utf-8"))
    meta = manifest["files"]["_internal/runtime.dll"]
    recovered = mirror.blocks_from_package(src / "PPT-Doctor-v2.1.0.zip", manifest,
                                          {meta["hash"]}, tmp_path / "recovered")
    assert len(recovered) == 1
    assert recovered[0].remote == "files/" + meta["hash"]
    assert recovered[0].local.read_bytes() == b"unchanged runtime"


class MemoryMirror:
    """只替换SSH/HTTP边界，收集/补块/校验/发布顺序走实际发布工具。"""

    def __init__(self, monkeypatch, old):
        self.files = {n: (old / n).read_bytes() for n in mirror.MANIFESTS}
        self.files["PPT-Doctor-v2.0.4.zip"] = (old / "PPT-Doctor-v2.0.4.zip").read_bytes()
        self.events = []
        self.fail_get = None
        self.fail_replace = None
        monkeypatch.setattr(mirror, "_connect", lambda: self)
        monkeypatch.setattr(mirror, "remote_hashes", self.inventory)
        monkeypatch.setattr(mirror, "replace_remote", self.replace)
        monkeypatch.setattr(mirror.urllib.request, "urlopen", self.urlopen)

    def relative(self, path):
        assert path.startswith(mirror.REMOTE + "/")
        return path[len(mirror.REMOTE) + 1:]

    def open_sftp(self):
        return self

    def close(self):
        pass

    def stat(self, path):
        return SimpleNamespace(st_size=len(self.files.get(path.removeprefix(mirror.REMOTE + "/"), b"")))

    def put(self, local, path):
        self.files[self.relative(path)] = Path(local).read_bytes()

    def remove(self, path):
        rel = self.relative(path)
        if rel not in self.files:
            raise FileNotFoundError(rel)
        del self.files[rel]
        self.events.append(("remove", rel))

    def replace(self, client, source, destination):
        rel = self.relative(destination)
        if rel == self.fail_replace:
            raise OSError("injected atomic replace failure")
        self.files[rel] = self.files.pop(self.relative(source))
        self.events.append(("replace", rel))

    def inventory(self, client, paths):
        return {p: {"path": p, "size": len(self.files[p]), "sha256": hashlib.sha256(self.files[p]).hexdigest()}
                for p in paths if p in self.files}

    def urlopen(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        assert url.startswith(mirror.PUBLIC + "/"), "test must never use external network"
        path = url[len(mirror.PUBLIC) + 1:]
        method = "GET" if isinstance(request, str) else request.get_method()
        self.events.append((method, path))
        if path not in self.files:
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        data = self.files[path]
        if method == "GET" and path == self.fail_get:
            data = b"wrong content"
        response = io.BytesIO(b"" if method == "HEAD" else data)
        response.headers = {"Content-Length": str(len(data)), "Content-Type": "application/octet-stream"}
        return response


def run_main(monkeypatch, tmp_path, src, *extra):
    monkeypatch.setattr(sys, "argv", ["mirror_release.py", src.name, "--from-dir", str(src),
                                      "--cache-dir", str(tmp_path / "cache"), *map(str, extra)])
    return mirror.main()


def test_legacy_migration_then_incremental_reuses_verified_baseline_and_repairs_blocks(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 0
    doc = json.loads(server.files["manifest.json"])
    assert doc["version"] == "2.1.0" and doc["full"]["version"] == "2.0.4"
    assert not any(p.startswith("PPT-Doctor-v2.1.0") for p in server.files)
    for name in mirror.MANIFESTS[1:]:
        assert server.files[name] == (src / name).read_bytes()
    # 每个安装版最新清单所需的全部块可达，包含本次发布未提供的旧runtime块。
    for name in mirror.MANIFESTS:
        for meta in json.loads(server.files[name])["files"].values():
            assert hashlib.sha256(server.files["files/" + meta["hash"]]).hexdigest() == meta["hash"]
    first_manifest = next(i for i, e in enumerate(server.events) if e == ("replace", "manifest.json"))
    assert all(i < first_manifest for i, e in enumerate(server.events) if e[0] == "GET" and e[1].startswith("files/"))
    record = (tmp_path / "cache" / "baseline.json").read_bytes()
    server.events.clear()
    newer = assets(tmp_path, "2.1.1")
    assert run_main(monkeypatch, tmp_path, newer) == 0
    assert (tmp_path / "cache" / "baseline.json").read_bytes() == record
    assert ("HEAD", "PPT-Doctor-v2.0.4.zip") in server.events
    assert not any(e[0] in ("GET", "replace") and e[1].endswith(".zip") for e in server.events)
    audit = json.loads((tmp_path / "cache" / "v2.1.1" / "publish-report.json").read_text("utf-8"))
    assert audit["baselineVerification"] == "retained-record+HEAD"
    assert audit["serverHashVerifiedBlocks"] > 0


def test_existing_updater_can_upgrade_all_editions_without_latest_full_zip(tmp_path, monkeypatch):
    from pptx_finder import updater
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 0
    for name, edition in mirror.EDITIONS.items():
        previous = json.loads((old / name).read_text("utf-8"))
        remote = json.loads(server.files[name])
        info = updater.compare(previous, remote)
        assert info is not None and any(p == "hub-api.json" for p, _, _ in info.changed)
        stage = updater.download_delta(mirror.PUBLIC, info, tmp_path / ("stage-" + edition))
        for path, digest, size in info.changed:
            assert mirror._sha256(stage / path) == digest and (stage / path).stat().st_size == size
        assert json.loads((stage / "manifest.json").read_text("utf-8"))["version"] == "2.1.0"


def test_dependency_hash_failure_preserves_all_old_manifests(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    before = {n: server.files[n] for n in mirror.MANIFESTS}
    server.fail_get = "files/" + hashlib.sha256(b"exe 2.1.0").hexdigest()
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 1
    assert {n: server.files[n] for n in mirror.MANIFESTS} == before
    assert not (tmp_path / "cache" / "baseline.json").exists()


def test_record_write_failure_is_not_reported_as_complete(tmp_path, monkeypatch):
    old, src = assets(tmp_path, "2.0.4"), assets(tmp_path)
    MemoryMirror(monkeypatch, old)
    def fail_record(*args):
        raise OSError("record locked")
    monkeypatch.setattr(mirror, "save_baseline_record", fail_record)
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 1
    report = json.loads((tmp_path / "cache/v2.1.0/publish-report.json").read_text("utf-8"))
    assert not report["complete"] and "record locked" in report["error"]


def test_newer_base_channel_cannot_be_downgraded_when_full_channel_lags(tmp_path, monkeypatch):
    old, src = assets(tmp_path, "2.0.4"), assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    data = json.loads(server.files["manifest-Base.json"])
    data["version"] = "2.2.0"
    server.files["manifest-Base.json"] = json.dumps(data).encode()
    before = {n: server.files[n] for n in mirror.MANIFESTS}
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 1
    assert {n: server.files[n] for n in mirror.MANIFESTS} == before


def test_same_size_corrupt_block_is_replaced(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    digest = hashlib.sha256(b"unchanged runtime").hexdigest()
    server.files["files/" + digest] = b"x" * len(b"unchanged runtime")
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 0
    assert server.files["files/" + digest] == b"unchanged runtime"


@pytest.mark.parametrize("reason", ["runtime-change", "hub-compat"])
def test_refresh_verifies_new_full_before_switch_and_never_deletes_old_automatically(tmp_path, monkeypatch, reason):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    assert run_main(monkeypatch, tmp_path, src, "--refresh-full", reason) == 0
    full_name = "PPT-Doctor-v2.1.0.zip"
    assert json.loads(server.files["manifest.json"])["full"]["path"] == full_name
    assert server.events.index(("GET", full_name)) < server.events.index(("replace", "manifest.json"))
    assert "PPT-Doctor-v2.0.4.zip" in server.files
    assert not any(p.endswith(("-Base.zip", "-Ocr.zip")) for p in server.files)
    audit = json.loads((tmp_path / "cache/v2.1.0/publish-report.json").read_text("utf-8"))
    assert audit["refreshReason"] == reason and audit["full"]["version"] == "2.1.0"


def test_partial_manifest_commit_can_be_resumed(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    previous = json.loads(server.files["manifest.json"])
    previous["full"] = dict(full(), sha256=mirror._sha256(old / "PPT-Doctor-v2.0.4.zip"))
    server.files["manifest.json"] = json.dumps(previous).encode()
    server.fail_replace = "manifest-Ocr.json"
    assert run_main(monkeypatch, tmp_path, src) == 1
    assert json.loads(server.files["manifest-Base.json"])["version"] == "2.1.0"
    assert server.files["manifest-Ocr.json"] == (old / "manifest-Ocr.json").read_bytes()
    server.fail_replace = None
    assert run_main(monkeypatch, tmp_path, src) == 0
    assert all(json.loads(server.files[n])["version"] == "2.1.0" for n in mirror.MANIFESTS)


def test_missing_baseline_does_not_guess_zip_from_latest_version(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    assert run_main(monkeypatch, tmp_path, src) == 1
    assert not any(e[0] == "replace" for e in server.events)


@pytest.mark.parametrize("problem", ["missing", "hash", "html"])
def test_bad_retained_baseline_is_rejected_before_publication(tmp_path, monkeypatch, problem):
    old, src = assets(tmp_path, "2.0.4"), assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    previous = json.loads(server.files["manifest.json"])
    previous["full"] = dict(full(), sha256=mirror._sha256(old / "PPT-Doctor-v2.0.4.zip"))
    server.files["manifest.json"] = json.dumps(previous).encode()
    path = "PPT-Doctor-v2.0.4.zip"
    if problem == "missing":
        del server.files[path]
    elif problem == "hash":
        server.files[path] = b"x" * len(server.files[path])
    else:
        def blocked(request, timeout=None):
            response = server.urlopen(request, timeout)
            if request.get_method() == "HEAD":
                response.headers["Content-Type"] = "text/html"
            return response
        monkeypatch.setattr(mirror.urllib.request, "urlopen", blocked)
    assert run_main(monkeypatch, tmp_path, src) == 1
    assert not any(e[0] == "replace" for e in server.events)


def test_refresh_without_full_zip_is_rejected(tmp_path, monkeypatch):
    old, src = assets(tmp_path, "2.0.4"), assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    (src / "PPT-Doctor-v2.1.0.zip").unlink()
    assert run_main(monkeypatch, tmp_path, src, "--refresh-full", "missing-baseline") == 1
    assert not any(e[0] == "replace" for e in server.events)


def test_missing_block_without_source_package_keeps_old_manifests(tmp_path, monkeypatch):
    old, src = assets(tmp_path, "2.0.4"), assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    for package in src.glob("*.zip"):
        package.unlink()
    before = {n: server.files[n] for n in mirror.MANIFESTS}
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 1
    assert {n: server.files[n] for n in mirror.MANIFESTS} == before


def test_verify_only_never_uploads_or_deletes(tmp_path, monkeypatch):
    old, src = assets(tmp_path, "2.0.4"), assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 0
    before = dict(server.files)
    server.events.clear()
    assert run_main(monkeypatch, tmp_path, src, "--verify-only") == 0
    assert server.files == before and not any(e[0] in ("replace", "remove") for e in server.events)


def test_corrupt_recovery_zip_stops_before_any_manifest_changes(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    for package in src.glob("*.zip"):
        with zipfile.ZipFile(package) as z:
            contents = {n: z.read(n) for n in z.namelist()}
        contents["PPT-Doctor/_internal/runtime.dll"] = b"tampered"
        with zipfile.ZipFile(package, "w") as z:
            for name, data in contents.items():
                z.writestr(name, data)
    before = {n: server.files[n] for n in mirror.MANIFESTS}
    assert run_main(monkeypatch, tmp_path, src, "--baseline-zip", old / "PPT-Doctor-v2.0.4.zip") == 1
    assert {n: server.files[n] for n in mirror.MANIFESTS} == before


def test_dry_run_does_not_connect_mirror_or_download_archives(tmp_path, monkeypatch, capsys):
    src = assets(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not contact mirror or download any artifact")
    monkeypatch.setattr(mirror, "_connect", forbidden)
    monkeypatch.setattr(mirror, "public_manifest", forbidden)
    monkeypatch.setattr(mirror, "_download", forbidden)
    assert run_main(monkeypatch, tmp_path, src, "--dry-run") == 0
    assert "未连接镜像" in capsys.readouterr().out
    metadata = [{"name": p.name, "size": p.stat().st_size, "browser_download_url": "unused"}
                for p in src.iterdir()]
    monkeypatch.setattr(mirror, "_github_assets", lambda tag: metadata)
    items = mirror.collect_from_github("2.1.0", tmp_path / "github", False, download=False)
    assert not any(i.remote.endswith(".zip") for i in items)
    refreshed = mirror.collect_from_github("2.1.0", tmp_path / "github", False, download=False, refresh_full=True)
    assert [i.remote for i in refreshed if i.remote.endswith(".zip")] == ["PPT-Doctor-v2.1.0.zip"]


@pytest.mark.parametrize("bad", ["../old.zip", "files/" + "a" * 64, "PPT-Doctor-v2.1.0.zip"])
def test_cleanup_refuses_non_zip_paths_and_active_baseline(bad):
    with pytest.raises(SystemExit):
        mirror.prune_full([bad], {"version": "2.1.0", "full": full("2.1.0")}, dry_run=True)


def test_cleanup_only_removes_explicit_old_zip_after_active_baseline_verification(tmp_path, monkeypatch):
    old = assets(tmp_path, "2.0.4")
    src = assets(tmp_path)
    server = MemoryMirror(monkeypatch, old)
    assert run_main(monkeypatch, tmp_path, src, "--refresh-full", "runtime-change") == 0
    current = json.loads(server.files["manifest.json"])
    blocks = {p: b for p, b in server.files.items() if p.startswith("files/")}
    assert mirror.prune_full(["PPT-Doctor-v2.0.4.zip"], current) == ["PPT-Doctor-v2.0.4.zip"]
    assert "PPT-Doctor-v2.0.4.zip" not in server.files
    assert {p: b for p, b in server.files.items() if p.startswith("files/")} == blocks


def test_watch_does_not_mistake_partial_publish_for_synced(tmp_path):
    spec = importlib.util.spec_from_file_location("mirror_watch_test", mirror.ROOT / "tools/mirror_watch.py")
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
    docs = {n: {"version": "2.1.0"} for n in mirror.MANIFESTS}
    assert not watch.mirror_is_current(docs, "2.1.0")
    docs["manifest.json"]["full"] = full()
    assert watch.mirror_is_current(docs, "2.1.0")
    docs["manifest-Ocr.json"]["version"] = "2.0.4"
    assert not watch.mirror_is_current(docs, "2.1.0")


@pytest.mark.skipif(os.name != "nt", reason="Windows镜像原子替换命令")
def test_actual_windows_remote_commands_hash_and_atomically_replace(tmp_path, monkeypatch):
    class LocalSSH:
        def exec_command(self, command, **kwargs):
            result = subprocess.run(command.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
            out = io.BytesIO(result.stdout)
            out.channel = SimpleNamespace(recv_exit_status=lambda: result.returncode)
            return None, out, io.BytesIO(result.stderr)
    monkeypatch.setattr(mirror, "REMOTE", tmp_path.as_posix())
    target, staging = tmp_path / "manifest.json", tmp_path / "staging.json"
    target.write_bytes(b"old")
    staging.write_bytes(b"new")
    client = LocalSSH()
    before = mirror.remote_hashes(client, ["manifest.json", "missing"])
    assert before["manifest.json"]["sha256"] == hashlib.sha256(b"old").hexdigest()
    assert "missing" not in before
    mirror.replace_remote(client, staging.as_posix(), target.as_posix())
    assert target.read_bytes() == b"new" and not staging.exists()
    # 锁住目标不允许删除/替换：替换失败时旧清单仍在，不可先删旧再rename。
    import win32con
    import win32file
    handle = win32file.CreateFile(str(target), win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
                                  None, win32con.OPEN_EXISTING, 0, None)
    staging.write_bytes(b"newer")
    try:
        with pytest.raises(SystemExit, match="镜像文件操作失败"):
            mirror.replace_remote(client, staging.as_posix(), target.as_posix())
        assert target.read_bytes() == b"new" and staging.read_bytes() == b"newer"
    finally:
        handle.Close()


@pytest.mark.skipif(os.name != "nt", reason="Windows OpenSSH默认cmd命令行长度")
def test_many_block_hashes_fit_actual_windows_cmd_limit(tmp_path, monkeypatch):
    lengths = []
    class CmdSSH:
        def exec_command(self, command, **kwargs):
            lengths.append(len(command))
            result = subprocess.run(["cmd.exe", "/d", "/s", "/c", command],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
            out = io.BytesIO(result.stdout)
            out.channel = SimpleNamespace(recv_exit_status=lambda: result.returncode)
            return None, out, io.BytesIO(result.stderr)
    monkeypatch.setattr(mirror, "REMOTE", tmp_path.as_posix())
    (tmp_path / "files").mkdir()
    paths = ["files/" + f"{i:064x}" for i in range(40)]
    for path in paths:
        (tmp_path / path).write_bytes(b"block")
    found = mirror.remote_hashes(CmdSSH(), paths)
    assert set(found) == set(paths)
    assert all(row["sha256"] == hashlib.sha256(b"block").hexdigest() for row in found.values())
    assert max(lengths) <= 8191
