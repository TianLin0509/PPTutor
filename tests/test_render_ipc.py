from __future__ import annotations

from pathlib import Path

from pptx_finder import render_client, render_service, renderer


def test_render_service_render_request_returns_path(monkeypatch, tmp_path):
    out = tmp_path / "page.png"

    def fake_render(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        assert path == "deck.pptx"
        assert page_no == 2
        assert cache_key == "k"
        assert long_edge == 720
        assert hi_priority is True
        assert priority == 0
        return out

    monkeypatch.setattr(renderer, "render_page", fake_render)

    resp = render_service.handle_request({
        "id": 7,
        "op": "render",
        "path": "deck.pptx",
        "page_no": 2,
        "cache_key": "k",
        "long_edge": 720,
        "hi_priority": True,
        "priority": 0,
    })

    assert resp == {"id": 7, "ok": True, "path": str(out)}


def test_renderer_ipc_disabled_in_source_by_default(monkeypatch):
    monkeypatch.delenv("PPTUTOR_RENDERER_CHILD", raising=False)
    monkeypatch.delenv("PPTUTOR_RENDERER_IPC", raising=False)
    monkeypatch.delattr(render_client.sys, "frozen", raising=False)

    assert render_client.should_use_ipc() is False
    assert renderer.diagnostic_lines() == ["renderer_ipc: enabled=False frozen=False"]


def test_renderer_ipc_can_be_forced_by_env(monkeypatch):
    monkeypatch.delenv("PPTUTOR_RENDERER_CHILD", raising=False)
    monkeypatch.setenv("PPTUTOR_RENDERER_IPC", "1")

    assert render_client.should_use_ipc() is True


def test_renderer_wrapper_uses_direct_path_when_ipc_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTUTOR_RENDERER_IPC", "0")
    called = []

    def fake_direct(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None):
        called.append((Path(path).name, page_no, long_edge, hi_priority, priority))
        return tmp_path / "direct.png"

    monkeypatch.setattr(renderer, "_render_page_direct", fake_direct)

    assert renderer.render_page("deck.pptx", 3, long_edge=480, priority=5) == tmp_path / "direct.png"
    assert called == [("deck.pptx", 3, 480, False, 5)]
