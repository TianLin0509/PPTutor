"""RenderWorker 预取调度：预览优先 + 预取低优先填缓存 + 新预览作废旧预取（mock COM）。"""
from __future__ import annotations

import threading
import time

from pptx_finder import renderer
from pptx_finder.ui.render_worker import RenderWorker


def _fake_renderer(monkeypatch, tmp_path, delay=0.05):
    calls: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None, **_kwargs):
        with lock:
            calls.append((page_no, hi_priority))
        time.sleep(delay)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "background_powerpoint_allowed", lambda: True, raising=False)
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
        qtbot.waitUntil(lambda: len(calls) >= 3, timeout=2500)  # 含交互空闲保护窗口
        with lock:
            got = list(calls)
        assert (1, True) in got               # 预览走高优先
        assert (2, False) in got and (3, False) in got  # 预取走低优先、确实渲染填缓存
    finally:
        w.stop()
        w.wait(3000)


def test_clicked_preview_never_borrows_active_user_powerpoint_session(
    qtbot,
    monkeypatch,
    tmp_path,
):
    calls: list[dict] = []

    def fake_render(*_args, **kwargs):
        calls.append(dict(kwargs))
        return tmp_path / "rendered.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)
    w = RenderWorker()
    w.start()
    try:
        with qtbot.waitSignal(w.rendered, timeout=1000):
            w.request(1, "clicked.pptx", 3)
        w.prefetch("clicked.pptx", 4)
        qtbot.waitUntil(lambda: len(calls) >= 2, timeout=1500)

        assert "allow_foreign_session" not in calls[0]
        assert calls[0].get("existing_session_only", False) is False
        assert "allow_foreign_session" not in calls[1]
        assert calls[1]["existing_session_only"] is True
    finally:
        w.stop()
        w.wait(3000)


def test_idle_after_preview_releases_hidden_powerpoint(qtbot, monkeypatch, tmp_path):
    """A preview session must not linger for Explorer to reuse later."""
    released = threading.Event()
    monkeypatch.setattr(
        renderer,
        "render_page",
        lambda *_args, **_kwargs: tmp_path / "preview.png",
    )
    monkeypatch.setattr(renderer, "shutdown", released.set)
    monkeypatch.setattr(RenderWorker, "_SESSION_IDLE_RELEASE_SEC", 0.03, raising=False)

    w = RenderWorker()
    w.start()
    try:
        with qtbot.waitSignal(w.rendered, timeout=1000):
            w.request(1, "f.pptx", 1)
        assert released.wait(0.5), "hidden PowerPoint was kept alive after preview became idle"
    finally:
        w.stop()
        w.wait(3000)


def test_idle_release_failure_does_not_kill_future_preview_requests(qtbot, monkeypatch, tmp_path):
    """A transient shutdown/RPC error must not leave the UI spinning forever."""
    shutdown_calls = 0

    def flaky_shutdown():
        nonlocal shutdown_calls
        shutdown_calls += 1
        if shutdown_calls == 1:
            raise RuntimeError("PowerPoint rejected shutdown")

    monkeypatch.setattr(
        renderer,
        "render_page",
        lambda _path, page_no, **_kwargs: tmp_path / f"{page_no}.png",
    )
    monkeypatch.setattr(renderer, "shutdown", flaky_shutdown)
    monkeypatch.setattr(RenderWorker, "_SESSION_IDLE_RELEASE_SEC", 0.03, raising=False)

    w = RenderWorker()
    w.start()
    try:
        with qtbot.waitSignal(w.rendered, timeout=1000):
            w.request(1, "first.pptx", 1)
        qtbot.waitUntil(lambda: shutdown_calls >= 1, timeout=1000)

        with qtbot.waitSignal(w.rendered, timeout=1000) as recovered:
            w.request(2, "second.pptx", 2)
        assert recovered.args == [2, str(tmp_path / "2.png")]
        assert w.isRunning()
    finally:
        w.stop()
        w.wait(3000)


def test_rapid_preview_burst_eventually_releases_hidden_powerpoint(
    qtbot, monkeypatch, tmp_path
):
    """Repeated page changes may delay, but must not cancel, idle cleanup."""
    rendered_pages: list[int] = []
    shutdowns: list[float] = []

    def fake_render(_path, page_no, **_kwargs):
        rendered_pages.append(page_no)
        time.sleep(0.005)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: shutdowns.append(time.monotonic()))
    monkeypatch.setattr(RenderWorker, "_SESSION_IDLE_RELEASE_SEC", 0.03, raising=False)

    w = RenderWorker()
    w.start()
    try:
        for page_no in range(1, 21):
            w.request(page_no, "burst.pptx", page_no)
            time.sleep(0.002)

        qtbot.waitUntil(lambda: bool(rendered_pages), timeout=1000)
        qtbot.waitUntil(lambda: bool(shutdowns), timeout=1500)
        assert w._idle_session_releases >= 1
        assert w.isRunning()
    finally:
        w.stop()
        w.wait(3000)


def test_preview_and_prefetch_reuse_the_same_safe_snapshot(qtbot, monkeypatch, tmp_path):
    calls: list[tuple[int, bool, bool]] = []
    lock = threading.Lock()

    def fake_render(
        path,
        page_no,
        cache_key=None,
        long_edge=2560,
        hi_priority=False,
        priority=None,
        use_snapshot=False,
        **_kwargs,
    ):
        with lock:
            calls.append((page_no, bool(hi_priority), bool(use_snapshot)))
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "background_powerpoint_allowed", lambda: True, raising=False)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.start()
    try:
        with qtbot.waitSignal(w.rendered, timeout=1000):
            w.request(1, "f.pptx", 1)
        w.prefetch("f.pptx", 2)
        qtbot.waitUntil(lambda: len(calls) >= 2, timeout=1500)
        with lock:
            got = list(calls)
        assert (1, True, True) in got
        assert (2, False, True) in got
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

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None, **_kwargs):
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


def test_prewarm_routes_through_renderer_safety_gate(qtbot, monkeypatch):
    events: list[str] = []

    def forbidden_direct_com():
        raise AssertionError("prewarm must not bypass renderer safety gate")

    monkeypatch.setattr(renderer, "_get_app", forbidden_direct_com)
    monkeypatch.setattr(renderer, "prewarm", lambda: events.append("prewarm"), raising=False)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.prewarm()
    try:
        w.start()
        qtbot.waitUntil(lambda: events == ["prewarm"], timeout=1000)
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

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None, **_kwargs):
        with lock:
            calls.append((page_no, hi_priority))
        time.sleep(0.05)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(RenderWorker, "_PREFETCH_IDLE_GRACE_SEC", 0.2, raising=False)
    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "background_powerpoint_allowed", lambda: True, raising=False)
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

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None, **_kwargs):
        with lock:
            calls.append((path, page_no, cache_key, hi_priority))
        started.set()
        release.wait(1)
        return tmp_path / f"{page_no}.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "background_powerpoint_allowed", lambda: True, raising=False)
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

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None, **_kwargs):
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


def test_prefetch_reuses_existing_session_when_background_start_is_blocked(qtbot, monkeypatch, tmp_path):
    calls: list[dict] = []

    monkeypatch.setattr(renderer, "background_powerpoint_allowed", lambda: False, raising=False)

    def fake_render(*_args, **kwargs):
        calls.append(dict(kwargs))
        return tmp_path / "x.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.start()
    try:
        w.prefetch("f.pptx", 2)
        qtbot.waitUntil(lambda: len(calls) == 1, timeout=1000)
        assert calls[0]["existing_session_only"] is True
    finally:
        w.stop()
        w.wait(3000)


def test_primary_prefetch_starts_quickly_without_raising_concurrency(qtbot, monkeypatch, tmp_path):
    started = threading.Event()
    started_at: list[float] = []

    def fake_render(*_args, **_kwargs):
        started_at.append(time.perf_counter())
        started.set()
        return tmp_path / "x.png"

    monkeypatch.setattr(renderer, "render_page", fake_render)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)
    w = RenderWorker()
    w.start()
    try:
        requested_at = time.perf_counter()
        w.prefetch("f.pptx", 2)

        assert started.wait(0.32), "primary neighbor should begin before a normal page-turn"
        assert started_at[0] - requested_at < 0.30
    finally:
        w.stop()
        w.wait(3000)


def test_release_session_runs_cleanup_on_render_thread_and_clears_queued_work(
    qtbot, monkeypatch
):
    shutdown_threads: list[int] = []
    caller_thread = threading.get_ident()
    monkeypatch.setattr(
        renderer,
        "shutdown",
        lambda: shutdown_threads.append(threading.get_ident()),
    )
    w = RenderWorker()
    w.prefetch("old.pptx", 2)
    w.start()
    try:
        qtbot.waitUntil(w.isRunning, timeout=1000)
        assert w.release_session(timeout_sec=1.0) is True
        assert shutdown_threads
        assert shutdown_threads[0] != caller_thread
        assert list(w._prefetch) == []
        assert w._preview is None
    finally:
        w.stop()
        w.wait(3000)


def test_release_session_never_hard_aborts_an_inflight_render(monkeypatch):
    """An external-open handoff must not strand the child-owned POWERPNT.EXE.

    Killing the renderer Python child does not kill the separate PowerPoint COM
    server it launched.  A timed-out handoff should therefore fail cleanly and
    let the render finish/clean up in its own apartment instead of producing a
    hash-named ghost presentation in the taskbar.
    """
    started = threading.Event()
    unblock = threading.Event()
    aborts = []

    def stuck_render(*_args, **_kwargs):
        started.set()
        unblock.wait(2)
        return None

    def abort_inflight():
        aborts.append(time.perf_counter())
        unblock.set()
        return True

    monkeypatch.setattr(renderer, "render_page", stuck_render)
    monkeypatch.setattr(renderer, "abort_inflight", abort_inflight, raising=False)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)
    w = RenderWorker()
    w.start()
    try:
        w.request(1, "stuck.pptx", 1)
        assert started.wait(1)
        before = time.perf_counter()
        assert w.release_session(timeout_sec=0.08) is False
        assert time.perf_counter() - before < 0.3
        assert aborts == []
    finally:
        unblock.set()
        w.stop()
        w.wait(3000)


def test_stop_first_allows_inflight_renderer_to_finish_cooperatively(monkeypatch):
    started = threading.Event()
    unblock = threading.Event()
    aborts = []

    def stuck_render(*_args, **_kwargs):
        started.set()
        unblock.wait(2)
        return None

    def abort_inflight():
        aborts.append(True)
        unblock.set()
        return True

    monkeypatch.setattr(renderer, "render_page", stuck_render)
    monkeypatch.setattr(renderer, "abort_inflight", abort_inflight, raising=False)
    monkeypatch.setattr(renderer, "shutdown", lambda: None)

    w = RenderWorker()
    w.start()
    try:
        w.request(1, "stuck.pptx", 1)
        assert started.wait(1)
        w.stop()
        assert w.wait(50) is False
        assert aborts == []
        unblock.set()
        assert w.wait(800)
    finally:
        unblock.set()
        if w.isRunning():
            w.wait(3000)
