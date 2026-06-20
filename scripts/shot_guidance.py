"""截图：引导交互（状态栏盾牌 + 详情红点 + 搜索框聚光灯 coachmark）真实渲染验证。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["PPTX_FINDER_DATA_DIR"] = tempfile.mkdtemp(prefix="shotguide_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_ui import StubRender, _index  # noqa: E402

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder.ui import theme  # noqa: E402
from pptx_finder.ui.main_window import MainWindow  # noqa: E402


class StubVer:
    def list_versions(self, p):
        return [{"version_id": "v3", "ts": 1718900000, "page_count": 24},
                {"version_id": "v2", "ts": 1718800000, "page_count": 22},
                {"version_id": "v1", "ts": 1718700000, "page_count": 18}]

    def list_docs(self):
        return list(range(7))

    def is_managed(self, p):
        return True

    def restore_to(self, *a, **k):
        return True

    def export(self, *a, **k):
        return True


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(theme.build_qss("cloud"))
    win = MainWindow(conn=_index(Path(tempfile.mkdtemp())), render_worker=StubRender(),
                     version_mgr=StubVer(), do_index=False)
    win.resize(1200, 720)
    win.refresh_version_shield()
    win.search_box.setText("昇腾")
    win._do_search()
    if win.result_list.count():
        win.result_list.setCurrentRow(0)
    win.show()
    _wait(300)
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    win.grab().save(str(out / "guide_main.png"))          # 盾牌 + 红点
    win._show_search_coach()
    _wait(350)
    win.grab().save(str(out / "guide_coach.png"))         # 搜索框聚光灯
    print("shield:", repr(win.version_shield.text()), "hidden:", win.version_shield.isHidden())
    print("detail_dot hidden:", win._detail_dot.isHidden())
    print("spotlight target:", win._spotlight._target.objectName() if getattr(win, "_spotlight", None) else None)
    print("saved:", out)
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
