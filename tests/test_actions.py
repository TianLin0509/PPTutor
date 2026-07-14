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
