"""A temporary COM refusal must not block a healthy page for 90 seconds."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import pywintypes
import winerror

from pptx_finder import renderer


@pytest.fixture
def preview(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "_state", threading.local())
    monkeypatch.setattr(renderer, "_failed_until", {})
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_kw: False)
    source = tmp_path / "live.pptx"
    source.write_bytes(b"source must remain unchanged")
    calls = {"open": [], "export": 0, "close": 0, "release": 0}
    failure = {"error": None}

    class FakeSlides:
        Count = 2

        def __call__(self, page):
            assert page == 2
            return self

        def Export(self, out, fmt, width, height):
            calls["export"] += 1
            if failure["error"] is not None:
                raise failure["error"]
            Path(out).write_bytes(b"exported PNG")

    class Presentation:
        Slides = FakeSlides()
        PageSetup = SimpleNamespace(SlideWidth=960, SlideHeight=540)

        def Close(self):
            calls["close"] += 1

    def open_pres(path, ReadOnly, WithWindow):
        calls["open"].append((path, ReadOnly, WithWindow))
        return Presentation()

    app = SimpleNamespace(Presentations=SimpleNamespace(Open=open_pres))
    monkeypatch.setattr(renderer, "_app_for_render", lambda **_kw: app)

    def release():
        calls["release"] += 1

    monkeypatch.setattr(renderer, "_release_local_app_reference", release)

    def render():
        return renderer.render_page(
            str(source), 2, cache_key="transient", long_edge=960,
            use_snapshot=True, hi_priority=True, allow_borrowed_session=True,
        )

    yield render, failure, calls, source
    renderer._close_pres()


@pytest.mark.parametrize("code", [
    winerror.RPC_E_CALL_REJECTED,
    winerror.RPC_E_SERVERCALL_RETRYLATER,
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_busy_com_page_can_retry_immediately_after_powerpoint_recovers(preview, code, wrapped):
    render, failure, calls, source = preview
    # Office may return the code directly or inside IDispatch EXCEPINFO.
    failure["error"] = pywintypes.com_error(
        winerror.DISP_E_EXCEPTION if wrapped else code,
        "localized Office message",
        (0, None, "localized Office message", None, 0, code) if wrapped else None,
        None,
    )
    assert render() is None
    failure["error"] = None
    assert render() is not None, "recovered COM must be called again without a 90s wait"
    assert calls["export"] == 2
    assert calls["close"] == 0, "temporary busy is not a corrupt presentation"
    assert calls["release"] == 0
    assert len(calls["open"]) == 1
    path, readonly, window = calls["open"][0]
    assert Path(path) != source and (readonly, window) == (1, 0)
    assert source.read_bytes() == b"source must remain unchanged"
    assert renderer.last_error() == ""


@pytest.mark.parametrize("error", [
    pywintypes.com_error(winerror.E_FAIL, "document failure", None, None),
    pywintypes.com_error(
        winerror.DISP_E_EXCEPTION, "busy text alone is not proof",
        (0, None, "RPC_E_CALL_REJECTED", None, 0, winerror.E_FAIL), None,
    ),
    RuntimeError("RPC_E_CALL_REJECTED"),
])
def test_unknown_or_document_errors_keep_failure_throttling(preview, error):
    render, failure, calls, _source = preview
    failure["error"] = error
    assert render() is None
    failure["error"] = None
    assert render() is None
    assert calls["export"] == 1
    assert calls["close"] == 1 and calls["release"] == 1
    assert "throttled" in renderer.last_error()


def test_persistently_busy_com_does_not_loop_or_forget_owned_snapshot(preview):
    render, failure, calls, _source = preview
    failure["error"] = pywintypes.com_error(
        winerror.RPC_E_CALL_REJECTED, "busy", None, None,
    )
    for _ in range(3):
        assert render() is None
    assert calls["export"] == 3
    assert calls["close"] == 0 and calls["release"] == 0
    assert len(renderer._state.owned_presentations) == 1
    assert "busy" in renderer.last_error()
    renderer._close_pres()
    assert calls["close"] == 1
    assert renderer._state.owned_presentations == []
