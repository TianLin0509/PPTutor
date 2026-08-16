"""External open actions must never turn the preview COM server into the user's UI."""
from __future__ import annotations

import sys
import types

from pptx_finder import actions


def test_open_at_page_shell_opens_then_only_attaches_to_the_open_document(monkeypatch, tmp_path):
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"ppt")
    events: list[object] = []

    class FakeView:
        def GotoSlide(self, page_no):
            events.append(("goto", page_no))

    class FakeWindow:
        View = FakeView()

        def Activate(self):
            events.append("activate")

    class FakeWindows:
        Count = 1

        def __call__(self, index):
            assert index == 1
            return FakeWindow()

    class FakePresentation:
        FullName = str(path)
        Windows = FakeWindows()
        Slides = types.SimpleNamespace(Count=12)

    class FakePresentations:
        Count = 1

        def __call__(self, index):
            assert index == 1
            return FakePresentation()

    app = types.SimpleNamespace(Presentations=FakePresentations())

    def get_active_object(_name):
        events.append("attach")
        return app

    def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError("opening a user document must never launch PowerPoint through COM")

    pythoncom = types.SimpleNamespace(
        CoInitialize=lambda: events.append("coinitialize"),
        CoUninitialize=lambda: events.append("couninitialize"),
    )
    client = types.SimpleNamespace(
        GetActiveObject=get_active_object,
        Dispatch=forbidden_dispatch,
        DispatchEx=forbidden_dispatch,
    )
    win32com = types.SimpleNamespace(client=client)
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(
        actions,
        "open_file",
        lambda value: events.append(("shell-open", value)) or True,
    )

    assert actions.open_at_page(str(path), 7) == (True, True)
    assert events.index(("shell-open", str(path))) < events.index("attach")
    assert ("goto", 7) in events
    assert events[-1] == "couninitialize"


def test_presentation_open_state_only_audits_existing_session(monkeypatch, tmp_path):
    target = tmp_path / "editing.pptx"
    other = tmp_path / "other.pptx"
    events: list[str] = []

    class Presentations:
        Count = 2

        def __call__(self, index):
            return types.SimpleNamespace(FullName=str(target if index == 1 else other))

    app = types.SimpleNamespace(Presentations=Presentations())
    pythoncom = types.SimpleNamespace(
        CoInitialize=lambda: events.append("init"),
        CoUninitialize=lambda: events.append("uninit"),
    )
    client = types.SimpleNamespace(
        GetActiveObject=lambda _name: app,
        Dispatch=lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
        DispatchEx=lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    monkeypatch.setattr(actions.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)

    assert actions.presentation_open_state(str(target)) is True
    assert actions.presentation_open_state(str(tmp_path / "closed.pptx")) is False
    assert events == ["init", "uninit", "init", "uninit"]


def test_presentation_open_state_is_uncertain_when_process_exists_but_com_fails(
    monkeypatch,
):
    pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
    client = types.SimpleNamespace(
        GetActiveObject=lambda _name: (_ for _ in ()).throw(RuntimeError("ROT busy"))
    )
    monkeypatch.setattr(actions.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(actions, "_powerpoint_process_running", lambda: True)

    assert actions.presentation_open_state("C:/editing.pptx") is None
