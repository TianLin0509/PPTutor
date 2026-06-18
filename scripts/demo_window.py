"""临时视觉验证：真实 MainWindow 上的入口位置 + 报告浮层盖在主界面的效果。

离屏渲染（WA_DontShowOnScreen），不弹窗。运行：uv run python scripts/demo_window.py
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from pptx_finder import db
from pptx_finder.ui import stats_entry
from pptx_finder.ui.main_window import MainWindow

from demo_report import _seed  # 复用丰富假数据

OUT = r"C:\Users\lintian\Desktop\claude-artifacts"


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = db.connect(":memory:")
    db.init_db(conn)
    _seed(conn)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    win.setAttribute(Qt.WA_DontShowOnScreen, True)  # 离屏，不弹窗
    win.resize(1180, 760)
    win.show()
    app.processEvents()
    win.grab().save(os.path.join(OUT, "主窗-入口.png"))
    print("入口截图 done")

    stats_entry._open_report(win)
    win._stats_overlay.setGeometry(win.rect())
    app.processEvents()
    win.grab().save(os.path.join(OUT, "主窗-报告.png"))
    print("报告截图 done")
    app.quit()


if __name__ == "__main__":
    main()
