"""Explicit PowerPoint transfers. Own only temporary presentations, never user decks.

All COM objects stay in one apartment. No retry of a mutating paste/insert: an
uncertain result must be reported, never duplicated by retrying the operation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading
import shutil
import tempfile

log = logging.getLogger(__name__)
_lock = threading.RLock()


class TransferError(RuntimeError):
    pass


@dataclass(frozen=True)
class PresentationTarget:
    name: str
    full_name: str
    caption: str
    slides: int


def _find_app():
    """WPS also registers PowerPoint's ROT class ID. Inspect all matching entries."""
    import pythoncom
    import win32com.client
    rot = pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    for moniker in rot.EnumRunning():
        try:
            name = moniker.GetDisplayName(ctx, None)
            is_app = name.upper() == "!{91493441-5A91-11CF-8700-00AA0060263B}"
            if not is_app and not name.lower().endswith((".pptx", ".ppt", ".pptm")):
                continue
            obj = win32com.client.Dispatch(rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
            app = obj if is_app else obj.Application
            if (Path(str(app.Path)) / "POWERPNT.EXE").is_file():
                return win32com.client.gencache.EnsureDispatch(app)
        except Exception:
            log.debug("unavailable PowerPoint ROT entry", exc_info=True)
    raise TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。")


def _targets(app):
    for i in range(1, int(app.Presentations.Count) + 1):
        pres = app.Presentations.Item(i)
        if int(pres.Windows.Count) and not bool(pres.ReadOnly):
            yield pres, PresentationTarget(str(pres.Name), str(pres.FullName),
                                           str(pres.Windows.Item(1).Caption),
                                           int(pres.Slides.Count))


@contextmanager
def existing_app():
    import pythoncom
    with _lock:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            try:
                app = _find_app()
            except Exception as exc:
                raise TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。") from exc
            yield app
        finally:
            pythoncom.CoUninitialize()


def list_presentations() -> list[PresentationTarget]:
    with existing_app() as app:
        return [target for _, target in _targets(app)]


def append_slide(source: str | Path, target: PresentationTarget) -> int:
    """Re-resolve the exact visible window; never fall back to ActivePresentation."""
    path = str(Path(source).resolve(strict=True))
    with existing_app() as app:
        for pres, current in _targets(app):
            if (current.caption == target.caption and
                    os.path.normcase(current.full_name) == os.path.normcase(target.full_name)):
                before = int(pres.Slides.Count)
                # Group the explicit edit into the user's undo history when supported.
                app.StartNewUndoEntry()
                try:
                    inserted = int(pres.Slides.InsertFromFile(path, before, 1, 1))
                except Exception as exc:
                    raise TransferError("插入未能确认完成，请先检查目标 PPT，避免重复插入。") from exc
                if inserted != 1 or int(pres.Slides.Count) != before + 1:
                    raise TransferError("插入结果与预期不符，请检查目标 PPT；没有自动重试。")
                return before + 1
        raise TransferError("目标 PPT 已关闭或窗口已变化，请重新选择。")


class TemporaryPresentations:
    """Use the existing renderer's audited activation/exit ownership safeguards."""
    def __init__(self, app):
        self.app = app
        self.owned = []

    def _own(self, pres):
        from . import renderer
        self.owned.append(pres)
        renderer._state.owned_presentations = self.owned
        if int(pres.Windows.Count):
            raise TransferError("临时演示文稿意外显示为窗口，已停止操作。")
        return pres

    def new(self):
        pres = self._own(self.app.Presentations.Add(0))
        pres.PageSetup.SlideWidth = 960
        pres.PageSetup.SlideHeight = 540
        return pres

    def close(self, pres):
        # Remove by identity only after successful close; otherwise cleanup retries it.
        pres.Saved = True
        pres.Close()
        self.owned[:] = [p for p in self.owned if p is not pres]

    def open(self, path):
        # Always a caller-owned snapshot, never an original company/user file.
        return self._own(self.app.Presentations.Open(str(Path(path).resolve()),
                                                    ReadOnly=1, WithWindow=0))


@contextmanager
def temporary_presentations(*, require_open=False):
    from . import renderer
    import pythoncom
    with _lock:
        session = None
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            renderer._state.com_initialized_by_renderer = True
            try:
                app = _find_app()
            except TransferError:
                if require_open:
                    raise TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再复制整页。") from None
                app = renderer._get_app()
            else:
                renderer._state.app = app
                renderer._state.app_mode = "borrowed"
                renderer._state.app_owned_pid = None
                renderer._state.app_owned_handle = None
            if require_open and not any(_targets(app)):
                raise TransferError("请先打开一个可编辑的目标 PPT，再复制整页。")
            session = TemporaryPresentations(app)
            yield session
        finally:
            if session is not None:
                for pres in session.owned:
                    # Discard only presentations created/opened by this operation.
                    try:
                        pres.Saved = True
                    except Exception:
                        log.warning("cannot mark owned temporary deck saved", exc_info=True)
                if not renderer._close_pres():
                    raise TransferError("临时 PPT 未能关闭，请稍后重试；未关闭你的演示文稿。")
            renderer._release_local_app_reference()


def flush_clipboard():
    import win32clipboard
    # Force Office to render its clipboard formats before closing the deck.
    # The Office process owns these OLE storage handles; never republish pointers.
    win32clipboard.OpenClipboard()
    try:
        fmt = 0
        while True:
            fmt = win32clipboard.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt >= 0xC000:
                name = win32clipboard.GetClipboardFormatName(fmt)
                if name.startswith("PowerPoint") or name == "Art::GVML ClipFormat":
                    win32clipboard.GetClipboardData(fmt)
    finally:
        win32clipboard.CloseClipboard()


def copy_slide(source: str | Path):
    with tempfile.TemporaryDirectory(prefix="ppt-copy-") as tmp:
        snapshot = Path(tmp) / "slide.pptx"
        shutil.copyfile(source, snapshot)
        with temporary_presentations(require_open=True) as session:
            pres = session.open(snapshot)
            pres.Slides.Item(1).Copy()
            flush_clipboard()


def copy_material(source: str | Path):
    from .native_clipboard import normalize_shapes
    with temporary_presentations() as session:
        pres = session.open(source)
        shapes = pres.Slides.Item(1).Shapes
        if not int(shapes.Count):
            raise TransferError("素材中没有可复制的对象。")
        shapes.Range().Copy()
        normalize_shapes(required=True)
