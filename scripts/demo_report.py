"""临时视觉验证：用丰富假数据渲染「胶片报告」浮层，离屏导出 PNG（cloud + raycast）。

仅供开发期肉眼看效果，不进测试套件。
运行：uv run python scripts/demo_report.py
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime

# 用真实 Windows 平台渲染（offscreen 平台缺中文字体会渲染成方块）；
# 不 show 不弹窗，仅 grab 离屏成图。
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder import db, stats  # noqa: E402
from pptx_finder.ui import report_overlay as ro  # noqa: E402
from pptx_finder.ui import theme  # noqa: E402

OUT_DIR = r"C:\Users\lintian\Desktop\claude-artifacts"


def _ts(y, mo, d, h):
    return datetime(y, mo, d, h).timestamp()


def _seed(conn):
    """构造一批多样假数据：一个 5 版的大组 + 终版命名 + 巨无霸 + 深夜/周末分布。"""
    rows = [
        # name, (y,mo,d,h), size, pages, group, chars
        ("AI战略汇报_v1.pptx", (2026, 5, 30, 22), 4_500_000, 80, 1, 3800),
        ("AI战略汇报_v2.pptx", (2026, 6, 1, 23), 4_800_000, 85, 1, 4000),
        ("AI战略汇报_v3定稿.pptx", (2026, 6, 3, 1), 4_900_000, 86, 1, 4100),
        ("AI战略汇报_最终版.pptx", (2026, 6, 2, 2), 5_200_000, 88, 1, 4200),
        ("AI战略汇报_真的final.pptx", (2026, 6, 4, 3), 5_000_000, 87, 1, 4150),
        ("周报0606.pptx", (2026, 6, 6, 15), 800_000, 12, None, 900),
        ("周报0607.pptx", (2026, 6, 7, 16), 820_000, 13, None, 950),
        ("部门预算修订.pptx", (2026, 6, 5, 10), 1_200_000, 24, None, 1500),
        ("产品路线图.pptx", (2026, 6, 1, 9), 2_100_000, 45, None, 2200),
        ("客户方案_改.pptx", (2026, 5, 28, 21), 1_800_000, 33, None, 1900),
        ("老古董2019.pptx", (2019, 3, 3, 14), 600_000, 18, None, 800),
    ]
    # 再撒一批让热力图丰富（偏深夜 + 周末）
    random.seed(42)
    for i in range(18):
        wd = random.choice([0, 1, 2, 3, 4, 5, 5, 6, 6])  # 周末加权
        hr = random.choice([22, 23, 0, 1, 2, 9, 10, 14, 21, 23])  # 深夜加权
        rows.append((f"杂项{i}.pptx", (2026, 6, 1 + wd, hr),
                     random.randint(200_000, 1_500_000),
                     random.randint(5, 40), None, random.randint(300, 2500)))

    for name, (y, mo, d, h), size, pages, gid, chars in rows:
        fid = db.upsert_file(conn, path="/decks/" + name, name=name, ext=".pptx",
                             size=size, mtime=_ts(y, mo, d, h), content_hash="h",
                             page_count=pages, status="ok", error="", indexed_at=0)
        db.replace_pages(conn, fid, [(1, "字" * chars, "t")])
        if gid is not None:
            conn.execute(
                "INSERT INTO minhash(file_id, sig, page_hashes, group_id) VALUES(?,?,?,?)",
                (fid, b"", "[]", gid))
    conn.commit()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = db.connect(":memory:")
    db.init_db(conn)
    _seed(conn)
    report = stats.build_report(conn, year=None)
    print(f"deck_count={report.deck_count} persona={report.persona.title} "
          f"badges={report.persona.badges}")
    for name in ("cloud", "raycast"):
        app.setStyleSheet(theme.build_qss(name))  # 模拟真实 app 环境（含中文字体）
        ov = ro.ReportOverlay(report, theme.tok(name))
        out = os.path.join(OUT_DIR, f"胶片报告-{name}.png")
        ok = ov.export_png(out)
        print(f"{name}: {ok} -> {out}")
    app.quit()


if __name__ == "__main__":
    main()
