"""连真实索引库（do_index=False，直接用已有数据）搜「算力」并截图，验证修复效果。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parent.parent

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder import db  # noqa: E402
from pptx_finder.config import db_path  # noqa: E402
from pptx_finder.ui.main_window import MainWindow  # noqa: E402


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    app = QApplication(sys.argv)
    conn = db.connect(str(db_path()))  # 真实库
    win = MainWindow(conn=conn, do_index=False)  # 不重新索引，直接用现有数据
    win._apply_theme("cloud", persist=False)  # 第一张强制云白（不污染用户主题）
    win.resize(1180, 740)
    win.show()

    win.search_box.setText("算力")
    win._do_search()
    pump(500)
    print("命中文件数:", win.result_list.count())
    if win.result_list.count() > 0:
        win.result_list.setCurrentRow(0)
    for _ in range(50):
        pump(500)
        if win._cur_pixmap is not None:
            break
    pump(700)

    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    win.grab().save(str(out / "verify_cloud.png"))
    print("云白②预览:", "是" if win._cur_pixmap is not None else "否")

    # 切 Raycast 深色（不持久化），验证双主题落地
    win._apply_theme("raycast", persist=False)
    for _ in range(40):
        pump(500)
        if win._cur_pixmap is not None:
            break
    pump(600)
    win.grab().save(str(out / "verify_raycast.png"))
    print("Raycast④ 截图完成")

    win._shutdown()
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
