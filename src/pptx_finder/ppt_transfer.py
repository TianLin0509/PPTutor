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


def _stamp(app, pid: int, frame: int):
    """记下实例所在进程和主窗口。COM 包装对象的属性会转发给 PowerPoint，只能写进实例字典。"""
    app.__dict__["_pd_pid"] = int(pid)
    app.__dict__["_pd_frame"] = int(frame)
    return app


def _app_hwnd(app) -> int:
    """实例主窗口句柄（窗口 Z 序用）。新版 Office 的 Application.HWND 报「找不到成员」，先用发现时记下的。"""
    frame = getattr(app, "__dict__", {}).get("_pd_frame")
    return int(frame) if frame else int(app.HWND)


def _app_identity(app) -> int:
    """Process identity survives active-window and ROT enumeration changes."""
    stamped = getattr(app, "__dict__", {})
    if "_pd_pid" in stamped:          # 0 = 只在 ROT 里见到、进程未知
        return int(stamped["_pd_pid"])
    import win32process
    return int(win32process.GetWindowThreadProcessId(int(app.HWND))[1])


def _find_apps():
    """找出所有可用的 Microsoft PowerPoint 实例，最近在前台的排前面。

    先按窗口找（每个实例都知道进程和主窗口），再看 ROT 补漏（WPS 也登记 PowerPoint 的类 ID，按
    POWERPNT.EXE 过滤）。2026-11-25 实测（Office 16.0.20625）：Application.HWND 报「找不到成员」，
    11-22 起按它去重让所有 ROT 条目都被丢掉——PowerPoint 开着也报「请先打开 PowerPoint」，
    素材复制 / 插入、搜索页复制本页都受影响；且 Office 首次失去焦点前不登记 ROT（KB238610）。
    """
    import pythoncom
    import win32com.client
    apps = []
    seen = set()
    for pid, frame, app in _window_apps():
        if pid not in seen:
            apps.append(_stamp(app, pid, frame))
            seen.add(pid)
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
            if not (Path(str(app.Path)) / "POWERPNT.EXE").is_file():
                continue
            if any(_same_object(app, known) for known in apps):
                continue
            try:
                identity = _app_identity(app)
            except Exception:  # noqa: BLE001  新版 Office 拿不到 HWND：进程未知
                identity = 0
            if identity and identity in seen:
                continue
            apps.append(_stamp(win32com.client.gencache.EnsureDispatch(app), identity, 0))
            if identity:
                seen.add(identity)
        except Exception:
            log.debug("unavailable PowerPoint ROT entry", exc_info=True)
    if not apps:
        raise TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。")
    return apps


def _deck_names(app) -> set[str]:
    try:
        return {os.path.normcase(str(app.Presentations.Item(i).FullName))
                for i in range(1, int(app.Presentations.Count) + 1)}
    except Exception:  # noqa: BLE001
        return set()


def _same_object(a, b) -> bool:
    """ROT 里的 Application 和从窗口拿到的可能不是同一个 COM 对象：
    先比对象身份，再比打开的文稿（同一份文件只会在一个 PowerPoint 进程里打开）。"""
    try:
        import pythoncom
        if a._oleobj_.QueryInterface(pythoncom.IID_IUnknown) == b._oleobj_.QueryInterface(pythoncom.IID_IUnknown):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(_deck_names(a) & _deck_names(b))


_OBJID_NATIVEOM = 0xFFFFFFF0
_WM_GETOBJECT = 0x003D


def _window_apps() -> list:
    """不经 ROT：按 Z 序（前台在先）枚举 PowerPoint 主窗口（PPTFrameClass），向文稿区要原生对象模型。

    返回 [(进程号, 主窗口句柄, Application)]。只读取，不激活、不改动窗口；对方挂起时 3 秒超时跳过。
    启动页还没开文稿的实例没有文稿区，这里拿不到（ROT 可能补上）。WPS 的窗口类不同，不会混进来。
    """
    import pythoncom
    import win32com.client
    import win32con
    import win32gui
    import win32process
    frames, out = [], []

    def collect(handle, _):
        try:
            if win32gui.GetClassName(handle) == "PPTFrameClass":
                frames.append(handle)
        except Exception:  # noqa: BLE001  窗口刚关闭
            pass
        return True
    try:
        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001
        log.debug("cannot enumerate PowerPoint windows", exc_info=True)
        return out
    for frame in frames:
        panes = []

        def child(handle, _):
            try:
                if win32gui.GetClassName(handle) == "mdiClass":
                    panes.append(handle)
            except Exception:  # noqa: BLE001
                pass
            return True
        try:
            win32gui.EnumChildWindows(frame, child, None)
            pid = int(win32process.GetWindowThreadProcessId(frame)[1])
        except Exception:  # noqa: BLE001  窗口刚关闭
            continue
        for pane in panes:
            try:
                _, lresult = win32gui.SendMessageTimeout(
                    pane, _WM_GETOBJECT, 0, _OBJID_NATIVEOM, win32con.SMTO_ABORTIFHUNG, 3000)
                if not lresult:
                    continue
                window = win32com.client.Dispatch(
                    pythoncom.ObjectFromLresult(lresult, pythoncom.IID_IDispatch, 0))
                app = window.Application
                if (Path(str(app.Path)) / "POWERPNT.EXE").is_file():
                    out.append((pid, frame, win32com.client.gencache.EnsureDispatch(app)))
                    break
            except Exception:  # noqa: BLE001  正忙 / 弹着对话框 / 权限级别更高
                log.debug("PowerPoint window has no native object model", exc_info=True)
    return out


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


def _copy_settled(obj, timeout: float = 10.0, fmt_name: str | None = "Art::GVML ClipFormat"):
    """Copy() 返回时 PowerPoint 可能还没把内容放上剪贴板（大素材更明显）。

    2026-11-25 实测：整页模板「规划与计划」等大素材 3 次复制 3 次失败——立刻去读剪贴板时
    原生格式还没到、或 PowerPoint 正在占用。等到剪贴板序号变化、原生格式出现并稳定 0.1 秒再往下走。
    """
    import time
    import win32clipboard
    fmt = win32clipboard.RegisterClipboardFormat(fmt_name) if fmt_name else 0
    before = win32clipboard.GetClipboardSequenceNumber()
    obj.Copy()
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        seq = win32clipboard.GetClipboardSequenceNumber()
        if seq != before and (not fmt or win32clipboard.IsClipboardFormatAvailable(fmt)):
            if seq == last:
                return
            last = seq
        time.sleep(0.1)
    log.warning("clipboard did not settle within %.0fs after PowerPoint Copy", timeout)


def _clipboard_quiet(quiet: float = 0.3, timeout: float = 3.0):
    """等剪贴板序号连续 quiet 秒不变（PowerPoint 复制大素材后还会陆续改写剪贴板）。"""
    import time
    import win32clipboard
    last, since = win32clipboard.GetClipboardSequenceNumber(), time.monotonic()
    deadline = since + timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        seq = win32clipboard.GetClipboardSequenceNumber()
        if seq != last:
            last, since = seq, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return


def _normalize_settled(normalize, attempts: int = 3):
    """把 PowerPoint 放上来的原生形状改成「主题色固定」的版本。

    2026-11-25 实测（434 条素材逐条复制）：大素材在我们读剪贴板时，PowerPoint 还在延迟渲染、
    会关掉我们打开的剪贴板（1418）或再改写一次（序号变化）。先逼它渲染完、等剪贴板安静，
    改写前发现被改过就重读重来；内容仍是刚复制的这份素材，重读不会重复粘贴任何东西。
    """
    import time
    for attempt in range(attempts):
        try:
            flush_clipboard()
            _clipboard_quiet()
            return normalize(required=True)
        except ValueError as exc:
            if "已被其他程序更改" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.2)


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
            _copy_settled(pres.Slides.Item(page), fmt_name=None)   # 整页没有 GVML 格式
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
            _copy_settled(shapes.Range())
            _normalize_settled(normalize_shapes)


# ---------- 团队素材：一键插入当前 PPT（移植自 HUB-H transferMaterial） ----------
MATERIAL_ERRORS = {
    "no_powerpoint": "先打开要放素材的 PPT，并点一下目标页；不会动你已打开的其他文稿。",
    "no_presentation": "PowerPoint 里没有打开的文稿：先打开要放素材的 PPT，再点插入。",
    "read_only": "当前文稿是只读的：另存一份可编辑的再插入。",
    "no_target_slide": "没点中目标页：在 PowerPoint 左侧缩略图里点一下要放的那页。",
    "busy": "PowerPoint 正忙，或上一个素材还在处理，稍等几秒再试。",
    "pack_missing": "素材包文件不在了：刷新素材库，或重新获取团队素材。",
    "timeout": "PowerPoint 60 秒没有响应：看看它是否弹出了对话框，处理后再试。",
    "ppt_unreachable": "连不上已打开的 PowerPoint（可能停在启动页、弹着对话框或以管理员身份运行）：打开一份文稿、关掉弹窗后再试。",
    "clipboard_busy": "剪贴板正被别的程序占用（截图、翻译或剪贴板工具），稍等几秒再点。",
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
        from .renderer import PowerPointSessionBusy
        if isinstance(exc, PowerPointSessionBusy):
            # 内部英文原因只进日志；界面给中文原因和下一步。
            log.warning("material transfer cannot reach PowerPoint: %s", exc)
            raise MaterialTransferError("ppt_unreachable") from exc
        if "剪贴板正被" in str(exc) or "Clipboard" in str(getattr(exc, "funcname", "") or ""):
            raise MaterialTransferError("clipboard_busy") from exc
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


AMBIGUOUS_TARGET = ("开着多个 PowerPoint，分不清要插到哪一个：先点一下目标 PPT 的窗口再插入，"
                    "或关掉不用的那个；也可以改用复制形状。")


def _visible_apps(apps) -> list:
    """有可见文稿窗口的实例；读不到窗口数说明实例正忙，直接停。"""
    out = []
    for app in apps:
        try:
            if int(app.Windows.Count):
                out.append(app)
        except Exception as exc:  # noqa: BLE001
            raise MaterialTransferError("busy") from exc
    return out


def _pick_instance(apps):
    """确定用户最近在用的那个 PowerPoint 实例；不确定就停，不按 ROT 顺序猜（m4 MF-M2-05）。"""
    visible = _visible_apps(apps)
    if not visible:
        return None
    if len(visible) == 1:
        return visible[0]
    try:
        order = {h: i for i, h in enumerate(_zorder())}
        ranks = [order[_app_hwnd(app)] for app in visible]
    except Exception as exc:  # noqa: BLE001  Z 序读不到 / 某实例不在 Z 序里
        raise MaterialTransferError("no_target_slide", AMBIGUOUS_TARGET) from exc
    return visible[ranks.index(min(ranks))]


def _active_target(apps):
    """用户最近在用的那个 PowerPoint 的活动文稿和当前页；不碰其他文稿。

    多个实例并存时按窗口 Z 序取最近在前台的那个；Z 序不可用就停下说明，不跨实例回落。
    选定实例后，它的活动窗口 / 文稿 / 当前页任何一步读不到都直接报错，不再看别的实例。
    """
    app = _pick_instance(apps)
    if app is None:
        raise MaterialTransferError("no_presentation")
    try:
        window = app.ActiveWindow
        pres = window.Presentation
    except Exception as exc:  # noqa: BLE001
        raise MaterialTransferError("no_target_slide") from exc
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
                _copy_settled(_material_shapes(source, slide_index).Range())
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


def export_material_deck(pack_path: str | Path, slide_index: int, name: str = "素材") -> Path:
    """不经 PowerPoint COM，把素材包第 slide_index 页另存成一份单页 PPT（兜底：复制 / 插入连不上 PowerPoint 时）。

    存到临时目录，文件名带时间戳避免被已打开的旧副本锁住；返回路径由界面交给系统默认程序打开。
    """
    import re
    import time
    from pptx import Presentation
    pack_path = Path(pack_path)
    if not pack_path.is_file():
        raise MaterialTransferError("pack_missing")
    prs = Presentation(str(pack_path))
    ids = list(prs.slides._sldIdLst)
    if isinstance(slide_index, bool) or not isinstance(slide_index, int) or not 1 <= slide_index <= len(ids):
        raise MaterialTransferError("pack_missing", "素材包页数已变化：请刷新素材库。")
    keep = ids[slide_index - 1]
    for sid in ids:
        if sid is not keep:
            prs.part.drop_rel(sid.rId)
            prs.slides._sldIdLst.remove(sid)
    folder = Path(tempfile.gettempdir()) / "PPT Doctor 素材"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\/:*?"<>|\s]+', "_", name).strip("._")[:40] or "素材"
    target = folder / f"{safe}-{time.strftime('%H%M%S')}.pptx"
    prs.save(str(target))
    return target
