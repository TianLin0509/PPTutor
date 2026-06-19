"""真机演示：版本管理 UI + 真实 watcher。隔离环境，造演示数据，起 GUI 供查看/交互。

演示目录在桌面 pptx-version-demo/，你可以用 PowerPoint 打开里面的 .pptx 改几个字保存，
版本窗口会自动冒出新版本（watcher 实时 + 定时刷新）。
不碰你的生产数据，不撞单实例。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DEMO = Path(os.environ["USERPROFILE"]) / "Desktop" / "pptx-version-demo"
os.environ["PPTX_FINDER_DATA_DIR"] = str(DEMO / "_appdata")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import fixtures_gen as fx  # noqa: E402

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder.ui import theme  # noqa: E402
from pptx_finder.ui.version_window import VersionWindow  # noqa: E402
from pptx_finder.versioning.manager import VersionManager  # noqa: E402


def main() -> int:
    work = DEMO / "我的方案"
    work.mkdir(parents=True, exist_ok=True)
    p = work / "算力方案.pptx"

    mgr = VersionManager()

    # 造一段演示历史（3 版），首次运行才造
    if not mgr.list_versions(str(p)):
        fx.make_pptx(p, [{"body": "封面 算力方案"}, {"body": "第二页 量子计算 章节（这段以后会删）"}])
        mgr.snapshot_now(str(p))
        time.sleep(0.05)
        fx.make_pptx(p, [{"body": "封面 算力方案 v2"}, {"body": "第二页 改成 经典计算"}])
        mgr.snapshot_now(str(p))
        time.sleep(0.05)
        fx.make_pptx(p, [{"body": "终稿封面"}, {"body": "终稿正文"}, {"body": "新增第三页"}])
        mgr.snapshot_now(str(p))

    mgr.start()  # 真实 watcher：改存 demo pptx 会自动留版本

    app = QApplication(sys.argv)
    app.setStyleSheet(theme.build_qss("cloud"))
    w = VersionWindow(mgr)
    w.setWindowTitle("版本管理演示 · 用 PowerPoint 改存 桌面/pptx-version-demo/我的方案/算力方案.pptx 试试")
    w.resize(960, 600)
    w.show()
    w.raise_()
    w.activateWindow()

    # 定时刷新：你改存 demo pptx 后，能看到新版本自动冒出来
    def refresh() -> None:
        it = w.doc_list.currentItem()
        if it is not None:
            data = it.data(Qt.UserRole)
            if isinstance(data, tuple):
                w._fill_versions(data[0])

    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(2500)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
