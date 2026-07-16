"""静白工作室（方向 A）验收截图：临时库 + demo_decks，不碰真实数据、不调起 PowerPoint。

产出（repo/artifacts/）：
- atelier_main.png       静白 · 搜索命中 + 选中第一条（预览为真实失败空态文案）
- atelier_dash.png       静白 · 零搜索仪表盘首屏
- atelier_dark_main.png  静黑 · 同主界面
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parent.parent

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder import db, indexer  # noqa: E402
from pptx_finder.ui.main_window import MainWindow  # noqa: E402


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, *a, **k):
        pass


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    demo = ROOT / "demo_decks"
    if not demo.exists():
        print("先跑 scripts/make_demo.py 生成 demo_decks/")
        return 1
    app = QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp(prefix="pptdoctor_shot_"))
    conn = db.connect(tmp / "shot.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(demo)], workers=1)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)

    def shot(theme: str, name: str, *, search: str | None) -> None:
        win._apply_theme(theme, persist=False)
        win.resize(1180, 760)
        win.show()
        if search is None:
            win.search_box.clear()
            win._do_search()
            pump(600)
        else:
            win.search_box.setText(search)
            win._do_search()
            pump(600)
            if win.result_list.count() > 0:
                win.result_list.setCurrentRow(0)
            pump(400)
            win._stop_spinner()
            win._show_preview_unavailable()  # 真实空态（StubRender 下不起 COM）
            win.result_list.setFocus()       # 焦点离开搜索框，还原静息态
            pump(300)
        win.grab().save(str(out / name))
        print(f"{name}: results={win.result_list.count()}")

    shot("atelier", "atelier_dash.png", search=None)
    shot("atelier", "atelier_main.png", search="算力")
    shot("atelier_dark", "atelier_dark_main.png", search="算力")

    win._shutdown()
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
