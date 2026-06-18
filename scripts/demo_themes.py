"""临时视觉验证：3 种新风格主界面截图（真实「昇腾」搜索场景，离屏渲染）。

运行：uv run python scripts/demo_themes.py
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from pptx_finder import db
from pptx_finder.text_tokenize import tokenize
from pptx_finder.ui.main_window import MainWindow

OUT = r"C:\Users\lintian\Desktop\claude-artifacts"


class StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, req_id, path, page_no, cache_key=None):
        self.rendered.emit(req_id, "")  # 空预览（无 COM）


def _seed(conn):
    files = [
        ("AI战略汇报_昇腾最终版.pptx",
         ["封面页", "第二页 昇腾 910B 集群部署方案 算力规模领先", "中间页", "图表页", "昇腾 总结"]),
        ("算力中心_技术选型.pptx", ["对比 昇腾 与 GPU 能效比 国产化路线"]),
        ("Q2业务复盘.pptx", ["基于 昇腾 的推理服务上线 时延下降 38%"]),
        ("年度技术规划v3.pptx", ["昇腾 算力底座 + 大模型微调链路"]),
    ]
    for name, pages in files:
        fid = db.upsert_file(conn, path="/decks/" + name, name=name, ext=".pptx",
                             size=2_000_000, mtime=1_717_000_000.0, content_hash="h",
                             page_count=len(pages), status="ok", error="", indexed_at=0)
        db.replace_pages(conn, fid, [(i + 1, p, tokenize(p)) for i, p in enumerate(pages)])
    conn.commit()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = db.connect(":memory:")
    db.init_db(conn)
    _seed(conn)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(1180, 760)
    win.show()
    app.processEvents()
    win.search_box.setText("昇腾")
    win._do_search()
    app.processEvents()

    for name in ("cinema", "morandi", "aurora"):
        win._apply_theme(name, persist=False)  # persist=False：不污染全局 ui.json
        app.processEvents()
        out = os.path.join(OUT, f"主界面-{name}.png")
        win.grab().save(out)
        print(f"{name} -> {out}")
    app.quit()


if __name__ == "__main__":
    main()
