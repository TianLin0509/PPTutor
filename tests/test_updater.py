"""增量自动更新真实测试：清单 diff（纯函数）+ 本地 HTTP 真实下载校验 + PowerShell helper 真实换文件。

不 mock 网络/文件：起真实 http.server 服务内容寻址块、真实跑 apply.ps1 做文件替换，
验证「只下变化块 / sha256 校验 / 原地替换+删废弃+重启」端到端可用。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from pptx_finder import updater
from pptx_finder import __version__


# ---------- 清单 diff（纯函数单测） ----------
def test_compare_changed_added_deleted():
    local = {"version": "0.9.0", "files": {
        "a.txt": {"hash": "h_a", "size": 5},
        "keep.bin": {"hash": "h_k", "size": 9},
        "gone.txt": {"hash": "h_g", "size": 3},
    }}
    remote = {"version": "0.9.1", "notes": "x", "files": {
        "a.txt": {"hash": "h_a2", "size": 7},   # 改
        "keep.bin": {"hash": "h_k", "size": 9},  # 不变
        "new.txt": {"hash": "h_n", "size": 4},   # 增
    }}
    info = updater.compare(local, remote)
    assert info is not None
    assert info.version == "0.9.1"
    assert {r[0] for r in info.changed} == {"a.txt", "new.txt"}  # keep.bin 未变不下载
    assert info.deleted == ["gone.txt"]
    assert info.total_bytes == 7 + 4


def test_compare_same_or_older_returns_none():
    m = {"version": "1.2.0", "files": {}}
    assert updater.compare(m, {"version": "1.2.0", "files": {}}) is None   # 同版本
    assert updater.compare(m, {"version": "1.1.9", "files": {}}) is None   # 更旧
    assert updater.compare(m, {"version": "1.10.0", "files": {}}) is not None  # 1.10 > 1.2（按段比，非字典序）


def test_build_manifest_excludes_self_and_uses_posix(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("yo", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")  # 应被排除
    m = updater.build_manifest(tmp_path, "0.9.0")
    assert set(m["files"]) == {"a.txt", "sub/b.txt"}  # 正斜杠 + 不含 manifest.json 自身


def test_local_manifest_self_heals_when_missing(tmp_path, monkeypatch):
    """已发出的绿色包若漏带 manifest，应能按当前安装目录补生成。"""
    (tmp_path / "PPT Doctor.exe").write_bytes(b"exe")
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "lib.dll").write_bytes(b"dll")
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)

    m = updater.local_manifest()

    assert m is not None
    assert m["version"] == __version__
    assert set(m["files"]) == {"PPT Doctor.exe", "_internal/lib.dll"}
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["version"] == __version__


# ---------- 本地 HTTP 真实下载 ----------
def _publish(src_dir: Path, server_root: Path, version: str, notes: str = "") -> dict:
    """模拟 tools/release：把 src_dir 发布为 server_root（manifest.json + files/<hash> 内容寻址）。"""
    m = updater.build_manifest(src_dir, version, notes)
    files_dir = server_root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    for rel, meta in m["files"].items():
        shutil.copyfile(src_dir / rel, files_dir / meta["hash"])
    (server_root / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


def _serve(root: Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_download_delta_real_http(tmp_path):
    v1 = tmp_path / "v1"
    (v1 / "new").mkdir(parents=True)
    (v1 / "a.txt").write_bytes(b"alpha")
    (v1 / "keep.bin").write_bytes(b"\x00\x01\x02" * 200)
    m1 = updater.build_manifest(v1, "0.9.0")

    v2 = tmp_path / "v2"
    shutil.copytree(v1, v2)
    (v2 / "a.txt").write_bytes(b"ALPHA-v2-changed")   # 改
    (v2 / "new" / "x.txt").write_bytes(b"brand new")  # 增（子目录）
    # keep.bin 不变

    server = tmp_path / "srv"
    _publish(v2, server, "0.9.1", "测试更新说明")
    httpd, base = _serve(server)
    try:
        remote = updater.fetch_remote_manifest(base)
        info = updater.compare(m1, remote)
        assert info.notes == "测试更新说明"
        assert {r[0] for r in info.changed} == {"a.txt", "new/x.txt"}  # 未变的 keep.bin 不在内

        staging = tmp_path / "stg"
        seen = []
        updater.download_delta(base, info, staging, progress=lambda d, t: seen.append((d, t)))

        assert (staging / "a.txt").read_bytes() == b"ALPHA-v2-changed"
        assert (staging / "new" / "x.txt").read_bytes() == b"brand new"
        assert not (staging / "keep.bin").exists()  # 增量铁证：未变文件不下载
        # 远端清单随更新落地 staging（供 helper 换成新本地清单）
        assert json.loads((staging / "manifest.json").read_text(encoding="utf-8"))["version"] == "0.9.1"
        assert seen and seen[-1][0] == seen[-1][1] > 0  # 进度回调到达 100%
    finally:
        httpd.shutdown()


def test_download_delta_hash_mismatch_raises(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    (v / "a.txt").write_bytes(b"real-content")
    m = updater.build_manifest(v, "0.9.1")
    server = tmp_path / "srv"
    (server / "files").mkdir(parents=True)
    h = m["files"]["a.txt"]["hash"]
    (server / "files" / h).write_bytes(b"TAMPERED")  # 内容被篡改，哈希对不上
    (server / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    httpd, base = _serve(server)
    try:
        info = updater.compare({"version": "0.9.0", "files": {}}, m)
        with pytest.raises(ValueError):
            updater.download_delta(base, info, tmp_path / "stg")
    finally:
        httpd.shutdown()


# ---------- PowerShell helper 真实换文件 ----------
@pytest.mark.skipif(os.name != "nt", reason="helper 是 PowerShell，仅 Windows")
def test_helper_swap_real(tmp_path):
    # 模拟已安装目录
    dest = tmp_path / "dist"
    (dest / "sub").mkdir(parents=True)
    (dest / "app.txt").write_text("OLD", encoding="utf-8")
    (dest / "sub" / "keep.txt").write_text("KEEP", encoding="utf-8")
    (dest / "obsolete.txt").write_text("DELETE_ME", encoding="utf-8")
    # staging：改 app.txt + 增 sub/new.txt + 新本地清单 manifest.json
    staging = tmp_path / "stg"
    (staging / "sub").mkdir(parents=True)
    (staging / "app.txt").write_text("NEW", encoding="utf-8")
    (staging / "sub" / "new.txt").write_text("ADDED", encoding="utf-8")
    (staging / "manifest.json").write_text('{"version":"0.9.1"}', encoding="utf-8")
    # relaunch 用一个写标记的 bat，验证「重启」确实发生
    marker = tmp_path / "relaunched.flag"
    (dest / "relaunch.bat").write_text(f'@echo done> "{marker}"\r\n', encoding="ascii")

    info = updater.UpdateInfo(
        version="0.9.1", notes="",
        changed=[("app.txt", "", 0), ("sub/new.txt", "", 0)],
        deleted=["obsolete.txt"], raw={})
    h = updater.write_helper(staging, dest, info, relaunch="relaunch.bat")
    # MainPid=0 → 不等待，立即换。同步跑到 helper 退出。
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", h["ps1"], "-MainPid", "0", "-Plan", h["plan_path"]],
        check=True, capture_output=True, timeout=60)
    for _ in range(150):  # Windows 满载时 ShellExecute 可能排队，允许最多 15 秒
        if marker.exists():
            break
        time.sleep(0.1)

    assert (dest / "app.txt").read_text(encoding="utf-8") == "NEW"            # 改
    assert (dest / "sub" / "new.txt").read_text(encoding="utf-8") == "ADDED"  # 增（含建子目录）
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["version"] == "0.9.1"  # 清单落地
    assert not (dest / "obsolete.txt").exists()                               # 删废弃
    assert (dest / "sub" / "keep.txt").read_text(encoding="utf-8") == "KEEP"  # 未列文件原封不动
    assert marker.exists()                                                    # 重启确实触发
    assert not staging.exists()                                               # staging 清理


def test_download_delta_cancel_aborts_before_fetch(tmp_path):
    """cancel() 返回真时在任何网络请求前就中止——关窗打断下载的基础，防 QThread 运行中析构崩溃。"""
    info = updater.UpdateInfo(version="0.9.1", notes="",
                              changed=[("a.txt", "deadbeef", 5)], deleted=[], raw={})
    with pytest.raises(InterruptedError):
        updater.download_delta("http://127.0.0.1:1/", info, tmp_path / "stg",
                               cancel=lambda: True)
    assert not (tmp_path / "stg" / "a.txt").exists()  # 取消在文件循环顶生效，未落任何文件
