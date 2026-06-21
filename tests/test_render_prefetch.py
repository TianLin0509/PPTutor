"""RenderWorker 预取调度：预览优先 + 预取低优先填缓存 + 新预览作废旧预取（mock COM）。"""
from __future__ import annotations

import threading
import time

from pptx_finder import renderer
from pptx_finder.ui.render_worker import RenderWorker


def _fake_renderer(monkeypatch, tmp_path, delay=0.05):
    calls: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False):
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
        qtbot.wait(350)                       # 等预取跑完
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
