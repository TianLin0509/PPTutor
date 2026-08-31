from __future__ import annotations

from pptx_finder import render_client, render_service, renderer


def test_wps_rot_without_powerpnt_pid_does_not_block_owned_preview(monkeypatch):
    monkeypatch.setattr(renderer, "_powerpoint_process_ids", lambda: set())
    renderer._invalidate_powerpoint_active_cache()
    assert renderer._powerpoint_active(force=True) is False


def test_app_for_render_starts_owned_microsoft_app_when_only_wps_is_in_rot(monkeypatch):
    owned = object()
    monkeypatch.setattr(renderer, "_powerpoint_process_ids", lambda: set())
    monkeypatch.setattr(renderer, "_get_app", lambda: owned)
    monkeypatch.setattr(
        renderer,
        "_attach_borrowed_powerpoint",
        lambda: (_ for _ in ()).throw(AssertionError("must not borrow WPS")),
    )
    renderer._state.app = None
    assert renderer._app_for_render(allow_borrowed_session=True) is owned


def test_renderer_service_returns_structured_empty_path_reason(monkeypatch):
    monkeypatch.setattr(renderer, "render_page", lambda *_a, **_k: None)
    monkeypatch.setattr(renderer, "last_error", lambda: "foreign WPS COM server")
    response = render_service.handle_request({
        "id": 7, "op": "render", "path": "x.pptx", "page_no": 1,
    })
    assert response == {
        "id": 7,
        "ok": True,
        "path": "",
        "reason": "foreign WPS COM server",
    }


def test_renderer_client_keeps_child_reason_in_diagnostics(monkeypatch):
    client = render_client.RendererProcessClient()
    monkeypatch.setattr(client, "request", lambda _payload: {
        "ok": True, "path": "", "reason": "PowerPoint ownership failed",
    })
    assert client.render_page(
        "x.pptx", 1, cache_key="k", long_edge=960,
        hi_priority=True, priority=0,
    ) is None
    assert client._last_error == "PowerPoint ownership failed"
