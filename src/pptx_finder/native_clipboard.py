"""Persist Office's native drawing clipboard without retaining cross-process pointers.

GVML is Office's zipped native drawing format (not a screenshot). Fix theme colour
references to the collected appearance so pasting into a different theme stays faithful.
"""
from contextlib import contextmanager
import io
import time
import zipfile

from lxml import etree as ET

FORMAT = "Art::GVML ClipFormat"
NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def freeze_theme_colours(payload: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        if sum(i.file_size for i in source.infolist()) > 100 * 1024 * 1024:
            raise ValueError("所选素材过大，请缩小选择范围。")
        parser = ET.XMLParser(resolve_entities=False, no_network=True)
        theme_names = [n for n in source.namelist() if n.startswith("clipboard/theme/") and n.endswith(".xml")]
        if len(theme_names) != 1:
            raise ValueError("无法确认素材的原始主题，未改变剪贴板。")
        theme = ET.fromstring(source.read(theme_names[0]), parser)
        scheme = theme.find(f".//{{{NS}}}clrScheme")
        if scheme is None:
            raise ValueError("素材缺少主题色信息。")
        palette = {}
        for colour in scheme:
            if len(colour):
                value = colour[0].get("lastClr") or colour[0].get("val")
                if value and len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
                    palette[ET.QName(colour).localname] = value
        for alias, name in {"tx1": "dk1", "tx2": "dk2", "bg1": "lt1", "bg2": "lt2"}.items():
            if name in palette:
                palette[alias] = palette[name]
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as out:
            for name in source.namelist():
                data = source.read(name)
                if name.startswith("clipboard/drawings/") and name.endswith(".xml"):
                    root = ET.fromstring(data, parser)
                    for node in root.iter(f"{{{NS}}}schemeClr"):
                        value = node.get("val")
                        if value in palette:
                            node.tag = f"{{{NS}}}srgbClr"
                            node.set("val", palette[value])
                            # Existing tint, shade, alpha, and luminance transforms remain intact.
                    data = ET.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
                out.writestr(name, data)
    return result.getvalue()


@contextmanager
def opened_clipboard(hwnd=0):
    import win32clipboard as cb
    for attempt in range(10):
        try:
            cb.OpenClipboard(hwnd)
            break
        except Exception:
            if attempt == 9:
                raise RuntimeError("剪贴板正被其他程序占用，请稍后重试。") from None
            time.sleep(0.03)
    try:
        yield cb
    finally:
        cb.CloseClipboard()


def normalize_shapes(*, required=False) -> int:
    """Read completely before replacing; preserve a PNG fallback for non-Office apps."""
    import win32clipboard as cb
    import win32gui
    fmt = cb.RegisterClipboardFormat(FORMAT)
    png_fmt = cb.RegisterClipboardFormat("PNG")
    with opened_clipboard():
        sequence = cb.GetClipboardSequenceNumber()
        if not cb.IsClipboardFormatAvailable(fmt):
            if required:
                raise ValueError("PowerPoint 没有提供可编辑的原生对象格式，未改为截图。")
            return sequence
        data = cb.GetClipboardData(fmt)
        png = cb.GetClipboardData(png_fmt) if cb.IsClipboardFormatAvailable(png_fmt) else None
    normalized = freeze_theme_colours(data)
    hwnd = win32gui.CreateWindowEx(0, "STATIC", "PPT Doctor clipboard", 0,
                                  0, 0, 0, 0, -3, 0, 0, None)
    try:
        with opened_clipboard(hwnd):
            if cb.GetClipboardSequenceNumber() != sequence:
                raise ValueError("剪贴板已被其他程序更改，请重新复制素材后重试。")
            cb.EmptyClipboard()
            cb.SetClipboardData(fmt, normalized)
            if png is not None:
                cb.SetClipboardData(png_fmt, png)
            return cb.GetClipboardSequenceNumber()
    finally:
        win32gui.DestroyWindow(hwnd)
