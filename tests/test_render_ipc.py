from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

from pptx_finder import render_client, render_service, renderer


def test_render_service_render_request_returns_path(monkeypatch, tmp_path):
    out = tmp_path / "page.png"

    def fake_render(
        path,
        page_no,
        cache_key=None,
        long_edge=2560,
        hi_priority=False,
        priority=None,
        use_snapshot=False,
    ):
        assert path == "deck.pptx"
        assert page_no == 2
        assert cache_key == "k"
        assert long_edge == 720
        assert hi_priority is True
        assert priority == 0
        assert use_snapshot is True
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
        "use_snapshot": True,
    })

    assert resp == {"id": 7, "ok": True, "path": str(out)}


def test_render_service_propagates_existing_session_only(monkeypatch, tmp_path):
    out = tmp_path / "prefetched.png"
    seen = []

    def fake_render(*_args, existing_session_only=False, **_kwargs):
        seen.append(bool(existing_session_only))
        return out

    monkeypatch.setattr(renderer, "render_page", fake_render)

    resp = render_service.handle_request({
        "id": 8,
        "op": "render",
        "path": "deck.pptx",
        "page_no": 3,
        "existing_session_only": True,
    })

    assert resp == {"id": 8, "ok": True, "path": str(out)}
    assert seen == [True]


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

    def fake_direct(path, page_no, cache_key=None, long_edge=2560, hi_priority=False, priority=None, use_snapshot=False):
        called.append((Path(path).name, page_no, long_edge, hi_priority, priority, use_snapshot))
        return tmp_path / "direct.png"

    monkeypatch.setattr(renderer, "_render_page_direct", fake_direct)

    assert renderer.render_page("deck.pptx", 3, long_edge=480, priority=5) == tmp_path / "direct.png"
    assert called == [("deck.pptx", 3, 480, False, 5, False)]


def test_render_service_waits_for_idle_parent_without_timeout(monkeypatch):
    parent_sock, child_sock = socket.socketpair()
    child_sock.settimeout(0.02)
    results = []
    errors = []

    def fake_handle_request(req):
        return {"id": req.get("id"), "ok": True, "shutdown": True}

    monkeypatch.setattr(render_service, "handle_request", fake_handle_request)

    def run_server():
        try:
            results.append(render_service.serve(child_sock, "tok"))
        except BaseException as exc:  # noqa: BLE001 - test must capture thread exceptions
            errors.append(exc)

    thread = threading.Thread(target=run_server)
    thread.start()
    with parent_sock:
        parent_file = parent_sock.makefile("rwb", buffering=0)
        hello = json.loads(parent_file.readline().decode("utf-8"))
        assert hello["type"] == "hello"
        assert hello["token"] == "tok"

        time.sleep(0.08)
        assert thread.is_alive()

        parent_file.write(json.dumps({"id": 1, "op": "shutdown"}).encode("utf-8") + b"\n")
        resp = json.loads(parent_file.readline().decode("utf-8"))
        assert resp == {"id": 1, "ok": True, "shutdown": True}

    thread.join(1)
    assert thread.is_alive() is False
    assert errors == []
    assert results == [0]


def test_render_service_parent_connect_timeout_exits_quietly(monkeypatch):
    def fake_connect(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(render_service.socket, "create_connection", fake_connect)

    assert render_service.main(["pptdoctor", "--renderer-worker", "12345", "tok"]) == 0
