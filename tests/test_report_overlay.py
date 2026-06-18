"""报告浮层单测：文案格式化纯逻辑 + 构造/导出 PNG smoke。"""
from __future__ import annotations

from datetime import datetime

from pptx_finder import db, stats
from pptx_finder.ui import report_overlay as ro
from pptx_finder.ui import theme


def _ts(y, mo, d, h):
    return datetime(y, mo, d, h).timestamp()


def test_human_bytes_gb():
    assert ro.human_bytes(1_500_000_000) == "1.4 GB"


def test_human_bytes_kb():
    assert ro.human_bytes(3500) == "3.4 KB"


def test_redmansion_mentions_book():
    assert "红楼梦" in ro.redmansion_equiv(730_000)


def test_hour_label_predawn():
    assert ro.hour_label(3) == "凌晨3点"


def test_hour_label_late_night():
    assert ro.hour_label(23) == "深夜23点"


def test_overlay_constructs_and_exports_png(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/述职终版.pptx", name="述职终版.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "赋能闭环抓手" * 100, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)

    ov = ro.ReportOverlay(report, theme.tok("cloud"))
    qtbot.addWidget(ov)
    out = tmp_path / "report.png"
    assert ov.export_png(str(out)) is True
    assert out.exists() and out.stat().st_size > 0


def test_overlay_year_switch_rebuilds_report(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    db.upsert_file(conn, path="/b.pptx", name="b.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2020, 1, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)
    assert ov.current_report.deck_count == 2     # 全部历史
    ov.switch_year(2026)
    assert ov.current_year == 2026
    assert ov.current_report.deck_count == 1     # 仅 2026 那份
    ov.switch_year(None)
    assert ov.current_year is None
    assert ov.current_report.deck_count == 2
