"""Explicit real Office validation on newly created, isolated fixture decks only."""
from __future__ import annotations

import json
import os
import uuid
import struct
import zlib
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from pptx_finder import materials, ppt_transfer, renderer


def main(output=None):
    root = Path(output or "artifacts/material-transfer-check").resolve() / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PPTX_FINDER_DATA_DIR"] = str(root / "appdata")
    import pythoncom
    import win32com.client
    before_pids = renderer._powerpoint_process_ids()
    if before_pids is None:
        raise RuntimeError("Cannot audit PowerPoint process ownership before the fixture run")
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    app = win32com.client.gencache.EnsureDispatch(win32com.client.DispatchEx("PowerPoint.Application"))
    new_pids = (renderer._powerpoint_process_ids() or set()) - before_pids
    owned_pid = next(iter(new_pids)) if len(new_pids) == 1 else None
    owned_handle = renderer._open_owned_powerpoint_handle(owned_pid) if owned_pid else None
    before_count = int(app.Presentations.Count)
    owned = []
    checks = {}
    worker = ThreadPoolExecutor(max_workers=1)
    def call(fn, *args):
        print("CHECK", fn.__name__, flush=True)
        return worker.submit(fn, *args).result(timeout=90)
    try:
        source = app.Presentations.Add(1)
        owned.append(source)
        source.PageSetup.SlideWidth = 960
        source.PageSetup.SlideHeight = 540
        slide = source.Slides.Add(1, 12)
        shape = slide.Shapes.AddShape(33, 60, 80, 220, 90)
        shape.TextFrame.TextRange.Text = "可编辑箭头"
        shape.Fill.ForeColor.RGB = 0x3344CC
        source.SaveAs(str(root / "source.pptx"), 24)
        dest = app.Presentations.Add(1)
        owned.append(dest)
        dest.Slides.Add(1, 12)
        dest.SaveAs(str(root / "destination.pptx"), 24)
        target = next(t for t in call(ppt_transfer.list_presentations) if t.full_name == dest.FullName)
        active_before = str(app.ActivePresentation.FullName)
        shape.Copy()
        library = materials.MaterialLibrary(root / "library")
        item = call(library.capture, "红色箭头", "箭头")
        checks["capture_preserves_active_window"] = str(app.ActivePresentation.FullName) == active_before
        call(library.copy, item.id)
        pasted = dest.Slides.Item(1).Shapes.Paste()
        checks["native_shape_after_source_close"] = int(pasted.Item(1).Type) == 1
        checks["editable_text"] = pasted.Item(1).TextFrame.TextRange.Text == "可编辑箭头"
        checks["shape_colour"] = pasted.Item(1).Fill.ForeColor.RGB == 0x3344CC
        source.SlideMaster.Theme.ThemeColorScheme.Colors(5).RGB = 0x228855
        themed = slide.Shapes.AddShape(5, 400, 220, 180, 90)
        themed.Fill.ForeColor.ObjectThemeColor = 5
        expected_theme_colour = int(themed.Fill.ForeColor.RGB)
        themed.Copy()
        themed_item = call(library.capture, "主题色圆角框", "框")
        call(library.copy, themed_item.id)
        copied_theme = dest.Slides.Item(1).Shapes.Paste().Item(1)
        checks["theme_colour_preserved"] = int(copied_theme.Fill.ForeColor.RGB) == expected_theme_colour
        # A PNG with transparent margins must stay an embedded picture, not a slide background.
        def chunk(kind, data):
            return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        pixels = b"".join(b"\0" + b"".join(bytes((40, 140, 210, 255 if 8 < x < 56 and 8 < y < 56 else 0))
                                            for x in range(64)) for y in range(64))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
        picture_path = root / "transparent.png"
        picture_path.write_bytes(png)
        picture = slide.Shapes.AddPicture(str(picture_path), 0, -1, 320, 80, 64, 64)
        picture.Copy()
        picture_item = call(library.capture, "透明 Logo", "图片")
        call(library.copy, picture_item.id)
        pasted_picture = dest.Slides.Item(1).Shapes.Paste().Item(1)
        checks["picture_stays_picture"] = int(pasted_picture.Type) == 13
        with zipfile.ZipFile(library.folder(picture_item.id) / "material.pptx") as archive:
            checks["transparent_image_bytes_preserved"] = png in [archive.read(n) for n in archive.namelist() if n.startswith("ppt/media/")]
        call(library.export_pack, [item.id, picture_item.id], root / "素材包.pptx")
        other = materials.MaterialLibrary(root / "other-library")
        imported = call(other.import_pack, root / "素材包.pptx")
        checks["pack_metadata"] = [(x.name, x.category) for x in imported] == [("红色箭头", "箭头"), ("透明 Logo", "图片")]
        call(other.copy, imported[0].id)
        pasted = dest.Slides.Item(1).Shapes.Paste()
        checks["native_after_pack_roundtrip"] = int(pasted.Item(1).Type) == 1
        checks["text_after_pack_roundtrip"] = pasted.Item(1).TextFrame.TextRange.Text == "可编辑箭头"
        call(ppt_transfer.copy_slide, root / "source.pptx")
        # The hidden source is now closed. Paste must still be a real slide.
        count = int(dest.Slides.Count)
        dest.Slides.Paste(count + 1)
        checks["whole_slide_clipboard"] = int(dest.Slides.Count) == count + 1
        checks["slide_text_editable"] = dest.Slides.Item(count + 1).Shapes.Item(1).TextFrame.TextRange.Text == "可编辑箭头"
        page = call(ppt_transfer.append_slide, root / "source.pptx", target)
        checks["append_exact_target"] = page == count + 2 and int(source.Slides.Count) == 1
        checks["no_temporary_decks_left"] = int(app.Presentations.Count) == before_count + 2
        checks["destination_remains_unsaved"] = not bool(dest.Saved)
        (root / "result.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({"checks": checks, "result": str(root / "result.json")}, ensure_ascii=False))
        assert all(checks.values()), checks
    finally:
        worker.shutdown(wait=True)
        for pres in reversed(owned):
            pres.Saved = True
            pres.Close()
        # Office can linger after Quit while its native slide clipboard is alive.
        # Close only the exact process created by this fixture, and only if empty.
        if owned_pid is not None and owned_handle is not None and int(app.Presentations.Count) == 0:
            import win32gui
            import win32process
            def hide_owned_empty_frame(hwnd, _):
                if (win32process.GetWindowThreadProcessId(hwnd)[1] == owned_pid
                        and win32gui.GetClassName(hwnd) == "PPTFrameClass"):
                    win32gui.ShowWindow(hwnd, 0)
            win32gui.EnumWindows(hide_owned_empty_frame, None)
            if not renderer._request_owned_powerpoint_exit(app, owned_pid, owned_handle=owned_handle):
                raise RuntimeError("Fixture PowerPoint process did not exit; no other process was touched")
        renderer._close_process_handle(owned_handle)
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
