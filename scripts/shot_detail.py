"""截图：详情抽屉版本时间线（修复后）——选中有版本文件，详情显示版本节点（不再「未纳入」）。

offscreen 构造主窗 + StubVerMgr（3 版），点详情 + 选中结果，grab 整窗。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["PPTX_FINDER_DATA_DIR"] = tempfile.mkdtemp(prefix="shotdetail_")
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
        return [
            {"version_id": "v3", "ts": 1718900000, "page_count": 24},
            {"version_id": "v2", "ts": 1718800000, "page_count": 22},
            {"version_id": "v1", "ts": 1718700000, "page_count": 18},
        ]

    def restore_to(self, *a, **k):
        return True

    def export(self, *a, **k):
        return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(theme.build_qss("raycast"))
    win = MainWindow(conn=_index(Path(tempfile.mkdtemp())), render_worker=StubRender(),
                     version_mgr=StubVer(), do_index=False)
    win.resize(1180, 680)
    win._toggle_detail()
    win.search_box.setText("昇腾")
    win._do_search()
    if win.result_list.count():
        win.result_list.setCurrentRow(0)
    win.show()
    loop = QEventLoop()
    QTimer.singleShot(600, loop.quit)
    loop.exec()
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    win.grab().save(str(out / "detail_versions.png"))
    print("详情版本节点数:", len(win.detail_panel._version_nodes))
    print("saved:", out / "detail_versions.png")
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
