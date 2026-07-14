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


def test_render_service_render_once_closes_the_historical_presentation(monkeypatch, tmp_path):
    out = tmp_path / "history.png"
    events = []

    monkeypatch.setattr(
        renderer,
        "render_page",
        lambda *_args, **_kwargs: events.append("render") or out,
    )
    monkeypatch.setattr(
        renderer,
        "close_current_presentation",
        lambda: events.append("close"),
    )

    resp = render_service.handle_request({
        "id": 19,
        "op": "render_once",
        "path": "history.pptx",
        "page_no": 1,
        "long_edge": 360,
    })

    assert resp == {"id": 19, "ok": True, "path": str(out)}
    assert events == ["render", "close"]


def test_renderer_client_abort_unblocks_request_without_retry():
    parent_sock, child_sock = socket.socketpair()
    client = render_client.RendererProcessClient(request_timeout=5.0)
    request_seen = threading.Event()
    release_server = threading.Event()
    errors = []
    starts = []

    class FakeProc:
        def __init__(self):
            self.dead = False

        def poll(self):
            return 0 if self.dead else None

        def terminate(self):
            self.dead = True

        def kill(self):
            self.dead = True

        def wait(self, timeout=None):
            self.dead = True
            return 0

    client._proc = FakeProc()
    client._sock = parent_sock
    client._file = parent_sock.makefile("rwb", buffering=0)
    original_start = client._start_locked

    def counted_start():
        starts.append(True)
        if len(starts) > 1:
            raise AssertionError("an explicitly aborted request must never restart")
        return original_start()

    client._start_locked = counted_start

    def server():
        with child_sock:
            f = child_sock.makefile("rwb", buffering=0)
            if f.readline():
                request_seen.set()
            release_server.wait(2)

    server_thread = threading.Thread(target=server)
    request_thread = threading.Thread(
        target=lambda: _capture_exception(
            errors,
            lambda: client.request({"op": "render", "path": "deck.pptx"}),
        )
    )
    server_thread.start()
    request_thread.start()
    try:
        assert request_seen.wait(1)
        started = time.perf_counter()
        assert client.abort() is True
        request_thread.join(1)
        assert not request_thread.is_alive()
        assert time.perf_counter() - started < 0.5
        assert errors
        assert len(starts) == 1
    finally:
        release_server.set()
        request_thread.join(1)
        server_thread.join(1)
        client.abort()


def _capture_exception(errors, fn):
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - explicit thread assertion helper
        errors.append(exc)


def test_renderer_diagnostics_never_waits_for_an_active_request_lock():
    client = render_client.RendererProcessClient()
    holding = threading.Event()
    release = threading.Event()

    def hold_request_lock():
        with client._lock:
            holding.set()
            release.wait(1)

    thread = threading.Thread(target=hold_request_lock)
    thread.start()
    try:
        assert holding.wait(1)
        timer = threading.Timer(0.2, release.set)
        timer.start()
        started = time.perf_counter()
        lines = client.diagnostic_lines()
        elapsed = time.perf_counter() - started
        timer.cancel()
        assert elapsed < 0.05
        assert "busy=True" in lines[0]
    finally:
        release.set()
        thread.join(1)


def test_external_open_waits_for_headless_powerpoint_to_exit(monkeypatch):
    process_states = iter(({77}, {77}, set()))
    monkeypatch.setattr(
        renderer,
        "_powerpoint_process_ids",
        lambda: next(process_states, set()),
    )
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: False)

    assert renderer.wait_for_external_open_ready(timeout_sec=0.2) is True


def test_external_open_refuses_a_persistent_headless_powerpoint(monkeypatch):
    monkeypatch.setattr(renderer, "_powerpoint_process_ids", lambda: {77})
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: False)

    assert renderer.wait_for_external_open_ready(timeout_sec=0.0) is False
