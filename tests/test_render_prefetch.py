"""RenderWorker 预取调度：预览优先 + 预取低优先填缓存 + 新预览作废旧预取（mock COM）。"""
from __future__ import annotations

import threading
import time

from pptx_finder import renderer
from pptx_finder.ui.render_worker import RenderWorker


def _fake_renderer(monkeypatch, tmp_path, delay=0.05):
    calls: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        with lock:
            calls.append((page_no, hi_priority))
        time.sleep(delay)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)
    return calls, lock


def test_preview_then_prefetch(qtbot, monkeypatch, tmp_path):
    calls, lock = _fake_renderer(monkeypatch, tmp_path)
    w = RenderWorker()
    w.start()
    try:
        with qtbot.waitSignal(w.rendered, timeout=2000):
            w.request(1, "f.pptx", 1)        # 预览 page1
        w.prefetch("f.pptx", 2)
        w.prefetch("f.pptx", 3)
        qtbot.wait(600)                       # 等预取跑完（含低优先空闲等待）
        with lock:
            got = list(calls)
        assert (1, True) in got               # 预览走高优先
        assert (2, False) in got and (3, False) in got  # 预取走低优先、确实渲染填缓存
    finally:
        w.stop()
        w.wait(3000)


def test_new_preview_cancels_pending_prefetch(qtbot, monkeypatch, tmp_path):
    calls, lock = _fake_renderer(monkeypatch, tmp_path, delay=0.08)
    w = RenderWorker()
    w.start()
    try:
        w.prefetch("f.pptx", 10)
        w.prefetch("f.pptx", 11)
        w.prefetch("f.pptx", 12)
        with qtbot.waitSignal(w.rendered, timeout=2000):
            w.request(99, "f.pptx", 5)        # 新预览 → 清空待预取
        qtbot.wait(250)
        with lock:
            pages = [p for p, _ in calls]
        assert 5 in pages                                  # 预览渲了
        assert len([p for p in pages if p in (10, 11, 12)]) <= 1  # 待预取被作废（至多 1 个在飞）
    finally:
        w.stop()
        w.wait(3000)


def test_preview_cancels_queued_prewarm(qtbot, monkeypatch, tmp_path):
    events: list[tuple[str, int | None, bool | None]] = []
    lock = threading.Lock()

    def fake_warm():
        with lock:
            events.append(("warm", None, None))
        return object()

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        with lock:
            events.append(("preview", page_no, hi_priority))
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "_get_app", fake_warm)
    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.prewarm()
    w.request(42, "f.pptx", 9)
    try:
        with qtbot.waitSignal(w.rendered, timeout=2000):
            w.start()
        with lock:
            got = list(events)
        assert got
        assert got[0] == ("preview", 9, True)
        assert ("warm", None, None) not in got
    finally:
        w.stop()
        w.wait(3000)


def test_prewarm_skips_when_preview_is_pending():
    w = RenderWorker()

    w.request(43, "f.pptx", 10)
    w.prewarm()

    assert w._warm is False


def test_preview_preempts_prefetch_during_idle_grace(qtbot, monkeypatch, tmp_path):
    calls: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        with lock:
            calls.append((page_no, hi_priority))
        time.sleep(0.05)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(RenderWorker, "_PREFETCH_IDLE_GRACE_SEC", 0.2, raising=False)
    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.start()
    try:
        w.prefetch("f.pptx", 2)
        qtbot.wait(50)
        with qtbot.waitSignal(w.rendered, timeout=2000):
            w.request(99, "f.pptx", 5)

        with lock:
            got = list(calls)
        assert got
        assert got[0] == (5, True)
    finally:
        w.stop()
        w.wait(3000)


def test_prefetch_dedupes_pending_pages():
    w = RenderWorker()

    w.prefetch("f.pptx", 2)
    w.prefetch("f.pptx", 2)
    w.prefetch("f.pptx", 2)

    assert list(w._prefetch) == [("f.pptx", 2, None, 960, RenderWorker._PRIORITY_PREFETCH)]
    lines = "\n".join(w.diagnostic_lines())
    assert "prefetch_pending=1" in lines
    assert "deduped=2" in lines


def test_render_worker_diagnostics_track_preview_clearing_prefetch():
    w = RenderWorker()

    w.prefetch("f.pptx", 2)
    w.prefetch("f.pptx", 3)
    w.request(1, "f.pptx", 1)

    lines = "\n".join(w.diagnostic_lines())
    assert "preview_pending=True" in lines
    assert "prefetch_pending=0" in lines
    assert "preview=0/1" in lines
    assert "cleared=2" in lines


def test_prefetch_dedupes_inflight_page(qtbot, monkeypatch, tmp_path):
    calls = []
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        with lock:
            calls.append((path, page_no, cache_key, hi_priority))
        started.set()
        release.wait(1)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.start()
    try:
        w.prefetch("f.pptx", 2)
        assert started.wait(1)
        w.prefetch("f.pptx", 2)
        release.set()
        qtbot.wait(250)

        with lock:
            got = list(calls)
        assert got == [("f.pptx", 2, None, False)]
    finally:
        release.set()
        w.stop()
        w.wait(3000)


def test_preview_render_error_emits_failure_and_worker_survives(qtbot, monkeypatch, tmp_path):
    calls: list[int] = []

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        calls.append(page_no)
        if page_no == 7:
            raise RuntimeError("COM render failed")
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.start()
    try:
        with qtbot.waitSignal(w.rendered, timeout=1000) as failed:
            w.request(1, "broken.pptx", 7)
        assert failed.args == [1, ""]

        with qtbot.waitSignal(w.rendered, timeout=1000) as recovered:
            w.request(2, "ok.pptx", 8)
        assert recovered.args == [2, str(tmp_path / "8.png")]
        assert calls == [7, 8]
    finally:
        w.stop()
        w.wait(3000)
