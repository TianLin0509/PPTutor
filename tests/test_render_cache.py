from __future__ import annotations

import os

from pptx_finder import renderer


def test_render_cache_maintenance_is_bounded_and_keeps_unrelated_assets(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    render_files = []
    for index in range(5):
        path = tmp_path / f"0123456789abcdef_{index + 1}_1600.png"
        path.write_bytes(b"x" * 100)
        os.utime(path, (100 + index, 100 + index))
        render_files.append(path)
    unrelated = tmp_path / "logo.png"
    unrelated.write_bytes(b"keep")

    result = renderer.maintain_render_cache(max_bytes=250, max_files=3)

    survivors = [path for path in render_files if path.exists()]
    assert len(survivors) <= 2
    assert sum(path.stat().st_size for path in survivors) <= 250
    assert survivors == render_files[-len(survivors):]
    assert unrelated.exists()
    assert result["deleted"] >= 3


def test_stale_snapshot_cleanup_keeps_current_and_recent_files(monkeypatch, tmp_path):
    snapshot_dir = tmp_path / "render_snapshots"
    snapshot_dir.mkdir()
    old = snapshot_dir / "old.pptx"
    current = snapshot_dir / "current.pptx"
    recent = snapshot_dir / "recent.pptx"
    for path in (old, current, recent):
        path.write_bytes(b"ppt")

    now = 10_000.0
    os.utime(old, (now - 500, now - 500))
    os.utime(current, (now - 500, now - 500))
    os.utime(recent, (now - 10, now - 10))
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer.time, "time", lambda: now)
    renderer._state.snapshot_path = str(current)
    renderer._state.stale_snapshots_checked = False

    assert renderer._cleanup_stale_snapshots(max_age_sec=100) == 1
    assert not old.exists()
    assert current.exists()
    assert recent.exists()

    # One pass per renderer session prevents repeated directory walks.
    assert renderer._cleanup_stale_snapshots(max_age_sec=0) == 0
