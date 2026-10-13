"""v2.0 多源更新：逐源回落、拦截页识别、人话原因、速度/剩余时间（对照 AI Hub 一键更新）。

用本机 http.server 起几个真源：一个模拟公司代理（任何路径都回 3.5 KB 的拦截网页），
一个 404，一个正常。走完 check → download → staging 校验，确认 attempts 的记录和原因文案。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import ssl
import threading
import urllib.error
from pathlib import Path

import pytest

from pptx_finder import netfetch, updater

INTERCEPT_PAGE = ("<!DOCTYPE html><html><head><title>Access Denied</title></head><body>"
                  + "公司网络安全策略已拦截此下载。" * 120 + "</body></html>").encode("utf-8")


class _InterceptHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(INTERCEPT_PAGE)))
        self.end_headers()
        self.wfile.write(INTERCEPT_PAGE)

    def log_message(self, *a):
        pass


class _QuietFiles(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve(handler):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _serve_dir(root: Path):
    return _serve(functools.partial(_QuietFiles, directory=str(root)))


def _publish(dist: Path, server: Path, version: str) -> dict:
    """照发布脚本的布局：manifest.json + files/<sha256>。"""
    m = updater.build_manifest(dist, version, f"PPT Doctor v{version}\n- 修复窗口拖不动\n- 新的更新弹窗")
    (server / "files").mkdir(parents=True, exist_ok=True)
    for rel, meta in m["files"].items():
        shutil.copy2(dist / rel, server / "files" / meta["hash"])
    (server / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


def _two_versions(tmp_path: Path):
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "PPT-Doctor.exe").write_bytes(b"MZ old exe" * 50)
    (v1 / "keep.dll").write_bytes(b"\x00\x01" * 3000)
    m1 = updater.build_manifest(v1, "1.9.0")
    v2 = tmp_path / "v2"
    shutil.copytree(v1, v2)
    (v2 / "PPT-Doctor.exe").write_bytes(b"MZ brand new v2 exe" * 400)
    return m1, v2


# ---------- 人话原因 ----------
@pytest.mark.parametrize("exc, needle", [
    (socket.timeout("timed out"), "连接超时"),
    (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "域名解析失败"),
    (urllib.error.URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED")), "证书校验失败"),
    (urllib.error.URLError(ConnectionResetError(10054, "reset")), "连接被中断"),
    (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), "拒绝连接"),
    (urllib.error.URLError(OSError("Tunnel connection failed: 407")), "代理"),
    (urllib.error.HTTPError("u", 404, "nf", None, None), "HTTP 404"),
    (urllib.error.HTTPError("u", 502, "bad", None, None), "HTTP 502"),
    (ValueError("哈希校验失败 app.exe：期望 a 实得 b"), "校验不一致"),
    (netfetch.SourceFailure("收到 3.5 KB 的网页"), "3.5 KB 的网页"),
])
def test_reasons_are_plain_chinese(exc, needle):
    assert needle in netfetch.reason_of(exc)


def test_web_page_detection():
    assert netfetch.looks_like_web_page("text/html; charset=utf-8", None)
    assert netfetch.looks_like_web_page("", b"  <!DOCTYPE html><html>")
    assert netfetch.looks_like_web_page(None, b"<html><body>blocked")
    assert not netfetch.looks_like_web_page("application/octet-stream", b"MZ\x90\x00")


def test_progress_meter_speed_and_eta_with_fake_clock():
    now = [0.0]
    meter = netfetch.ProgressMeter(10_000_000, "GitHub", clock=lambda: now[0])
    assert meter.update(100) is None            # 不到 250ms 不出快照
    now[0] = 1.0
    snap = meter.update(2_000_000)
    assert snap["source"] == "GitHub"
    assert snap["speed"] == pytest.approx(2_000_000)
    assert snap["eta"] == 4                     # 剩 8 MB / 2 MB/s
    now[0] = 2.0
    snap = meter.update(3_000_000)              # 瞬时 1 MB/s → 平滑 0.7*2 + 0.3*1 = 1.7 MB/s
    assert snap["speed"] == pytest.approx(1_700_000)
    assert snap["eta"] == round(7_000_000 / 1_700_000)
    text = netfetch.describe_progress(snap, "识别组件")
    assert text.startswith("从 GitHub 下载识别组件 2.9 / 9.5 MB")
    assert "MB/s" in text and "剩约" in text


# ---------- 单文件逐源回落 ----------
def test_file_fallback_records_each_source_reason(tmp_path):
    payload = b"component-bytes" * 4000
    import hashlib
    sha = hashlib.sha256(payload).hexdigest()
    good_root = tmp_path / "good"
    good_root.mkdir()
    (good_root / "component.zip").write_bytes(payload)
    bad_root = tmp_path / "bad"
    bad_root.mkdir()
    (bad_root / "component.zip").write_bytes(b"x" * len(payload))   # 大小对、内容不对

    intercept, intercept_url = _serve(_InterceptHandler)
    empty, empty_url = _serve_dir(tmp_path / "nothing-here")
    bad, bad_url = _serve_dir(bad_root)
    good, good_url = _serve_dir(good_root)
    statuses = []
    try:
        ok, attempts = netfetch.download_with_fallback(
            [("github-release", intercept_url + "/component.zip"),
             ("self-hosted", empty_url + "/component.zip"),
             ("self-hosted-3", bad_url + "/component.zip"),
             ("self-hosted-4", good_url + "/component.zip")],
            tmp_path / "out" / "component.zip", expect_size=len(payload), expect_sha256=sha,
            on_status=statuses.append, what="识别组件")
    finally:
        for s in (intercept, empty, bad, good):
            s.shutdown()
    assert (tmp_path / "out" / "component.zip").read_bytes() == payload
    assert not list((tmp_path / "out").glob("*.part"))
    assert [a.status for a in attempts] == ["failed", "failed", "failed", "ok"]
    assert attempts[0].name == "GitHub"
    assert "网页" in attempts[0].reason and "代理拦截" in attempts[0].reason
    assert "KB" in attempts[0].reason
    assert "404" in attempts[1].reason
    assert "校验不一致" in attempts[2].reason
    assert ok.label == "self-hosted-4" and ok.bytes == len(payload)
    assert statuses and statuses[-1]["received"] == len(payload)


def test_all_sources_failed_lists_every_reason(tmp_path):
    intercept, url = _serve(_InterceptHandler)
    try:
        with pytest.raises(netfetch.AllSourcesFailed) as ei:
            netfetch.download_with_fallback(
                [("github-release", url + "/a.zip"), ("self-hosted", "http://127.0.0.1:9/a.zip")],
                tmp_path / "a.zip", expect_size=7_000_000, expect_sha256="0" * 64, timeout=3)
    finally:
        intercept.shutdown()
    msg = str(ei.value)
    assert "GitHub：收到" in msg and "备用服务器：" in msg
    assert len(ei.value.attempts) == 2


def test_cancel_is_not_recorded_as_a_source_failure(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    (root / "f.bin").write_bytes(b"z" * 300_000)
    srv, url = _serve_dir(root)
    try:
        with pytest.raises(InterruptedError):
            netfetch.download_with_fallback(
                [("github-release", url + "/f.bin"), ("self-hosted", url + "/f.bin")],
                tmp_path / "f.bin", expect_size=300_000, expect_sha256="0" * 64,
                cancel=lambda: True)
    finally:
        srv.shutdown()
    assert not (tmp_path / "f.bin").exists()


# ---------- 整个更新：check → download → staging ----------
@pytest.fixture
def frozen_local(monkeypatch):
    def apply(local: dict):
        monkeypatch.setattr(updater, "is_frozen", lambda: True)
        monkeypatch.setattr(updater, "local_manifest", lambda: local)
    return apply


def test_check_then_download_falls_back_from_intercepted_source(tmp_path, monkeypatch, frozen_local):
    m1, v2 = _two_versions(tmp_path)
    frozen_local(m1)
    good_dir = tmp_path / "good"
    _publish(v2, good_dir, "2.0.0")
    intercept, intercept_url = _serve(_InterceptHandler)
    good, good_url = _serve_dir(good_dir)
    monkeypatch.setenv("PPTX_FINDER_UPDATE_NO_GITHUB", "1")
    try:
        # 代理源的清单也被换成网页 → 查版本时记一条「收到的是网页」
        report = updater.check_sources(f"{intercept_url};{good_url}", timeout=5)
        assert report.best is not None and report.best.version == "2.0.0"
        assert [c.ok for c in report.checks] == [False, True]
        assert "网页" in report.checks[0].reason
        assert report.checks[1].version == "2.0.0"
        assert [s.label for s in report.best.sources] == ["self-hosted-2"]

        # 下载阶段让代理源也排进来（清单拿到了、文件被拦）：手动把它放在最前面
        info = report.best
        info.sources = updater.update_sources(f"{intercept_url};{good_url}")
        statuses, seen_attempts = [], []
        staging = tmp_path / "staging"
        updater.download_update(f"{intercept_url};{good_url}", info, staging,
                                on_status=statuses.append, on_attempts=seen_attempts.append)
    finally:
        intercept.shutdown()
        good.shutdown()
    assert (staging / "PPT-Doctor.exe").read_bytes() == (v2 / "PPT-Doctor.exe").read_bytes()
    assert not (staging / "keep.dll").exists()           # 增量：没变的不下载
    assert json.loads((staging / "manifest.json").read_text("utf-8"))["version"] == "2.0.0"
    assert [a["status"] for a in info.attempts] == ["failed", "ok"]
    assert "代理拦截" in info.attempts[0]["reason"]
    assert info.used_source.label == "self-hosted-2"
    assert info.kind == "incremental"
    assert seen_attempts[0][0]["status"] == "downloading"
    assert statuses and statuses[-1]["source"] == info.used_source.name


def test_missing_blocks_fall_back_to_the_same_sources_whole_package(tmp_path, monkeypatch, frozen_local):
    import zipfile
    m1, v2 = _two_versions(tmp_path)
    frozen_local(m1)
    srv_dir = tmp_path / "srv"
    m2 = _publish(v2, srv_dir, "2.0.0")
    shutil.rmtree(srv_dir / "files")                      # 块没随这一版发布（用户落后好几版）
    with zipfile.ZipFile(srv_dir / "PPT-Doctor-v2.0.0.zip", "w") as z:
        for rel in m2["files"]:
            z.write(v2 / rel, "PPT-Doctor/" + rel)
    srv, url = _serve_dir(srv_dir)
    monkeypatch.setenv("PPTX_FINDER_UPDATE_NO_GITHUB", "1")
    try:
        report = updater.check_sources(url, timeout=5)
        info = report.best
        updater.download_update(url, info, tmp_path / "stg")
    finally:
        srv.shutdown()
    assert info.kind == "package"
    assert (tmp_path / "stg" / "PPT-Doctor.exe").read_bytes() == (v2 / "PPT-Doctor.exe").read_bytes()
    assert info.attempts[-1]["status"] == "ok"


def test_all_sources_down_gives_a_readable_check_error(tmp_path, monkeypatch, frozen_local):
    m1, _v2 = _two_versions(tmp_path)
    frozen_local(m1)
    monkeypatch.setenv("PPTX_FINDER_UPDATE_NO_GITHUB", "1")
    report = updater.check_sources("http://127.0.0.1:9;http://127.0.0.1:7", timeout=2)
    assert report.best is None and report.all_failed
    assert report.error.startswith("所有更新源都连不上：")
    assert "备用服务器" in report.error


def test_highest_version_wins_and_same_version_sources_queue_up(tmp_path, monkeypatch, frozen_local):
    m1, v2 = _two_versions(tmp_path)
    frozen_local(m1)
    old_dir, new_a, new_b = tmp_path / "old", tmp_path / "a", tmp_path / "b"
    _publish(v2, old_dir, "1.9.5")
    _publish(v2, new_a, "2.0.0")
    _publish(v2, new_b, "2.0.0")
    servers = [_serve_dir(d) for d in (old_dir, new_a, new_b)]
    monkeypatch.setenv("PPTX_FINDER_UPDATE_NO_GITHUB", "1")
    try:
        report = updater.check_sources(";".join(u for _s, u in servers), timeout=5)
    finally:
        for s, _u in servers:
            s.shutdown()
    assert report.best.version == "2.0.0"
    assert [s.label for s in report.best.sources] == ["self-hosted-2", "self-hosted-3"]
    assert [c.version for c in report.checks] == ["1.9.5", "2.0.0", "2.0.0"]


# ---------- 更新结果：只提示一次 ----------
def test_update_result_is_described_once(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "update_result_path", lambda: tmp_path / "update_result.json")
    (tmp_path / "update_result.json").write_text(
        json.dumps({"version": "2.0.0", "ok": True, "rolled_back": False, "reason": ""}),
        encoding="utf-8-sig")
    text = updater.describe_update_result(updater.consume_update_result(), "2.0.0")
    assert text == "✅ 已更新到 v2.0.0"
    assert updater.consume_update_result() is None
    bad = {"version": "2.0.0", "ok": False, "rolled_back": True, "reason": "replace failed: x.dll"}
    assert "已恢复原来的版本" in updater.describe_update_result(bad, "1.9.0")


@pytest.mark.skipif(os.name != "nt", reason="helper 是 PowerShell，仅 Windows")
def test_helper_writes_a_result_file(tmp_path):
    import subprocess
    dest = tmp_path / "install"
    dest.mkdir()
    (dest / "a.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / "stg"
    staging.mkdir()
    (staging / "a.txt").write_text("new", encoding="utf-8")
    (staging / "manifest.json").write_text("{}", encoding="utf-8")
    info = updater.UpdateInfo(version="2.0.0", notes="", changed=[("a.txt", "0" * 64, 3)],
                              deleted=[], raw={})
    result = tmp_path / "update_result.json"
    h = updater.write_helper(staging, dest, info, relaunch="", log_path=tmp_path / "u.log",
                             result_path=result)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", h["ps1"], "-MainPid", "0", "-Plan", h["plan_path"]],
                   check=True, timeout=60, stdin=subprocess.DEVNULL)
    data = json.loads(result.read_text(encoding="utf-8-sig"))
    assert data == {"version": "2.0.0", "ok": True, "rolled_back": False, "reason": ""}
    assert (dest / "a.txt").read_text(encoding="utf-8") == "new"


# ---------- 识别组件（插件）下载同样逐源回落 ----------
def _component(tmp_path: Path):
    import zipfile
    payload = {"pptdoctor-ocr.exe": b"MZ-fake-exe", "models/det.onnx": b"onnx" * 1000}
    archive = tmp_path / "comp" / "component.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as z:
        for rel, data in payload.items():
            z.writestr(rel, data)
    import hashlib
    manifest = {
        "version": "1.2.0",
        "files": {rel: {"hash": hashlib.sha256(d).hexdigest(), "size": len(d)}
                  for rel, d in payload.items()},
        "archive": {"name": "component.zip",
                    "hash": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "size": archive.stat().st_size},
    }
    (archive.parent / "component.json").write_text(json.dumps(manifest), "utf-8")
    return manifest, archive.parent


def test_component_install_falls_back_past_an_intercepting_source(tmp_path, monkeypatch):
    from pptx_finder import imgtext_ocr
    _manifest, comp_dir = _component(tmp_path)
    target = tmp_path / "installed"
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: target)
    monkeypatch.delenv("PPTUTOR_OCR_CMD", raising=False)
    intercept, iurl = _serve(_InterceptHandler)
    good, gurl = _serve_dir(comp_dir)
    statuses = []
    try:
        # 清单两个源都能取到（代理只拦大文件，Hub 公司实测的情况）：让拦截源给出真清单
        sources = [("github-release", gurl), ("self-hosted", gurl)]
        manifest = imgtext_ocr.fetch_manifest_from_sources(sources)
        assert [label for label, _ in manifest["_bases"]] == ["github-release", "self-hosted"]
        manifest["_bases"] = [("github-release", iurl), ("self-hosted", gurl)]
        version = imgtext_ocr.install(manifest, status=statuses.append)
    finally:
        intercept.shutdown()
        good.shutdown()
    assert version == "1.2.0"
    assert (target / "pptdoctor-ocr.exe").read_bytes() == b"MZ-fake-exe"
    attempts = [s["attempts"] for s in statuses if "attempts" in s][-1]
    assert [a["status"] for a in attempts] == ["failed", "ok"]
    assert attempts[0]["name"] == "GitHub" and "代理拦截" in attempts[0]["reason"]
    assert "识别组件" in attempts[0]["reason"]
    progress = [s for s in statuses if "received" in s]
    assert progress and progress[-1]["source"] == "备用服务器"


def test_component_all_sources_fail_lists_reasons(tmp_path, monkeypatch):
    from pptx_finder import imgtext_ocr
    manifest, _comp_dir = _component(tmp_path)
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: tmp_path / "installed")
    intercept, iurl = _serve(_InterceptHandler)
    try:
        manifest["_bases"] = [("github-release", iurl), ("self-hosted", "http://127.0.0.1:9")]
        with pytest.raises(imgtext_ocr.OcrUnavailable) as ei:
            imgtext_ocr.install(manifest)
    finally:
        intercept.shutdown()
    msg = str(ei.value)
    assert msg.startswith("识别组件下载失败：GitHub：收到") and "备用服务器：" in msg
    assert not (tmp_path / "installed").exists()


def test_formula_manifest_uses_both_sources_and_records_checks(tmp_path, monkeypatch):
    from pptx_finder.screenshots import formula_component
    from pptx_finder import imgtext_ocr
    _m, comp_dir = _component(tmp_path)
    intercept, iurl = _serve(_InterceptHandler)
    good, gurl = _serve_dir(comp_dir)
    monkeypatch.setattr(formula_component, "sources", lambda: [("github-release", iurl),
                                                              ("self-hosted", gurl)])
    try:
        manifest = formula_component.fetch_manifest()
    finally:
        intercept.shutdown()
        good.shutdown()
    assert manifest["_base"] == gurl
    checks = manifest["_checks"]
    assert checks[0]["ok"] is False and "网页" in checks[0]["reason"]
    assert checks[1]["ok"] is True
    # 生产默认：GitHub formula tag 在前，自建 /formula 作备用
    labels = [label for label, _ in imgtext_ocr.component_sources("formula", formula_component.RELEASE_BASE)]
    assert labels == ["github-release", "self-hosted"]
