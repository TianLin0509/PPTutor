"""真实启动主窗（offscreen），索引 demo_decks，真实搜索 + COM 渲染，截图保存。

这是一次真实端到端冒烟：真解析、真索引、真 PowerPoint 渲染、真 UI 组装。
产出 artifacts/smoke_main.png 供肉眼验收。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PPTX_FINDER_ROOTS"] = str(ROOT / "demo_decks")
os.environ["PPTX_FINDER_DATA_DIR"] = tempfile.mkdtemp(prefix="pptxfinder_demo_")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder.ui.main_window import MainWindow  # noqa: E402


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow(do_index=True, workers=2)
    win.resize(1180, 740)
    win.show()

    # 等索引完成（最多 ~30s）
    for _ in range(60):
        pump(500)
        if win._indexer is not None and win._indexer.isFinished():
            break
    pump(500)
    print("索引状态:", win.status_label.text())

    # 真实搜索 + 选中 + 等待真实渲染
    win.search_box.setText("算力 集群")
    win._do_search()
    pump(400)
    print("命中文件数:", win.result_list.count())
    if win.result_list.count() > 0:
        win.result_list.setCurrentRow(0)
    for _ in range(60):  # 最多 ~30s 等 COM 渲染
        pump(500)
        if win._cur_pixmap is not None:
            break
    pump(800)

    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    shot = out / "smoke_main.png"
    win.grab().save(str(shot))

    print("预览已渲染:", "是" if win._cur_pixmap is not None else "否")
    if win.result_list.count() > 0:
        r0 = win._results[0]
        print("首条结果:", r0.name, "| 命中页:", [h.page_no for h in r0.hits])
    print("截图:", shot)

    win._shutdown()
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
