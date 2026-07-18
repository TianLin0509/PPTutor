"""胶片报告 UI 改版（v0.9.3）：吐槽文案 / 人格矩阵 / 火焰色 / 滚动数字 纯逻辑 + 构造 smoke。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from PySide6.QtWidgets import QFrame

from pptx_finder import db, stats
from pptx_finder.ui import heatmap as hm
from pptx_finder.ui import report_overlay as ro
from pptx_finder.ui import theme


# ---------- #1 吐槽文案 ----------
def test_roast_night_thresholds():
    assert "修仙" in ro.roast_night(0.5)
    assert ro.roast_night(0.0) != ro.roast_night(0.5)
    assert all(ro.roast_night(r) for r in (0.0, 0.1, 0.3, 0.5))   # 各档都有文案


def test_roast_curse_thresholds():
    assert "谎" in ro.roast_curse(0.4)
    assert ro.roast_curse(0.0)                                    # 低档也有文案


def test_roast_zombie_only_for_old():
    assert "天" in ro.roast_zombie(2000)
    assert ro.roast_zombie(10) == ""                             # 不老不吐槽


def test_roast_weekend_thresholds():
    assert "加班" in ro.roast_weekend(600)
    assert ro.roast_weekend(0)                                   # 低档也有（正向）文案


# ---------- #3 人格矩阵 ----------
def _n(ratio=0.0, w=0.0):
    return stats.NightOwlStat(0, ratio, 0, w, None, None)


def _d():
    return stats.VersionDramaStat(None, 0, 0, 0.0, None, 0.0)


def _s(chars=1000, decks=5):
    return stats.ScaleStat("a", 10, "a", 100, chars, 100, decks)


def test_persona_matrix_night_high_output():
    p = stats.persona(_n(ratio=0.5), _d(), _s(chars=1000, decks=80))
    assert p.rhythm == "夜猫子"
    assert p.output == "高产型"
    assert p.role == "夜间作战参谋"


def test_persona_matrix_normal_has_role():
    p = stats.persona(_n(), _d(), _s(chars=1000, decks=5))
    assert p.rhythm == "正常作息"
    assert p.output == "精修型"
    assert p.role                                                # 查表命中或默认，非空


def test_persona_matrix_hoarder():
    p = stats.persona(_n(), _d(), _s(chars=1000, decks=300))
    assert p.output == "囤积型"


# ---------- #4 火焰色 + 峰值 ----------
def test_fire_color_transparent_when_empty():
    assert hm.fire_color(0, 10).alpha() == 0


def test_fire_color_warm_when_hot():
    c = hm.fire_color(10, 10)
    assert c.alpha() > 0 and c.red() > 150                       # 高热偏红/暖


def test_peak_label_format():
    assert hm.peak_label(2, 23) == "周三 23 点"
    assert hm.peak_label(0, 9) == "周一 9 点"


def test_heatmap_widget_exposes_peak_position(qtbot):
    matrix = [[0] * 24 for _ in range(7)]
    matrix[2][23] = 9
    w = hm.HeatmapWidget(matrix, accent=(255, 140, 66), empty="#241f33", ink="#999")
    qtbot.addWidget(w)
    assert (w.peak_wd, w.peak_hour) == (2, 23)


# ---------- #2 滚动数字 ----------
def test_rollnumber_formats(qtbot):
    a = ro.RollNumber(3521, fmt="comma"); qtbot.addWidget(a)
    a.finish(); assert a.text() == "3,521"
    b = ro.RollNumber(11907180, fmt="wan"); qtbot.addWidget(b)
    b.finish(); assert b.text() == "1191万"
    c = ro.RollNumber(5.4, fmt="gb", suffix=" GB"); qtbot.addWidget(c)
    c.finish(); assert c.text() == "5.4 GB"


def test_rollnumber_starts_at_zero(qtbot):
    a = ro.RollNumber(3521, fmt="comma")
    qtbot.addWidget(a)
    assert a.text() == "0"                                       # 未 start 前从 0 起


# ---------- 改版浮层构造 smoke ----------
def test_overlay_follows_theme_and_has_copy(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    mt = datetime(2026, 6, 1, 2).timestamp()
    fid = db.upsert_file(conn, path="/终版.pptx", name="终版.pptx", ext=".pptx", size=2_000_000,
                         mtime=mt, content_hash="h", page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "算力" * 100, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)
    light = theme.tok("atelier")
    ov = ro.ReportOverlay(report, light)            # 浮层配色跟随传入主题
    qtbot.addWidget(ov)
    assert ov._tok is not ro.FILM                   # 不再强制固定 FILM 深色
    assert ov._tok["card0"] == light["panel"]       # 卡片底 ← 主题 panel 派生
    assert ov._tok["card1"] == light["panel2"]
    assert ov._tok["roast"] == light["acc"]         # 吐槽色 ← 主题强调色派生
    assert light["panel"] in ov._card.styleSheet()  # 卡片 stylesheet 实含主题色
    assert ro.FILM["card0"] not in ov._card.styleSheet()
    assert hasattr(ov, "copy_btn")                  # 复制按钮存在
    assert len(ov._rolls) == 2                      # 英雄区两个滚动数字（份数 / 字数）
    out = tmp_path / "r.png"
    assert ov.export_png(str(out)) is True          # 仍能导出 PNG（导出=所见即所得）
    assert out.exists() and out.stat().st_size > 0


def test_overlay_stylesheet_differs_between_light_and_dark_theme(qtbot, tmp_path):
    """静白 / 静黑分别构造：关键 stylesheet 色值不同且各自匹配对应主题 token。"""
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    mt = datetime(2026, 6, 1, 2).timestamp()
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000, mtime=mt,
                   content_hash="h", page_count=5, status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    light, dark = theme.tok("atelier"), theme.tok("atelier_dark")
    ov_light = ro.ReportOverlay(report, light)
    qtbot.addWidget(ov_light)
    ov_dark = ro.ReportOverlay(report, dark)
    qtbot.addWidget(ov_dark)
    # 卡片渐变底 ← 主题 panel 派生，两主题互不串色
    assert light["panel"] in ov_light._card.styleSheet()
    assert dark["panel"] in ov_dark._card.styleSheet()
    assert ov_light._card.styleSheet() != ov_dark._card.styleSheet()
    # 卡片边框 ← 主题 bd
    assert light["bd"] in ov_light._card.styleSheet()
    assert dark["bd"] in ov_dark._card.styleSheet()
    # 区块卡背景 ← 主题 canvas
    assert light["canvas"] in ov_light.findChild(QFrame, "activityCard").styleSheet()
    assert dark["canvas"] in ov_dark.findChild(QFrame, "activityCard").styleSheet()
    # 遮罩压暗与主题无关，保持一致
    assert ov_light.styleSheet() == ov_dark.styleSheet()


def test_copy_blocked_during_year_switch(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    mt = datetime(2026, 6, 1, 2).timestamp()
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000, mtime=mt,
                   content_hash="h", page_count=5, status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)
    msgs = []
    monkeypatch.setattr(ro, "QMessageBox",
                        SimpleNamespace(information=lambda *a, **k: msgs.append(a)), raising=False)
    ov._switch_inflight = (1, 2026)   # 模拟年度切换重算中
    ov._copy_clicked()
    assert msgs == []                 # 守卫生效：没抓图、没弹「已复制」框
