"""Render both new UI flows with isolated real material fixtures, without indexing."""
import os
from pathlib import Path
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor, QFont

from pptx_finder.materials import MaterialLibrary
from pptx_finder.ui.material_window import MaterialWindow
from pptx_finder.ui.imgtext_window import ImgTextWindow
from pptx_finder.ui.theme import apply_to_app, tok
from pptx_finder import imgtext_ocr


root = Path("artifacts/v1.6.0-ui").resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ["PPTX_FINDER_DATA_DIR"] = str(root / "appdata")
fixture = max(Path("artifacts/material-transfer-check").glob("*/result.json"), key=lambda p: p.stat().st_mtime).parent
app = QApplication([])
apply_to_app(app, "atelier")
# Fixture the component state only; real offline OCR is checked separately on the EXE.
imgtext_ocr.is_installed = lambda: True
sample = QImage(960, 540, QImage.Format_RGB32)
sample.fill(QColor("white"))
painter = QPainter(sample)
painter.setPen(QColor("#182431"))
painter.setFont(QFont("Microsoft YaHei", 30))
painter.drawText(70, 140, "让好素材随手可用")
painter.setFont(QFont("Microsoft YaHei", 18))
painter.drawText(70, 205, "PPT Doctor · 转字 / 收藏 / 分享")
painter.fillRect(70, 270, 820, 7, QColor("#C04432"))
painter.drawText(70, 355, "整页复制到剪贴板，或放进已打开的 PPT。")
painter.end()
sample.save(str(root / "source.png"))
win = MaterialWindow(tok("atelier"), library=MaterialLibrary(fixture / "library"))
win.show()
conversion = ImgTextWindow(tok("atelier"), source=str(root / "source.png"))
conversion._result_path = str(fixture / "source.pptx")
conversion._status.setText("已生成可编辑文本框。可复制整页，或选择已打开的 PPT，在末尾新增一页。")
conversion._sync_buttons()
conversion.show()


def capture():
    if win._tasks:
        QTimer.singleShot(100, capture)
        return
    assert win.list_widget.count() >= 2
    win.list_widget.item(0).setSelected(True)
    win.grab().save(str(root / "material-library.png"))
    conversion.grab().save(str(root / "image-text.png"))
    win.resize(620, 440)
    QTimer.singleShot(200, narrow)


def narrow():
    for button in (win.capture_btn, win.copy_btn, win.export_btn, win.import_btn):
        assert win.rect().contains(button.geometry()), button.text()
    win.grab().save(str(root / "material-library-narrow.png"))
    win.close()
    conversion.close()
    QTimer.singleShot(1200, app.quit)


QTimer.singleShot(500, capture)
QTimer.singleShot(20000, app.quit)
sys.exit(app.exec())
