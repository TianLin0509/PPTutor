"""库体检窗口 HealthWindow：渲染 + 一键回收流程（注入假 scan/recycle，不碰真文件/真回收站）。"""
from __future__ import annotations

from pptx_finder import health
from pptx_finder.ui import health_window as hw


def _report_with_dups() -> health.HealthReport:
    g = health.DuplicateGroup(
        content_hash="sha256:" + "a" * 64,
        paths=["/keep.pptx", "/dup1.pptx", "/dup2.pptx"],
        keep_path="/keep.pptx", size=1000, reclaimable=2000,
    )
    return health.HealthReport(
        deck_count=5, score=72, duplicate_groups=[g],
        duplicate_reclaimable=2000, duplicate_redundant=2,
        zombie_count=1, zombie_bytes=500, curse_count=1,
        bloat_biggest=("big.pptx", 99999), bloat_longest=("long.pptx", 180),
        parse_failed=1, parse_failed_by_status={"encrypted": 1},
    )


def _report_clean() -> health.HealthReport:
    return health.HealthReport(deck_count=5, score=100)


def test_health_window_renders(qtbot):
    rep = _report_with_dups()
    win = hw.HealthWindow(
        {"win": "#101010", "grn": "#34c759"},
        lambda: rep,
        lambda paths: {"ok": True, "recycled": len(paths), "failed": [], "freed_bytes": 0},
    )
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)
    assert win._report.score == 72
    assert win._score_lab.text() == "72"
    assert len(win._dup_checks) == 1          # 一组重复
    assert win._recycle_btn is not None        # 有回收按钮


def test_health_window_clean_has_no_recycle_btn(qtbot):
    win = hw.HealthWindow({"win": "#101010"}, _report_clean,
                          lambda paths: {"ok": True, "recycled": 0, "failed": [], "freed_bytes": 0})
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)
    assert win._report.score == 100
    assert win._dup_checks == []               # 无重复 → 无勾选项
    assert win._recycle_btn is None            # 无回收按钮


def test_health_window_recycle_recycles_redundant(qtbot, monkeypatch):
    captured: dict = {}

    def recycle(paths):
        captured["paths"] = list(paths)
        return {"ok": True, "recycled": len(paths), "failed": [], "freed_bytes": 2000}

    # 自动确认 + 吞掉结果弹窗
    monkeypatch.setattr(hw.QMessageBox, "question", lambda *a, **k: hw.QMessageBox.Yes)
    monkeypatch.setattr(hw.QMessageBox, "information", lambda *a, **k: None)

    scans = [_report_with_dups(), _report_clean()]   # 回收后第二次扫描返回干净

    def scan():
        return scans.pop(0) if scans else _report_clean()

    win = hw.HealthWindow({"win": "#101010"}, scan, recycle)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recycle_btn is not None, timeout=4000)
    win._recycle_selected()
    qtbot.waitUntil(lambda: "paths" in captured, timeout=4000)
    # 只回收冗余副本（保留 keep），不动 keep_path
    assert captured["paths"] == ["/dup1.pptx", "/dup2.pptx"]
