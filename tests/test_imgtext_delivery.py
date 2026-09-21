from types import SimpleNamespace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QInputDialog

from pptx_finder.ui import imgtext_window as module
from pptx_finder.ppt_transfer import PresentationTarget


def test_conversion_uses_cache_and_two_delivery_buttons(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(module.imgtext_ocr, "is_installed", lambda: True)
    monkeypatch.setattr(module.imgtext_ocr, "recognize_one", lambda _: [])
    saves = []
    def convert(source, target, rows):
        saves.append(target)
        Path(target).write_bytes(b"pptx")
        return SimpleNamespace(blocks=[1], runs=[1], skipped_small=[], skipped_shape=[],
                               skipped_busy=[], skipped_formula=[])
    monkeypatch.setattr(module.imgtext, "convert", convert)
    def forbidden(*args):
        raise AssertionError("conversion must not ask for a save filename")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", forbidden)
    source = tmp_path / "image.png"
    img = QImage(40, 30, QImage.Format_RGB32)
    img.fill(Qt.white)
    img.save(str(source))
    win = module.ImgTextWindow({}, source=str(source))
    qtbot.addWidget(win)
    win.show()
    qtbot.mouseClick(win._convert_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks)
    assert len(saves) == 1
    assert Path(saves[0]).is_relative_to(tmp_path / "data" / "cache" / "imgtext-results")
    assert win._copy_btn.isEnabled() and win._insert_btn.isEnabled()
    copied = []
    monkeypatch.setattr(module.ppt_transfer, "copy_slide", copied.append)
    qtbot.mouseClick(win._copy_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks)
    assert copied == saves
    assert "缩略图区" in win._status.text()
    target = PresentationTarget("目标.pptx", "C:/work/目标.pptx", "目标", 10)
    monkeypatch.setattr(module.ppt_transfer, "list_presentations", lambda: [target])
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: (args[3][0], True))
    inserted = []
    def append(path, chosen):
        inserted.append((path, chosen))
        return 11
    monkeypatch.setattr(module.ppt_transfer, "append_slide", append)
    qtbot.mouseClick(win._insert_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks)
    assert inserted == [(saves[0], target)]
    assert "第 11 页" in win._status.text()
