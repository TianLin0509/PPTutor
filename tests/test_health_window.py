"""库体检窗口 HealthWindow：渲染 + 一键/全部回收流程 + 病灶链接跳转（注入假 scan/recycle，不碰真文件/真回收站）。"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

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
        bloat_biggest=("big.pptx", 99999, "/big.pptx"),
        bloat_longest=("long.pptx", 180, "/long.pptx"),
        parse_failed=1, parse_failed_by_status={"encrypted": 1},
        zombie_examples=[health.AilmentExample("老古董.pptx", "/old.pptx")],
        curse_examples=[health.AilmentExample("最终版方案.pptx", "/curse.pptx")],
        parse_failed_examples=[health.AilmentExample("坏文件.pptx", "/bad.pptx")],
    )


def _report_many_dups(n: int) -> health.HealthReport:
    """n 组重复（每组 1 份冗余）：用来验证全部回收不受 _DUP_SHOW_LIMIT 展示上限影响。"""
    groups = [
        health.DuplicateGroup(
            content_hash="sha256:" + "%064x" % i,
            paths=[f"/keep{i}.pptx", f"/dup{i}.pptx"],
            keep_path=f"/keep{i}.pptx", size=100, reclaimable=100,
        )
        for i in range(n)
    ]
    return health.HealthReport(
        deck_count=n * 2, score=50, duplicate_groups=groups,
        duplicate_reclaimable=100 * n, duplicate_redundant=n,
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
    assert win._recycle_all_btn is None        # 无全部回收按钮


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


def test_health_window_notifies_parent_after_recycle(qtbot, monkeypatch):
    parent = QWidget()
    qtbot.addWidget(parent)
    callbacks: list[dict] = []
    parent._after_health_recycle = lambda result: callbacks.append(dict(result))

    def recycle(paths):
        return {
            "ok": True,
            "recycled": len(paths),
            "recycled_paths": list(paths),
            "failed": [],
            "freed_bytes": 2000,
        }

    monkeypatch.setattr(hw.QMessageBox, "question", lambda *a, **k: hw.QMessageBox.Yes)
    monkeypatch.setattr(hw.QMessageBox, "information", lambda *a, **k: None)

    scans = [_report_with_dups(), _report_clean()]
    win = hw.HealthWindow(
        {"win": "#101010"},
        lambda: scans.pop(0) if scans else _report_clean(),
        recycle,
        parent,
    )
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recycle_btn is not None, timeout=4000)

    win._recycle_selected()

    qtbot.waitUntil(lambda: bool(callbacks), timeout=4000)
    assert callbacks[0]["recycled_paths"] == ["/dup1.pptx", "/dup2.pptx"]


def test_recycle_all_collects_all_groups_beyond_show_limit(qtbot, monkeypatch):
    """全部回收：45 组（> _DUP_SHOW_LIMIT=40）也全量收集，确认文案带总个数与总可释放空间。"""
    rep = _report_many_dups(45)
    chunks: list[int] = []
    collected: list[str] = []

    def recycle(paths):
        chunks.append(len(paths))
        collected.extend(paths)
        return {"ok": True, "recycled": len(paths), "recycled_paths": list(paths),
                "failed": [], "freed_bytes": 100 * len(paths), "index_deleted": len(paths)}

    asked: list[str] = []

    def _yes(_w, _t, msg, *a, **k):
        asked.append(msg)
        return hw.QMessageBox.Yes

    monkeypatch.setattr(hw.QMessageBox, "question", _yes)
    monkeypatch.setattr(hw.QMessageBox, "information", lambda *a, **k: None)

    win = hw.HealthWindow({"win": "#101010", "acc": "#0A84FF"}, lambda: rep, recycle)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recycle_all_btn is not None, timeout=4000)
    assert len(win._dup_checks) == hw._DUP_SHOW_LIMIT  # 展示仍按上限截断

    win._recycle_all()
    qtbot.waitUntil(lambda: not win._recycle_inflight, timeout=4000)

    assert chunks == [20, 20, 5]                       # 按 _RECYCLE_CHUNK 分块串行
    assert collected == [f"/dup{i}.pptx" for i in range(45)]  # 全量组、每组只收冗余、不动 keep
    assert "45 组" in asked[0] and "45 份" in asked[0]
    assert health.human_bytes(4500) in asked[0]


def test_recycle_progress_signal_updates_bar(qtbot, monkeypatch):
    """一键回收所选也走分块 + 进度：25 份 → 两块，进度信号 (20,25)/(25,25)，完成后进度条隐藏。"""
    rep = _report_many_dups(25)
    chunks: list[int] = []

    def recycle(paths):
        chunks.append(len(paths))
        return {"ok": True, "recycled": len(paths), "recycled_paths": list(paths),
                "failed": [], "freed_bytes": 100 * len(paths)}

    monkeypatch.setattr(hw.QMessageBox, "question", lambda *a, **k: hw.QMessageBox.Yes)
    monkeypatch.setattr(hw.QMessageBox, "information", lambda *a, **k: None)

    win = hw.HealthWindow({"win": "#101010", "acc": "#0A84FF"}, lambda: rep, recycle)
    qtbot.addWidget(win)
    progress: list[tuple[int, int]] = []
    win.recycle_progress.connect(lambda d, t: progress.append((d, t)))
    qtbot.waitUntil(lambda: win._recycle_btn is not None, timeout=4000)
    bar = win._progress
    assert bar is not None and bar.isHidden()          # 未回收时进度条隐藏

    win._recycle_selected()                            # 默认全部勾选 → 25 份
    qtbot.waitUntil(lambda: not win._recycle_inflight, timeout=4000)

    assert chunks == [20, 5]
    assert progress == [(20, 25), (25, 25)]
    qtbot.waitUntil(lambda: win._progress is not bar or win._progress.isHidden(), timeout=4000)


def test_health_links_render_and_click_calls_owner(qtbot):
    """病灶/重复组渲染 healthLink 链接；点击经主窗鸭子钩子 _locate_health_item 跳转。"""
    parent = QWidget()
    qtbot.addWidget(parent)
    calls: list[tuple[str, str]] = []
    parent._locate_health_item = lambda path, name: calls.append((path, name))

    win = hw.HealthWindow({"win": "#101010", "acc": "#0A84FF"},
                          _report_with_dups, lambda paths: {}, parent)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)

    links = win.findChildren(QLabel, "healthLink")
    tips = {l.toolTip() for l in links}
    # 僵尸/诅咒/解析失败示例 + 巨无霸两条 + 重复组保留项
    assert {"/old.pptx", "/curse.pptx", "/bad.pptx", "/big.pptx", "/long.pptx", "/keep.pptx"} <= tips
    zombie = next(l for l in links if l.toolTip() == "/old.pptx")
    assert "老古董" in zombie.text()

    zombie.linkActivated.emit("loc")
    assert calls == [("/old.pptx", "老古董.pptx")]


def test_health_link_without_owner_falls_back_to_folder(qtbot, monkeypatch):
    """独立窗（拿不到主窗钩子）降级为打开所在文件夹。"""
    opened: list[str] = []
    monkeypatch.setattr(hw.actions, "open_folder", lambda path: opened.append(path) or True)

    win = hw.HealthWindow({"win": "#101010"}, _report_with_dups, lambda paths: {})
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)

    win._activate_health_link("/old.pptx", "老古董.pptx")
    assert opened == ["/old.pptx"]
