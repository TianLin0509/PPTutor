from __future__ import annotations

from types import SimpleNamespace

import fixtures_gen as fx

from pptx_finder.ui import slim_window as sw
from pptx_finder.ui import theme


def _minimal_report(path):
    return sw.slim.SlimReport(
        path=str(path),
        original_size=123,
        package_parts=1,
        package_compressed_bytes=123,
        buckets=(),
        duplicate_media_groups=(),
        duplicate_media_reclaimable=0,
        orphan_parts=(),
        orphan_reclaimable=0,
        junk_parts=(),
        junk_reclaimable=0,
        unused_layouts=(),
        unused_masters=(),
    )


def test_slim_window_analyzes_and_generates_copy(qtbot, tmp_path, monkeypatch):
    deck = tmp_path / "source.pptx"
    out = tmp_path / "source.slim.pptx"
    fx.make_pptx(deck, [{"body": "瘦身窗口"}])
    messages = []

    monkeypatch.setattr(
        sw,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), "")),
        raising=False,
    )
    monkeypatch.setattr(
        sw,
        "QMessageBox",
        SimpleNamespace(
            information=lambda *args, **kwargs: messages.append(("info", args)),
            warning=lambda *args, **kwargs: messages.append(("warn", args)),
        ),
        raising=False,
    )

    win = sw.SlimWindow(theme.tok("cloud"), str(deck))
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)

    assert win._make_btn.isEnabled()
    assert "原始大小" in win._summary.text()

    win._make_slim_copy()

    qtbot.waitUntil(lambda: out.exists() and bool(messages), timeout=4000)
    assert messages[0][0] == "info"
    assert out.exists() and out.stat().st_size > 0
    assert deck.exists()


def test_slim_window_reports_analyze_failure(qtbot, tmp_path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a zip")

    win = sw.SlimWindow(theme.tok("cloud"), str(bad))
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: not win._scan_inflight, timeout=4000)

    assert "分析失败" in win._summary.text()
    assert win._make_btn.isEnabled() is False


def test_slim_window_rejects_source_overwrite_before_task(qtbot, tmp_path, monkeypatch):
    deck = tmp_path / "source.pptx"
    fx.make_pptx(deck, [{"body": "源文件不能被覆盖"}])
    messages = []
    calls = []

    monkeypatch.setattr(
        sw,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(deck), "")),
        raising=False,
    )
    monkeypatch.setattr(
        sw,
        "QMessageBox",
        SimpleNamespace(
            information=lambda *args, **kwargs: messages.append(("info", args)),
            warning=lambda *args, **kwargs: messages.append(("warn", args)),
        ),
        raising=False,
    )

    win = sw.SlimWindow(
        theme.tok("cloud"),
        str(deck),
        analyze_fn=lambda path: _minimal_report(path),
        slim_fn=lambda *_args: calls.append("slim"),
    )
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)

    win._make_slim_copy()

    assert calls == []
    assert messages and messages[0][0] == "warn"
    assert "不能覆盖源文件" in messages[0][1][2]
    assert win._slim_inflight is False


def test_slim_window_reports_slim_failure_detail(qtbot, tmp_path, monkeypatch):
    deck = tmp_path / "source.pptx"
    out = tmp_path / "source.slim.pptx"
    fx.make_pptx(deck, [{"body": "失败提示"}])
    messages = []

    monkeypatch.setattr(
        sw,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), "")),
        raising=False,
    )
    monkeypatch.setattr(
        sw,
        "QMessageBox",
        SimpleNamespace(
            information=lambda *args, **kwargs: messages.append(("info", args)),
            warning=lambda *args, **kwargs: messages.append(("warn", args)),
        ),
        raising=False,
    )

    def fail_slim(_source, _dest, **_kwargs):
        raise PermissionError("locked by another process")

    win = sw.SlimWindow(
        theme.tok("cloud"),
        str(deck),
        analyze_fn=lambda path: _minimal_report(path),
        slim_fn=fail_slim,
    )
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)

    win._make_slim_copy()

    qtbot.waitUntil(lambda: bool(messages), timeout=4000)
    assert messages[0][0] == "warn"
    assert "locked by another process" in messages[0][1][2]
    assert win._slim_inflight is False
    assert win._refresh_btn.isEnabled()
    assert win._make_btn.isEnabled()


def test_slim_window_late_slim_done_ignored_after_close(qtbot, tmp_path, monkeypatch):
    deck = tmp_path / "source.pptx"
    fx.make_pptx(deck, [{"body": "关闭后回调"}])
    messages = []
    callbacks = []

    class Parent:
        _closing = False

        def _after_slim_created(self, result):
            callbacks.append(result)

    monkeypatch.setattr(
        sw,
        "QMessageBox",
        SimpleNamespace(
            information=lambda *args, **kwargs: messages.append(("info", args)),
            warning=lambda *args, **kwargs: messages.append(("warn", args)),
        ),
        raising=False,
    )

    parent = Parent()
    win = sw.SlimWindow(
        theme.tok("cloud"),
        str(deck),
        parent,
        analyze_fn=lambda path: _minimal_report(path),
    )
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._report is not None, timeout=4000)
    win.close()

    win._on_slim_done(sw.slim.SlimResult(
        ok=True,
        source_path=str(deck),
        output_path=str(tmp_path / "source.slim.pptx"),
        original_size=100,
        slim_size=80,
        saved_bytes=20,
        removed_parts=(),
        deduped_media=0,
        actions=("重新打包 PPTX",),
    ))

    assert messages == []
    assert callbacks == []
