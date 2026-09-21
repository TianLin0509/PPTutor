from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pptx_finder import ppt_transfer as transfer


def app_with_decks(monkeypatch, decks):
    app = SimpleNamespace(Presentations=SimpleNamespace(Count=len(decks), Item=lambda i: decks[i-1]),
                          StartNewUndoEntry=lambda: None)
    @contextmanager
    def context():
        yield app
    monkeypatch.setattr(transfer, "existing_app", context)
    return app


def deck(path="C:/work/汇报.pptx", visible=True, readonly=False):
    slides = SimpleNamespace(Count=3)
    calls = []
    def insert(*args):
        calls.append(args)
        slides.Count += 1
        return 1
    slides.InsertFromFile = insert
    return SimpleNamespace(Name="汇报.pptx", FullName=path, ReadOnly=readonly, Slides=slides,
                           Windows=SimpleNamespace(Count=int(visible), Item=lambda _: SimpleNamespace(Caption=path)),
                           calls=calls)


def test_only_open_visible_editable_decks_are_listed(monkeypatch):
    app_with_decks(monkeypatch, [deck(), deck(visible=False), deck(readonly=True)])
    assert len(transfer.list_presentations()) == 1


def test_same_basename_does_not_insert_into_wrong_file(monkeypatch, tmp_path):
    a, b = deck("C:/one/汇报.pptx"), deck("C:/two/汇报.pptx")
    app_with_decks(monkeypatch, [a, b])
    target = transfer.list_presentations()[1]
    source = tmp_path / "source.pptx"
    source.touch()
    assert transfer.append_slide(source, target) == 4
    assert not a.calls
    assert len(b.calls) == 1
    assert b.calls[0][1:] == (3, 1, 1)


def test_closed_target_never_falls_back_to_active_presentation(monkeypatch, tmp_path):
    a = deck()
    app = app_with_decks(monkeypatch, [a])
    target = transfer.list_presentations()[0]
    app.Presentations.Count = 0
    source = tmp_path / "source.pptx"
    source.touch()
    with pytest.raises(transfer.TransferError, match="已关闭"):
        transfer.append_slide(source, target)
    assert not a.calls


def test_uncertain_insert_is_never_retried(monkeypatch, tmp_path):
    a = deck()
    app_with_decks(monkeypatch, [a])
    target = transfer.list_presentations()[0]
    calls = []
    def uncertain(*args):
        calls.append(args)
        a.Slides.Count += 1
        raise RuntimeError("RPC disconnected after mutation")
    a.Slides.InsertFromFile = uncertain
    source = tmp_path / "source.pptx"
    source.touch()
    with pytest.raises(transfer.TransferError, match="检查目标"):
        transfer.append_slide(source, target)
    assert len(calls) == 1


def test_whole_slide_copy_requires_a_live_user_deck_and_uses_a_snapshot(monkeypatch, tmp_path):
    source = tmp_path / "user.pptx"
    source.write_bytes(b"owned source")
    opened = []
    @contextmanager
    def context(*, require_open=False):
        assert require_open is True
        def open_snapshot(path):
            assert path != source
            assert path.read_bytes() == source.read_bytes()
            opened.append(path)
            return SimpleNamespace(Slides=SimpleNamespace(Item=lambda _: SimpleNamespace(Copy=lambda: None)))
        yield SimpleNamespace(open=open_snapshot)
    monkeypatch.setattr(transfer, "temporary_presentations", context)
    monkeypatch.setattr(transfer, "flush_clipboard", lambda: None)
    transfer.copy_slide(source)
    assert source.read_bytes() == b"owned source"
    assert len(opened) == 1 and not opened[0].exists()
