"""PPT Doctor 内源无感下载记录器单测（2026-11-25）。"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path

import pytest

from pptx_finder import openx_recorder, openx_source, updater


def test_inactive_by_default_in_dev_and_respects_kill_switch(monkeypatch):
    monkeypatch.delenv("PPTX_FINDER_OPENX_RECORD", raising=False)
    monkeypatch.delenv("PPTX_FINDER_OPENX", raising=False)
    assert openx_recorder.is_active(frozen=False) is False
    assert openx_recorder.is_active(frozen=True) is True
    monkeypatch.setenv("PPTX_FINDER_OPENX_RECORD", "0")
    assert openx_recorder.is_active(frozen=True) is False


def test_record_dedupe_and_diagnostics(tmp_path):
    calls = []

    def fake_run(target):
        calls.append(target)
        return {
            "ok": True,
            "method": "dom-click",
            "took_ms": 80,
            "api_paths": ["/openxApi/project/version/release/download"],
            "api_requests": ["POST /openxApi/project/version/release/download"],
        }

    rec = openx_recorder.OpenxDownloadRecorder(data_dir=tmp_path, run_session=fake_run)
    r1 = rec.record(release_version="ppt-doctor-update-2.1.18", file_name="abc", background=False)
    assert r1["status"] == "recorded"
    assert len(calls) == 1
    assert calls[0]["page_url"] == openx_source.PAGE_URL

    # 同一版本不重复计数
    r2 = rec.record(release_version="ppt-doctor-update-2.1.18", file_name="def", background=False)
    assert r2["status"] == "recorded"
    assert r2.get("cached") is True
    assert len(calls) == 1

    # 组件独立计数
    r3 = rec.record(release_version="ppt-doctor-ocr-1.2.0", file_name="component.zip", kind="component:ocr", background=False)
    assert r3["status"] == "recorded"
    assert len(calls) == 2

    diag = rec.diagnostics()
    assert diag["recorded_count"] == 2
    assert diag["pending_count"] == 0
    assert diag["stats"]["succeeded"] == 2
    assert diag["stats"]["failed"] == 0
    assert diag["stats"]["last_api_requests"] == ["POST /openxApi/project/version/release/download"]


def test_fast_restart_keeps_pending_and_resumes_next_launch(tmp_path):
    started = threading.Event()
    gate = threading.Event()

    def slow_run(_target):
        started.set()
        gate.wait(timeout=5.0)
        return {"ok": False, "transient": True, "reason": "interrupted"}

    first = openx_recorder.OpenxDownloadRecorder(data_dir=tmp_path, run_session=slow_run)
    th = first.record(release_version="ppt-doctor-update-2.1.19", background=True)
    assert started.wait(timeout=2.0)
    # 模拟用户秒点「立即重启」：旧进程 dispose 不得抹掉已落盘的 pending_queue
    first.dispose()
    gate.set()
    th.join(timeout=2.0)

    raw = json.loads((tmp_path / openx_recorder.RECORD_FILE).read_text(encoding="utf-8"))
    assert len(raw["pending_queue"]) == 1
    assert raw["pending_queue"][0]["release_version"] == "ppt-doctor-update-2.1.19"
    assert raw["stats"]["failed"] == 0

    # 新版启动补记
    resumed = []
    second = openx_recorder.OpenxDownloadRecorder(
        data_dir=tmp_path,
        run_session=lambda t: (resumed.append(t["release_version"]) or {"ok": True, "took_ms": 40}),
    )
    out = second.resume_pending(delay_sec=0.0, background=False)
    assert out and out[0]["status"] == "recorded"
    assert resumed == ["ppt-doctor-update-2.1.19"]
    assert second.diagnostics()["pending_count"] == 0


def test_failure_is_silent_and_retries_once_on_transient(tmp_path):
    rec = openx_recorder.OpenxDownloadRecorder(
        data_dir=tmp_path,
        run_session=lambda _t: {"ok": False, "transient": True, "reason": "需要登录", "took_ms": 50},
    )
    res = rec.record(release_version="ppt-doctor-update-2.1.20", background=False)
    assert res["status"] == "failed"
    diag = rec.diagnostics()
    assert diag["stats"]["failed"] == 1
    assert diag["stats"]["last_reason"] == "需要登录"
    assert diag["pending_count"] == 1


def test_download_update_from_openx_triggers_recorder_without_affecting_result(tmp_path, monkeypatch):
    payload = b"ppt-doctor-delta-payload"
    digest = hashlib.sha256(payload).hexdigest()
    recorded = []

    monkeypatch.setattr(openx_recorder, "record_download_async", lambda **kw: recorded.append(kw))
    src = updater.OpenxSource(
        label="openx",
        manifest_url="openx:manifest.json",
        block_url="openx:{hash}",
        package_url="",
    )
    monkeypatch.setattr(updater.OpenxSource, "block", lambda self, h: f"https://file.openx.huawei.com/openx-file/{h}")

    class _FakeHttp(io.BytesIO):
        def __init__(self, data: bytes, url: str):
            super().__init__(data)
            self._url = url
            self.headers = {"Content-Length": str(len(data)), "Content-Type": "application/octet-stream"}

        def geturl(self):
            return self._url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda url, timeout=None: _FakeHttp(payload, str(url)))
    info = updater.UpdateInfo(
        version="2.1.18",
        notes="test",
        changed=[("app.bin", digest, len(payload))],
        deleted=[],
        raw={"version": "2.1.18", "files": {"app.bin": {"hash": digest, "size": len(payload)}}},
        source=src,
        sources=[src],
    )
    staging = tmp_path / "staging"
    out = updater.download_update("https://unused.invalid", info, staging)
    assert (out / "app.bin").read_bytes() == payload
    assert len(recorded) == 1
    assert recorded[0]["release_version"] == "ppt-doctor-update-2.1.18"
    assert recorded[0]["page_url"] == openx_source.PAGE_URL


def test_sanitize_and_download_url_helpers():
    assert openx_recorder.sanitize_api_path("https://openx.huawei.com/openxApi/project/12345/download?t=1") == "/openxApi/project/:id/download"
    assert openx_recorder.sanitize_api_path("https://example.invalid/a") == ""
    assert openx_recorder.is_download_like_url("https://file.openx.huawei.com/openx-file/abc.zip", "abc.zip") is True
    assert openx_recorder.is_download_like_url("https://openx.huawei.com/openxApi/visitOpenXRecord/saveRecord") is False
