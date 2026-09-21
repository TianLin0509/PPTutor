from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog

from pptx_finder.materials import MaterialLibrary, Material
from pptx_finder.ui.material_window import MaterialWindow


def test_capture_search_copy_and_export_flow(qtbot, tmp_path, monkeypatch):
    library = MaterialLibrary(tmp_path / "materials")
    copied, exported = [], []
    def capture(name, category):
        item = Material("a" * 32, name, category, 1)
        folder = library.folder(item.id)
        folder.mkdir()
        library._write(folder, item)
        image = QImage(80, 40, QImage.Format_RGB32)
        image.fill(Qt.red)
        image.save(str(folder / "preview.png"))
        return item
    monkeypatch.setattr(library, "capture", capture)
    monkeypatch.setattr(library, "copy", copied.append)
    monkeypatch.setattr(library, "export_pack", lambda ids, path: exported.append((ids, path)))
    win = MaterialWindow(library=library)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: not win._tasks)
    win.name_edit.setText("红色箭头")
    win.category_edit.setText("箭头")
    qtbot.mouseClick(win.capture_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks and win.list_widget.count() == 1)
    win.search.setText("找不到")
    assert win.list_widget.count() == 0
    win.search.setText("红色")
    assert win.list_widget.count() == 1
    win.list_widget.item(0).setSelected(True)
    qtbot.mouseClick(win.copy_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks)
    assert copied == ["a" * 32]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path / "分享.pptx"), ""))
    qtbot.mouseClick(win.export_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks)
    assert exported == [(["a" * 32], tmp_path / "分享.pptx")]


def test_failure_is_visible_and_buttons_recover(qtbot, tmp_path, monkeypatch):
    library = MaterialLibrary(tmp_path)
    def fail(*args):
        raise RuntimeError("剪贴板正在被占用")
    monkeypatch.setattr(library, "capture", fail)
    win = MaterialWindow(library=library)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: not win._tasks)
    qtbot.mouseClick(win.capture_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: not win._tasks)
    assert "剪贴板正在被占用" in win.status.text()
    assert win.capture_btn.isEnabled()
