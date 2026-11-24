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
    instance_id: int = 0


def _app_identity(app) -> int:
    """Process identity survives active-window and ROT enumeration changes."""
    import win32process
    return int(win32process.GetWindowThreadProcessId(int(app.HWND))[1])


def _find_apps():
    """WPS also registers PowerPoint's ROT class ID. Inspect all matching entries."""
    import pythoncom
    import win32com.client
    rot = pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    apps = []
    seen = set()
    for moniker in rot.EnumRunning():
        try:
            name = moniker.GetDisplayName(ctx, None)
            is_app = name.upper() == "!{91493441-5A91-11CF-8700-00AA0060263B}"
            if not is_app and not name.lower().endswith((".pptx", ".ppt", ".pptm")):
                continue
            obj = win32com.client.Dispatch(rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
            app = obj if is_app else obj.Application
            if (Path(str(app.Path)) / "POWERPNT.EXE").is_file():
                identity = _app_identity(app)
                if identity not in seen:
                    apps.append(win32com.client.gencache.EnsureDispatch(app))
                    seen.add(identity)
        except Exception:
            log.debug("unavailable PowerPoint ROT entry", exc_info=True)
    if not apps:
        raise TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。")
    return apps


def _find_app():
    apps = _find_apps()
    # 整页复制也需要一个可编辑的目标；首个实例可能没有文稿。
    return next((app for app in apps if any(_targets(app))), apps[0])


def _targets(app):
    for i in range(1, int(app.Presentations.Count) + 1):
        pres = app.Presentations.Item(i)
        if int(pres.Windows.Count) and not bool(pres.ReadOnly):
            yield pres, PresentationTarget(str(pres.Name), str(pres.FullName),
                                           str(pres.Windows.Item(1).Caption),
                                           int(pres.Slides.Count), _app_identity(app))


@contextmanager
def existing_apps():
    import pythoncom
    with _lock:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            try:
                apps = _find_apps()
            except Exception as exc:
                raise TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。") from exc
            yield apps
        finally:
            pythoncom.CoUninitialize()


@contextmanager
def existing_app():
    with existing_apps() as apps:
        yield apps[0]


def list_presentations() -> list[PresentationTarget]:
    with existing_apps() as apps:
        return [target for app in apps for _, target in _targets(app)]


def _validate_page(page: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise TransferError("页码必须从 1 开始，请重新选择预览页。")


def _insert_slide(pres, path: str, page: int = 1) -> int:
    """Insert once into the explicitly selected deck; path is a caller-owned snapshot."""
    _validate_page(page)
    before = int(pres.Slides.Count)
    try:
        inserted = int(pres.Slides.InsertFromFile(path, before, page, page))
        after = int(pres.Slides.Count)
    except Exception as exc:
        raise TransferError("插入未能确认完成，请先检查目标 PPT，避免重复插入。") from exc
    if inserted != 1 or after != before + 1:
        raise TransferError("插入结果与预期不符，请检查目标 PPT；没有自动重试。")
    return before + 1


def append_slide(source: str | Path, target: PresentationTarget, page: int = 1) -> int:
    """Re-resolve the exact visible window; never fall back to ActivePresentation."""
    _validate_page(page)
    with _source_snapshot(source) as path, existing_apps() as apps:
        matches = []
        for app in apps:
            if target.instance_id and _app_identity(app) != target.instance_id:
                continue
            for pres, current in _targets(app):
                if (current.caption == target.caption and
                        os.path.normcase(current.full_name) == os.path.normcase(target.full_name)):
                    matches.append((app, pres))
        if len(matches) > 1:
            raise TransferError("有多个同名目标窗口，请重新选择要插入的 PPT。")
        if matches:
            app, pres = matches[0]
            # Group the explicit edit into the user's undo history when supported.
            app.StartNewUndoEntry()
            return _insert_slide(pres, path, page)
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
                raise TransferError("请先在 Microsoft PowerPoint 中打开可编辑的目标文稿，再复制整页。")
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


def flush_clipboard(attempts: int = 3):
    """Force Office to render its clipboard formats before closing the deck.

    Read-only, so a short retry is safe: clipboard managers / other apps can hold the
    clipboard for a moment right after Copy (seen in the 2026-11-22 parallel gate).
    """
    import time
    import win32clipboard
    for attempt in range(attempts):
        try:
            _flush_clipboard_once(win32clipboard)
            return
        except Exception:  # noqa: BLE001  pywintypes.error：被别的进程短暂占用
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def _flush_clipboard_once(win32clipboard):
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


@contextmanager
def _source_snapshot(source: str | Path):
    source = Path(source).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="ppt-copy-") as tmp:
        # Preserve .ppt for legacy binary decks, too.
        snapshot = Path(tmp) / ("slide" + source.suffix)
        shutil.copyfile(source, snapshot)
        yield str(snapshot)


def copy_slide(source: str | Path, page: int = 1):
    _validate_page(page)
    with _source_snapshot(source) as snapshot:
        with temporary_presentations(require_open=True) as session:
            pres = session.open(Path(snapshot))
            if page > int(pres.Slides.Count):
                raise TransferError("源 PPT 页数已变化，请刷新预览后重新选择。")
            pres.Slides.Item(page).Copy()
            flush_clipboard()


def copy_material(source: str | Path, slide_index: int = 1):
    """把素材包第 slide_index 页的形状复制成可编辑的原生对象（不需要用户开着 PowerPoint）。"""
    from .native_clipboard import normalize_shapes
    source = Path(source)
    if not source.is_file():
        raise MaterialTransferError("pack_missing")
    with _material_errors():
        with _source_snapshot(source) as snapshot, temporary_presentations() as session:
            pres = session.open(snapshot)
            shapes = _material_shapes(pres, slide_index)
            shapes.Range().Copy()
            normalize_shapes(required=True)


# ---------- 团队素材：一键插入当前 PPT（移植自 HUB-H transferMaterial） ----------
MATERIAL_ERRORS = {
    "no_powerpoint": "先打开要放素材的 PPT，并点一下目标页；不会动你已打开的其他文稿。",
    "no_presentation": "PowerPoint 里没有打开的文稿：先打开要放素材的 PPT，再点插入。",
    "read_only": "当前文稿是只读的：另存一份可编辑的再插入。",
    "no_target_slide": "没点中目标页：在 PowerPoint 左侧缩略图里点一下要放的那页。",
    "busy": "PowerPoint 正忙，或上一个素材还在处理，稍等几秒再试。",
    "pack_missing": "素材包文件不在了：刷新素材库，或重新获取团队素材。",
    "timeout": "PowerPoint 60 秒没有响应：看看它是否弹出了对话框，处理后再试。",
}
#: COM 「对方正忙，稍后再试」类错误
_BUSY_HRESULTS = {-2147418111, -2147417846, -2147417848}


class MaterialTransferError(TransferError):
    """带 code 的插入 / 复制失败；界面按 code 选文案，不解析异常字符串。"""

    def __init__(self, code: str, message: str = ""):
        if code not in MATERIAL_ERRORS:
            raise ValueError(f"unknown material error code: {code}")
        super().__init__(message or MATERIAL_ERRORS[code])
        self.code = code


@contextmanager
def _material_errors():
    try:
        yield
    except MaterialTransferError:
        raise
    except Exception as exc:  # noqa: BLE001  pywintypes.com_error 等
        hresult = getattr(exc, "hresult", None)
        if hresult is None and getattr(exc, "args", None):
            hresult = exc.args[0] if isinstance(exc.args[0], int) else None
        if hresult in _BUSY_HRESULTS:
            raise MaterialTransferError("busy") from exc
        raise


def _material_shapes(pres, slide_index: int):
    if isinstance(slide_index, bool) or not isinstance(slide_index, int) or slide_index < 1:
        raise MaterialTransferError("pack_missing", "素材页码不对：请刷新素材库。")
    if slide_index > int(pres.Slides.Count):
        raise MaterialTransferError("pack_missing", "素材包页数已变化：请刷新素材库。")
    shapes = pres.Slides.Item(slide_index).Shapes
    if not int(shapes.Count):
        raise MaterialTransferError("pack_missing", "这一页素材是空的：请刷新素材库。")
    return shapes


def _zorder() -> list[int]:
    """顶层窗口句柄，按 Z 序从前到后（EnumWindows 的顺序）。"""
    import win32gui
    handles = []
    win32gui.EnumWindows(lambda h, _: handles.append(int(h)) or True, None)
    return handles


def _by_recent_focus(apps):
    """多个 PowerPoint 实例时，最近在前台的那个排第一（主框架窗口在 Z 序里最靠前）。"""
    if len(apps) < 2:
        return list(apps)
    try:
        order = {h: i for i, h in enumerate(_zorder())}
    except Exception:  # noqa: BLE001
        order = {}

    def rank(app):
        try:
            return order.get(int(app.HWND), len(order) + 1)
        except Exception:  # noqa: BLE001
            return len(order) + 2
    return sorted(apps, key=rank)


def _active_target(apps):
    """用户最近在用的那个 PowerPoint 的活动文稿和当前页；不碰其他文稿。

    多个实例并存时按窗口 Z 序取最近在前台的那个（不是 ROT 枚举顺序）；
    实例内取它自己的 ActiveWindow（该实例里最后激活的文稿窗口）。
    """
    saw_window = False
    for app in _by_recent_focus(apps):
        try:
            if not int(app.Windows.Count):
                continue
            window = app.ActiveWindow
            pres = window.Presentation
        except Exception:  # noqa: BLE001  没有活动窗口
            continue
        saw_window = True
        if bool(pres.ReadOnly):
            raise MaterialTransferError("read_only")
        slide = None
        try:
            slide = window.View.Slide
        except Exception:  # noqa: BLE001  焦点在缩略图栏 / 大纲 / 幻灯片浏览视图
            try:
                rng = window.Selection.SlideRange
                if int(rng.Count) >= 1:
                    slide = rng.Item(1)
            except Exception:  # noqa: BLE001
                slide = None
        if slide is None:
            raise MaterialTransferError("no_target_slide")
        return app, pres, slide
    raise MaterialTransferError("no_target_slide" if saw_window else "no_presentation")


def insert_material(pack_path: str | Path, slide_index: int) -> dict:
    """把素材包第 slide_index 页的形状贴到当前活动文稿的当前页。

    只读、无窗口打开素材包的临时副本，只关自己开的那份；不保存、不关闭用户文稿；
    插入记成一步可撤销。失败抛 MaterialTransferError（7 个 code 之一）。
    """
    pack_path = Path(pack_path)
    if not pack_path.is_file():
        raise MaterialTransferError("pack_missing")
    if not _lock.acquire(timeout=0.5):
        raise MaterialTransferError("busy")
    try:
        with _material_errors():
            try:
                with existing_apps() as apps:
                    _active_target(apps)        # 先把「没开 / 只读 / 没点页」问清楚，不起临时实例
            except MaterialTransferError:
                raise
            except TransferError as exc:
                raise MaterialTransferError("no_powerpoint") from exc
            with _source_snapshot(pack_path) as snapshot, temporary_presentations() as session:
                app, pres, slide = _active_target(_find_apps())
                source = session.open(snapshot)
                _material_shapes(source, slide_index).Range().Copy()
                flush_clipboard()
                app.StartNewUndoEntry()
                pasted = slide.Shapes.Paste()
                _keep_on_slide(pres, pasted)
                return {"presentation": str(pres.Name), "slide": int(slide.SlideIndex),
                        "shapes": int(pasted.Count)}
    finally:
        _lock.release()


def _keep_on_slide(pres, shapes):
    """目标文稿比例不同（如 4:3）时，跑出页面的素材整体挪回页面中央。"""
    try:
        sw, sh = float(pres.PageSetup.SlideWidth), float(pres.PageSetup.SlideHeight)
        left, top = float(shapes.Left), float(shapes.Top)
        width, height = float(shapes.Width), float(shapes.Height)
        if left < 0 or top < 0 or left + width > sw or top + height > sh:
            shapes.IncrementLeft((sw - width) / 2 - left)
            shapes.IncrementTop((sh - height) / 2 - top)
    except Exception:  # noqa: BLE001  挪不动不算失败，形状已经贴上
        log.debug("cannot recenter pasted material", exc_info=True)


def call_with_timeout(fn, timeout: float = 60.0):
    """在独立线程里跑 COM 操作；超时抛 timeout（线程继续跑完，期间再点会得到 busy）。"""
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001  原样带回调用方线程
            box["error"] = exc

    thread = threading.Thread(target=work, name="material-transfer", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise MaterialTransferError("timeout")
    if "error" in box:
        raise box["error"]
    return box.get("value")
