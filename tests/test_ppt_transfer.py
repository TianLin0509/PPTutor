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
    @contextmanager
    def contexts():
        yield [app]
    monkeypatch.setattr(transfer, "existing_apps", contexts)
    monkeypatch.setattr(transfer, "_app_identity", lambda instance: id(instance))
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
            return SimpleNamespace(Slides=SimpleNamespace(Count=1, Item=lambda _: SimpleNamespace(Copy=lambda: None)))
        yield SimpleNamespace(open=open_snapshot)
    monkeypatch.setattr(transfer, "temporary_presentations", context)
    monkeypatch.setattr(transfer, "flush_clipboard", lambda: None)
    transfer.copy_slide(source)
    assert source.read_bytes() == b"owned source"
    assert len(opened) == 1 and not opened[0].exists()


# ---------- 团队素材一键插入：7 个结构化错误码 ----------
class _Shapes:
    def __init__(self, n=2):
        self.Count = n
        self.copied = 0
        self.Left, self.Top, self.Width, self.Height = 100.0, 100.0, 200.0, 80.0

    def Range(self):
        return self

    def Copy(self):
        self.copied += 1


def _material_world(monkeypatch, tmp_path, *, windows=1, readonly=False, view_slide=True, selection=False,
                    source_pages=3, source_shapes=2):
    pasted = []
    target_slide = SimpleNamespace(SlideIndex=5, Shapes=SimpleNamespace(
        Paste=lambda: pasted.append(1) or SimpleNamespace(Count=2, Left=10.0, Top=10.0, Width=50.0,
                                                          Height=20.0, IncrementLeft=lambda d: None,
                                                          IncrementTop=lambda d: None)))
    pres = SimpleNamespace(Name="季度汇报.pptx", ReadOnly=readonly,
                           PageSetup=SimpleNamespace(SlideWidth=960.0, SlideHeight=540.0))

    class View:
        @property
        def Slide(self):
            if not view_slide:
                raise RuntimeError("thumbnail pane")
            return target_slide
    rng = SimpleNamespace(Count=1 if selection else 0, Item=lambda i: target_slide)
    window = SimpleNamespace(Presentation=pres, View=View(), Selection=SimpleNamespace(SlideRange=rng))
    undo = []
    app = SimpleNamespace(Windows=SimpleNamespace(Count=windows), ActiveWindow=window,
                          StartNewUndoEntry=lambda: undo.append(1))

    @contextmanager
    def apps():
        yield [app]
    monkeypatch.setattr(transfer, "existing_apps", apps)
    monkeypatch.setattr(transfer, "_find_apps", lambda: [app])
    shapes = _Shapes(source_shapes)
    opened = []

    class Session:
        def open(self, path):
            opened.append(path)
            return SimpleNamespace(Slides=SimpleNamespace(Count=source_pages,
                                                          Item=lambda i: SimpleNamespace(Shapes=shapes)))

    @contextmanager
    def temp():
        yield Session()
    monkeypatch.setattr(transfer, "temporary_presentations", temp)
    monkeypatch.setattr(transfer, "flush_clipboard", lambda: None)
    pack = tmp_path / "team-materials.pptx"
    pack.write_bytes(b"pptx")
    return SimpleNamespace(pack=pack, pasted=pasted, undo=undo, shapes=shapes, opened=opened)


def test_insert_material_pastes_into_active_slide_from_a_snapshot(monkeypatch, tmp_path):
    w = _material_world(monkeypatch, tmp_path)
    info = transfer.insert_material(w.pack, 2)
    assert info == {"presentation": "季度汇报.pptx", "slide": 5, "shapes": 2}
    assert w.pasted == [1] and w.undo == [1] and w.shapes.copied == 1
    assert w.opened and transfer.Path(w.opened[0]) != w.pack           # 只开临时副本，不开原件


def test_insert_material_uses_thumbnail_pane_selection(monkeypatch, tmp_path):
    w = _material_world(monkeypatch, tmp_path, view_slide=False, selection=True)
    assert transfer.insert_material(w.pack, 1)["slide"] == 5


@pytest.mark.parametrize("kwargs, page, code", [
    ({"windows": 0}, 1, "no_presentation"),
    ({"readonly": True}, 1, "read_only"),
    ({"view_slide": False}, 1, "no_target_slide"),
    ({"source_pages": 3}, 9, "pack_missing"),
    ({"source_shapes": 0}, 1, "pack_missing"),
])
def test_insert_material_error_codes(monkeypatch, tmp_path, kwargs, page, code):
    w = _material_world(monkeypatch, tmp_path, **kwargs)
    with pytest.raises(transfer.MaterialTransferError) as info:
        transfer.insert_material(w.pack, page)
    assert info.value.code == code and str(info.value)
    assert w.pasted == []


def test_insert_material_without_powerpoint_and_missing_pack(monkeypatch, tmp_path):
    @contextmanager
    def no_apps():
        raise transfer.TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。")
        yield
    monkeypatch.setattr(transfer, "existing_apps", no_apps)
    monkeypatch.setattr(transfer, "temporary_presentations",
                        lambda: pytest.fail("没开 PowerPoint 时不应起临时实例"))
    pack = tmp_path / "p.pptx"
    pack.write_bytes(b"x")
    with pytest.raises(transfer.MaterialTransferError) as info:
        transfer.insert_material(pack, 1)
    assert info.value.code == "no_powerpoint"
    with pytest.raises(transfer.MaterialTransferError) as info:
        transfer.insert_material(tmp_path / "gone.pptx", 1)
    assert info.value.code == "pack_missing"


def test_insert_material_busy_and_timeout(monkeypatch, tmp_path):
    import threading
    w = _material_world(monkeypatch, tmp_path)
    hold = threading.Event()
    release = threading.Event()

    def holder():
        with transfer._lock:
            hold.set()
            release.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    hold.wait(5)
    try:
        with pytest.raises(transfer.MaterialTransferError) as info:
            transfer.insert_material(w.pack, 1)
        assert info.value.code == "busy"
    finally:
        release.set()
        t.join()
    with pytest.raises(transfer.MaterialTransferError) as info:
        transfer.call_with_timeout(lambda: threading.Event().wait(1), timeout=0.05)
    assert info.value.code == "timeout"
    assert transfer.call_with_timeout(lambda: 7, timeout=1) == 7


def test_com_call_rejected_maps_to_busy(monkeypatch, tmp_path):
    w = _material_world(monkeypatch, tmp_path)

    class ComError(Exception):
        pass
    err = ComError(-2147418111, "Call was rejected by callee.", None, None)
    err.hresult = -2147418111

    def boom():
        raise err
    w.shapes.Copy = boom
    with pytest.raises(transfer.MaterialTransferError) as info:
        transfer.insert_material(w.pack, 1)
    assert info.value.code == "busy"


def test_material_error_codes_are_the_contract_seven():
    assert set(transfer.MATERIAL_ERRORS) == {"no_powerpoint", "no_presentation", "read_only",
                                             "no_target_slide", "busy", "pack_missing", "timeout"}
    with pytest.raises(ValueError):
        transfer.MaterialTransferError("whatever")
