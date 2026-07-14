"""报告浮层单测：文案格式化纯逻辑 + 构造/导出 PNG smoke。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFrame, QWidget

from pptx_finder import db, stats
from pptx_finder.ui import report_overlay as ro
from pptx_finder.ui import theme


def _ts(y, mo, d, h):
    return datetime(y, mo, d, h).timestamp()


def _add_export_sentinel(ov: ro.ReportOverlay) -> None:
    sentinel = QFrame()
    sentinel.setObjectName("fullExportSentinel")
    sentinel.setAttribute(Qt.WA_StyledBackground, True)
    sentinel.setFixedHeight(96)
    sentinel.setStyleSheet("#fullExportSentinel{background:#00ff00;border:none;border-radius:0;}")
    ov._content_lay.addWidget(sentinel)
    ov._content.adjustSize()


def _assert_green_sentinel_visible(image: QImage) -> None:
    assert not image.isNull()
    green_hits = 0
    y0 = max(0, image.height() - 180)
    step_x = max(1, image.width() // 28)
    for y in range(y0, image.height()):
        for x in range(12, max(13, image.width() - 12), step_x):
            color = image.pixelColor(x, y)
            if color.green() > 200 and color.red() < 80 and color.blue() < 80:
                green_hits += 1
                if green_hits >= 8:
                    return
    assert green_hits >= 8


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
    assert ov.findChild(QFrame, "activityCard") is not None
    assert ov.findChild(QFrame, "libraryDnaCard") is not None
    assert ov.close_btn.text() == "×"
    assert ov.close_btn.width() >= 32
    assert ov.close_btn.height() >= 32
    out = tmp_path / "report.png"
    assert ov.export_png(str(out)) is True
    assert out.exists() and out.stat().st_size > 0


def test_export_png_captures_full_scroll_content(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/full-export.pptx", name="full-export.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "full export path " * 240, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)
    parent = QWidget()
    parent.resize(900, 560)
    qtbot.addWidget(parent)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent)
    qtbot.addWidget(ov)
    _add_export_sentinel(ov)

    visible_card_height = int(ov._card.height() * ov._card.devicePixelRatioF())
    out = tmp_path / "full-report.png"

    assert ov.export_png(str(out)) is True
    image = QImage(str(out))

    assert image.height() > visible_card_height
    _assert_green_sentinel_visible(image)


def test_copy_clicked_captures_full_scroll_content(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/full-copy.pptx", name="full-copy.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "full copy path " * 240, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)
    parent = QWidget()
    parent.resize(900, 560)
    qtbot.addWidget(parent)
    messages = []
    monkeypatch.setattr(
        ro,
        "QMessageBox",
        SimpleNamespace(information=lambda *args, **kwargs: messages.append(args)),
        raising=False,
    )

    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent)
    qtbot.addWidget(ov)
    _add_export_sentinel(ov)

    visible_card_height = int(ov._card.height() * ov._card.devicePixelRatioF())

    ov._copy_clicked()
    image = ro.QApplication.clipboard().pixmap().toImage()

    assert messages
    assert image.height() > visible_card_height
    _assert_green_sentinel_visible(image)


def test_overlay_uses_larger_responsive_card(qtbot, tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/large-report.pptx", name="large-report.pptx", ext=".pptx",
                   size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                   page_count=88, status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    parent = QWidget()
    parent.resize(1500, 1400)
    qtbot.addWidget(parent)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent, conn=conn)
    qtbot.addWidget(ov)

    assert ov._card.width() == 1140
    assert ov._card.height() == 1120
    assert ov.close_btn.parentWidget() is ov._card
    assert ov._scroll.widget() is ov._content
    assert ov.close_btn not in ov._scroll.findChildren(QWidget)
    assert ov._month_btn.text() == "本月"
    assert ov._week_btn.text() == "本周"


def test_export_button_saves_png(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/clicked.pptx", name="clicked.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "export button path" * 100, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)

    ov = ro.ReportOverlay(report, theme.tok("cloud"))
    qtbot.addWidget(ov)
    out = tmp_path / "clicked-report.png"
    messages = []
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), "")),
        raising=False,
    )
    monkeypatch.setattr(
        ro,
        "QMessageBox",
        SimpleNamespace(information=lambda *args, **kwargs: messages.append(args)),
        raising=False,
    )

    qtbot.mouseClick(ov.export_btn, Qt.LeftButton)

    qtbot.waitUntil(lambda: out.exists() and out.stat().st_size > 0 and bool(messages), timeout=1000)


def test_export_button_saves_png_in_background(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/async-export.pptx", name="async-export.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "async export button path" * 100, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []
    messages = []
    out = tmp_path / "async-report.png"

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), "")),
        raising=False,
    )
    monkeypatch.setattr(
        ro,
        "QMessageBox",
        SimpleNamespace(information=lambda *args, **kwargs: messages.append(args)),
        raising=False,
    )

    ov = ro.ReportOverlay(report, theme.tok("cloud"))
    qtbot.addWidget(ov)
    export_calls = []
    monkeypatch.setattr(ov, "export_png", lambda _path: export_calls.append(_path) or True)

    qtbot.mouseClick(ov.export_btn, Qt.LeftButton)

    assert export_calls == []
    assert tasks and tasks[-1].label == "stats-report-export"
    assert ov.export_btn.isEnabled() is False
    assert messages == []

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert out.exists() and out.stat().st_size > 0
    assert messages
    assert ov.export_btn.isEnabled() is True


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
    qtbot.waitUntil(lambda: ov.current_report.deck_count == 1, timeout=1000)
    assert ov.current_year == 2026
    assert ov.current_report.deck_count == 1     # 仅 2026 那份
    ov.switch_year(None)
    # “全部”是浮层初始报告，切回时直接复用，不再重复扫库。
    assert ov._switch_inflight is None
    assert ov.current_report is report
    assert ov.current_year is None
    assert ov.current_report.deck_count == 2


def test_overlay_year_switch_builds_in_background(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    calls = []
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    def fake_build_report(_conn, *, year=None):
        calls.append(year)
        return report

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(ro.stats, "build_report", fake_build_report)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)
    calls.clear()

    ov.switch_year(2026)

    assert calls == []
    assert tasks and tasks[-1].label == "stats-report-switch"
    assert "生成" in ov._content_lay.itemAt(0).widget().text()

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert calls == [2026]
    assert ov.current_year == 2026


def test_overlay_month_and_week_filters_build_with_time_windows(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 10, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    calls = []
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    def fake_build_report(_conn, *, year=None, since_ts=None, until_ts=None):
        calls.append((year, since_ts, until_ts))
        return report

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(ro.stats, "build_report", fake_build_report)
    monkeypatch.setattr(ro, "_month_bounds", lambda: (10.0, 20.0), raising=False)
    monkeypatch.setattr(ro, "_week_bounds", lambda: (30.0, 40.0), raising=False)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov.switch_scope(ro._SCOPE_MONTH)
    assert tasks[-1].label == "stats-report-switch"
    assert ov._month_btn.isChecked()
    tasks[-1].done.emit(tasks[-1].fn())
    assert calls[-1] == (None, 10.0, 20.0)
    assert ov.current_scope == ro._SCOPE_MONTH

    ov.switch_scope(ro._SCOPE_WEEK)
    assert tasks[-1].label == "stats-report-switch"
    assert ov._week_btn.isChecked()
    tasks[-1].done.emit(tasks[-1].fn())
    assert calls[-1] == (None, 30.0, 40.0)
    assert ov.current_scope == ro._SCOPE_WEEK


def test_overlay_tasks_registered_for_parent_shutdown(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self._label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    parent = QWidget()
    parent._bg_tasks = []
    qtbot.addWidget(parent)
    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent, conn=conn)

    ov.switch_year(2026)
    switch_task = tasks[-1]

    assert switch_task in ov._report_tasks
    assert switch_task in parent._bg_tasks

    switch_task.finished.emit()

    assert switch_task not in ov._report_tasks
    assert switch_task not in parent._bg_tasks


def test_overlay_export_task_registered_for_parent_shutdown(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/export-parent.pptx", name="export-parent.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "export parent path" * 100, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []
    out = tmp_path / "export-parent.png"

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self._label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    parent = QWidget()
    parent._bg_tasks = []
    qtbot.addWidget(parent)
    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), "")),
        raising=False,
    )

    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent)

    ov._export_clicked()
    export_task = tasks[-1]

    assert export_task in ov._report_tasks
    assert export_task in parent._bg_tasks

    export_task.finished.emit()

    assert export_task not in ov._report_tasks
    assert export_task not in parent._bg_tasks


def test_overlay_year_switch_reuses_inflight_same_year(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov.switch_year(2026)
    first_task = tasks[-1]
    ov.switch_year(2026)
    ov.switch_year(2026)

    switch_tasks = [task for task in tasks if task.label == "stats-report-switch"]
    assert switch_tasks == [first_task]


def test_overlay_year_switch_ignores_new_switch_while_inflight(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)

    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov.switch_year(2026)
    first_task = tasks[-1]
    ov.switch_year(None)

    switch_tasks = [task for task in tasks if task.label == "stats-report-switch"]
    assert switch_tasks == [first_task]
    assert ov._switch_inflight == (1, ro._SCOPE_YEAR, 2026)
    assert ov.current_year == 2026


def test_overlay_year_switch_disables_export_until_report_ready(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []
    dialogs = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: dialogs.append(args) or ("C:/loading.png", "")),
        raising=False,
    )
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov.switch_year(2026)

    assert ov.export_btn.isEnabled() is False
    ov._export_clicked()
    assert dialogs == []

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)

    assert ov.export_btn.isEnabled() is True


def test_overlay_late_export_done_does_not_reenable_during_year_switch(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []
    messages = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: ("C:/report.png", "")),
        raising=False,
    )
    monkeypatch.setattr(
        ro,
        "QMessageBox",
        SimpleNamespace(information=lambda *args, **kwargs: messages.append(args)),
        raising=False,
    )
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov._export_clicked()
    export_task = tasks[-1]
    assert export_task.label == "stats-report-export"
    ov.switch_year(2026)
    switch_task = tasks[-1]
    assert switch_task.label == "stats-report-switch"
    assert ov.export_btn.isEnabled() is False

    export_task.done.emit(True)

    assert ov.export_btn.isEnabled() is False

    result = switch_task.fn()
    switch_task.done.emit(result)

    assert ov.export_btn.isEnabled() is True


def test_overlay_year_switch_done_does_not_reenable_active_export(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []
    messages = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: ("C:/report.png", "")),
        raising=False,
    )
    monkeypatch.setattr(
        ro,
        "QMessageBox",
        SimpleNamespace(information=lambda *args, **kwargs: messages.append(args)),
        raising=False,
    )
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov._export_clicked()
    export_task = tasks[-1]
    assert export_task.label == "stats-report-export"

    ov.switch_year(2026)
    switch_task = tasks[-1]
    assert switch_task.label == "stats-report-switch"
    result = switch_task.fn()
    switch_task.done.emit(result)

    assert ov.export_btn.isEnabled() is False

    export_task.done.emit(True)

    assert ov.export_btn.isEnabled() is True


def test_overlay_year_switch_failure_shows_retryable_error(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov.switch_year(2026)
    tasks[-1].done.emit(None)

    text = ov._content_lay.itemAt(0).widget().text()
    assert "生成失败" in text
    assert "重试" in text
    assert ov._all_btn.isEnabled()
    assert ov._year_btn.isEnabled()
    assert ov.export_btn.isEnabled() is False


def test_overlay_late_year_switch_ignored_after_closing(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    year_report = stats.build_report(conn, year=2026)
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    ov = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(ov)

    ov.switch_year(2026)
    ov._closing = True
    tasks[-1].done.emit(year_report)

    assert ov.current_report is report


def test_overlay_late_year_switch_ignored_after_owner_closing(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/a.pptx", name="a.pptx", ext=".pptx", size=1000,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=5,
                   status="ok", error="", indexed_at=0)
    conn.commit()
    report = stats.build_report(conn, year=None)
    year_report = stats.build_report(conn, year=2026)
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    parent = QWidget()
    parent._closing = False
    qtbot.addWidget(parent)
    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent, conn=conn)

    ov.switch_year(2026)
    parent._closing = True
    tasks[-1].done.emit(year_report)

    assert ov.current_report is report


def test_overlay_late_export_done_ignored_after_owner_closing(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/owner-export.pptx", name="owner-export.pptx", ext=".pptx",
                         size=2_000_000, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=88, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "owner export path" * 100, "t")])
    conn.commit()
    report = stats.build_report(conn, year=None)
    tasks = []
    messages = []
    out = tmp_path / "owner-export.png"

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    parent = QWidget()
    parent._closing = False
    qtbot.addWidget(parent)
    monkeypatch.setattr(ro, "BackgroundTask", FakeTask, raising=False)
    monkeypatch.setattr(
        ro,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), "")),
        raising=False,
    )
    monkeypatch.setattr(
        ro,
        "QMessageBox",
        SimpleNamespace(information=lambda *args, **kwargs: messages.append(args)),
        raising=False,
    )
    ov = ro.ReportOverlay(report, theme.tok("cloud"), parent=parent)

    ov._export_clicked()
    parent._closing = True
    tasks[-1].done.emit(True)

    assert messages == []
    assert ov.export_btn.isEnabled() is False
