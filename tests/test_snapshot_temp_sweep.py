"""留底暂存副本清扫：防 %TEMP% 被 .pptdoctor-snapshot-* 堆爆（同事实测 C 盘满）。

泄漏链：stable_snapshot_source 每次留底在 %TEMP% 暂存完整副本；删除失败被静默
吞掉或进程被杀即永久残留，且此前无任何清扫机制。
"""
from __future__ import annotations

import os
import time

from pptx_finder.versioning import vault


def test_sweep_deletes_only_prefix_files(tmp_path):
    stale1 = tmp_path / ".pptdoctor-snapshot-aaa111.pptx"
    stale2 = tmp_path / ".pptdoctor-snapshot-bbb222.pptx"
    keep = tmp_path / "other-temp-file.tmp"
    for p in (stale1, stale2, keep):
        p.write_bytes(b"x")
    assert vault.sweep_stale_snapshot_temps(tempdir=str(tmp_path)) == 2
    assert not stale1.exists() and not stale2.exists()
    assert keep.exists()


def test_sweep_respects_max_age(tmp_path):
    fresh = tmp_path / ".pptdoctor-snapshot-young1.pptx"
    old = tmp_path / ".pptdoctor-snapshot-old001.pptx"
    fresh.write_bytes(b"x")
    old.write_bytes(b"x")
    old_time = time.time() - 7200
    os.utime(old, (old_time, old_time))
    assert vault.sweep_stale_snapshot_temps(max_age_sec=3600, tempdir=str(tmp_path)) == 1
    assert fresh.exists() and not old.exists()


def test_sweep_missing_dir_returns_zero(tmp_path):
    assert vault.sweep_stale_snapshot_temps(tempdir=str(tmp_path / "nope")) == 0


def test_unlink_snapshot_tmp_retries_transient_lock(tmp_path, monkeypatch):
    target = tmp_path / ".pptdoctor-snapshot-retry.pptx"
    target.write_bytes(b"x")
    calls = {"n": 0}
    real_unlink = os.unlink

    def flaky(path):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(13, "Permission denied")
        return real_unlink(path)

    monkeypatch.setattr(vault.os, "unlink", flaky)
    monkeypatch.setattr(vault.time, "sleep", lambda _s: None)
    vault._unlink_snapshot_tmp(str(target))
    assert calls["n"] == 3
    assert not target.exists()


def test_unlink_snapshot_tmp_gives_up_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        vault.os, "unlink", lambda _p: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    monkeypatch.setattr(vault.time, "sleep", lambda _s: None)
    with caplog.at_level("WARNING", logger=vault.log.name):
        vault._unlink_snapshot_tmp(str(tmp_path / "x.pptx"))  # 不抛，只告警
    assert any("cleanup failed" in r.message for r in caplog.records)
