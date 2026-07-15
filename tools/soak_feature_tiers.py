"""Long-running, production-isolated UI responsiveness soak for PPT Doctor.

The harness intentionally exercises the real SQLite search/index threads and Qt
widgets, but replaces PowerPoint rendering with a tiny asynchronous stub.  It
therefore cannot open or close a user's PowerPoint session.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return float(ordered[index])


def _rss_bytes() -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.WorkingSetSize) if ok else 0
    except Exception:
        return 0


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _build_corpus(root: Path, count: int) -> None:
    tests_dir = Path(__file__).resolve().parents[1] / "tests"
    sys.path.insert(0, str(tests_dir))
    import fixtures_gen as fixtures

    root.mkdir(parents=True, exist_ok=True)
    for index in range(max(1, count)):
        fixtures.make_pptx(
            root / f"Deck-{index:04d}-AI-SP.pptx",
            [
                {"title": f"AI SP roadmap {index}", "body": "AI SP 全字短语 基础搜索"},
                {"title": "Network", "body": f"算力 网络 方案 {index % 17}"},
                {"title": "Summary", "body": f"结论 风险 计划 {index % 11}"},
            ],
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-sec", type=float, default=300.0)
    parser.add_argument("--corpus-size", type=int, default=160)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-dir")
    args = parser.parse_args()

    output = Path(args.output).resolve()
    trace_path = output.with_suffix(".trace.log")
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("", encoding="utf-8")

    def trace(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    workspace = Path(args.data_dir).resolve() if args.data_dir else Path(
        tempfile.mkdtemp(prefix="pptdoctor-soak-")
    )
    data_dir = workspace / "data"
    corpus = workspace / "corpus"
    os.environ["PPTX_FINDER_DATA_DIR"] = str(data_dir)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["PPTUTOR_BACKGROUND_POWERPOINT_RENDER"] = "0"

    data_dir.mkdir(parents=True, exist_ok=True)
    _build_corpus(corpus, args.corpus_size)
    trace(f"corpus_ready count={args.corpus_size}")

    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QPushButton

    from pptx_finder.ui.main_window import MainWindow
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.ui.stats_entry import _open_report

    app = QApplication.instance() or QApplication([])
    preview_png = workspace / "preview.png"
    image = QImage(960, 540, QImage.Format_ARGB32)
    image.fill(0xFFEDF3FA)
    image.save(str(preview_png))

    class _RenderStub(QObject):
        rendered = Signal(int, str)

        def __init__(self):
            super().__init__()
            self.pending = 0
            self.requests = 0
            self.prefetches = 0

        def request(self, req_id, _path, _page, **_kwargs):
            self.requests += 1
            self.pending += 1

            def finish():
                self.pending = max(0, self.pending - 1)
                self.rendered.emit(req_id, str(preview_png))

            QTimer.singleShot(12, finish)

        def prefetch(self, _path, _page, **_kwargs):
            self.prefetches += 1

        def clear(self):
            self.pending = 0

        def diagnostic_lines(self):
            return [
                f"soak_render_stub: requests={self.requests} "
                f"prefetches={self.prefetches} pending={self.pending}"
            ]

    class _VersionStub:
        def is_initialized(self):
            return False

        def diagnostic_lines(self):
            return ["soak_version_stub: enabled=False"]

        def audit_integrity(self, **_kwargs):
            return {"ok": True}

    render = _RenderStub()
    win = MainWindow(
        render_worker=render,
        do_index=True,
        roots=[str(corpus)],
        workers=2,
    )
    conn = win._conn
    win.show()
    app.processEvents()
    trace("window_ready")

    start_wall = time.monotonic()
    start_cpu = time.process_time()
    last_metric_wall = start_wall
    last_metric_cpu = start_cpu
    heartbeat_last = start_wall
    heartbeat_gaps: list[float] = []
    slow_ui_events: list[dict] = []
    operation_ms: list[float] = []
    errors: list[str] = []
    metrics: list[dict] = []
    operation_count = 0
    settings_dialogs: list[QDialog] = []
    operation_index = 0
    preview_probe = {"step": 0, "started": 0.0, "completed": False}
    finished = False
    activity_state = {"current": "idle", "last": "startup"}

    heartbeat_timer = QTimer()
    heartbeat_timer.setInterval(25)

    def heartbeat() -> None:
        nonlocal heartbeat_last
        now = time.monotonic()
        gap = max(0.0, (now - heartbeat_last) * 1000.0 - 25.0)
        heartbeat_gaps.append(gap)
        if gap >= 100.0 and len(slow_ui_events) < 100:
            slow_ui_events.append({
                "elapsed_sec": round(now - start_wall, 3),
                "gap_ms": round(gap, 3),
                "current_operation": activity_state["current"],
                "last_operation": activity_state["last"],
            })
        heartbeat_last = now

    heartbeat_timer.timeout.connect(heartbeat)
    heartbeat_timer.start()

    # MainWindow's real startup check launches the low-priority two-process index.
    trace("index_start_scheduled")

    queries = ["AI SP", "算力", "Deck-0001", "roadmap", "风险 计划", "不存在xyz"]
    operation_names = (
        "search_ai_sp", "search_compute", "search_filename", "search_roadmap",
        "search_risk_plan", "search_missing", "primary_sort", "secondary_sort",
        "case_toggle", "show_recent", "select_result", "page_next", "page_previous",
        "toggle_facet", "toggle_detail", "refresh_status", "open_settings", "open_report",
    )

    def close_settings_later(dialog: QDialog) -> None:
        if dialog in settings_dialogs:
            settings_dialogs.remove(dialog)
        dialog.close()
        dialog.deleteLater()

    def action() -> None:
        nonlocal operation_count, operation_index
        elapsed = time.monotonic() - start_wall
        # Two minutes of realistic bursts followed by three minutes of idle.
        if int(elapsed // 60) % 5 >= 2:
            return
        started = time.perf_counter()
        try:
            op = operation_index % 18
            operation_index += 1
            activity_state["current"] = operation_names[op]
            if op in (0, 1, 2, 3, 4, 5):
                win.search_box.setText(queries[op])
                win._do_search()
            elif op == 6:
                win.sort_combo.setCurrentIndex(
                    (win.sort_combo.currentIndex() + 1) % win.sort_combo.count()
                )
            elif op == 7:
                win.sort_secondary.setCurrentIndex(
                    (win.sort_secondary.currentIndex() + 1) % win.sort_secondary.count()
                )
            elif op == 8:
                win.case_sensitive_btn.click()
            elif op == 9:
                win.search_box.clear()
                win._do_search()
            elif op == 10 and win.result_list.count():
                win.result_list.setCurrentRow(win._first_selectable_row())
            elif op == 11:
                win._wheel_page(-120)
            elif op == 12:
                win._wheel_page(120)
            elif op == 13:
                win._toggle_facet()
            elif op == 14:
                win._toggle_detail()
            elif op == 15:
                win._refresh_status()
            elif op == 16 and not settings_dialogs:
                dialog = SettingsDialog(_VersionStub(), win)
                settings_dialogs.append(dialog)
                dialog.show()
                QTimer.singleShot(350, lambda dialog=dialog: close_settings_later(dialog))
            elif op == 17 and getattr(win, "_stats_overlay", None) is None:
                _open_report(win)
                QTimer.singleShot(
                    1200,
                    lambda: getattr(win, "_stats_overlay", None).close()
                    if getattr(win, "_stats_overlay", None) is not None else None,
                )
            win._note_user_activity()
            operation_count += 1
        except Exception:
            errors.append(traceback.format_exc(limit=6))
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            operation_ms.append(elapsed_ms)
            activity_state["last"] = activity_state["current"]
            activity_state["current"] = "idle"

    action_timer = QTimer()
    action_timer.setInterval(140)
    action_timer.timeout.connect(action)
    action_timer.start()

    def exercise_preview_after_index() -> None:
        """Exercise the real result-to-preview UI after the initial scan settles.

        The regular workload deliberately keeps telling the indexer that the user
        is active, so a brand-new database may not contain selectable results
        during the first burst.  This small state machine waits for the low-
        priority scan to finish, then validates result selection and page turns.
        PowerPoint itself remains replaced by the safe asynchronous render stub.
        """
        if preview_probe["completed"] or time.monotonic() - start_wall < 5.0:
            return
        if win._indexer is not None and win._indexer.isRunning():
            return
        now = time.monotonic()
        step = int(preview_probe["step"])
        if step == 0:
            win.search_box.setText("AI SP")
            win._do_search()
            preview_probe.update(step=1, started=now)
            return
        if step == 1:
            if now - float(preview_probe["started"]) < 0.5:
                return
            row = win._first_selectable_row()
            if row >= 0:
                win.result_list.setCurrentRow(row)
                preview_probe.update(step=2, started=now)
            elif now - float(preview_probe["started"]) > 5.0:
                errors.append("preview probe found no selectable search result")
                preview_probe["completed"] = True
            return
        if now - float(preview_probe["started"]) < 0.25:
            return
        if step in (2, 3, 4, 5):
            win._wheel_page(-120 if step % 2 == 0 else 120)
            preview_probe.update(step=step + 1, started=now)
            return
        preview_probe["completed"] = True

    preview_timer = QTimer()
    preview_timer.setInterval(100)
    preview_timer.timeout.connect(exercise_preview_after_index)
    preview_timer.start()

    def capture_metrics() -> None:
        nonlocal last_metric_wall, last_metric_cpu
        now_wall = time.monotonic()
        now_cpu = time.process_time()
        wall_delta = max(0.001, now_wall - last_metric_wall)
        trace("capture_search_begin")
        try:
            search_metrics = win._search_worker.diagnostics()
        except Exception:
            trace("capture_search_error " + traceback.format_exc(limit=8).replace("\n", " | "))
            raise
        trace("capture_search_done")
        rss = _rss_bytes()
        trace("capture_rss_done")
        metrics.append(
            {
                "elapsed_sec": round(now_wall - start_wall, 3),
                "process_cpu_percent": round((now_cpu - last_metric_cpu) / wall_delta * 100.0, 3),
                "rss_bytes": rss,
                "operations": operation_count,
                "heartbeat_p95_ms": round(_percentile(heartbeat_gaps[-2400:], 0.95), 3),
                "heartbeat_max_ms": round(max(heartbeat_gaps[-2400:] or [0.0]), 3),
                "ui_monitor_max_gap_ms": round(float(win._ui_loop_max_gap_ms), 3),
                "search": search_metrics,
                "index_running": bool(win._indexer and win._indexer.isRunning()),
                "render_requests": render.requests,
                "render_prefetches": render.prefetches,
                "errors": len(errors),
            }
        )
        last_metric_wall = now_wall
        last_metric_cpu = now_cpu
        trace("capture_write_begin")
        _write_json(output.with_suffix(".partial.json"), {"metrics": metrics, "errors": errors})
        trace("capture_write_done")

    metrics_timer = QTimer()
    metrics_timer.setInterval(60_000)
    metrics_timer.timeout.connect(capture_metrics)
    metrics_timer.start()

    def finish() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        trace("finish_begin")
        action_timer.stop()
        preview_timer.stop()
        heartbeat_timer.stop()
        metrics_timer.stop()
        trace("final_metrics_begin")
        capture_metrics()
        trace("final_metrics_done")
        for dialog in list(settings_dialogs):
            close_settings_later(dialog)
        trace("settings_closed")
        overlay = getattr(win, "_stats_overlay", None)
        if overlay is not None:
            overlay.close()
        trace("overlay_closed")
        trace("shutdown_begin")
        win._shutdown()
        trace("shutdown_done")
        elapsed = time.monotonic() - start_wall
        cpu = time.process_time() - start_cpu
        summary = {
            "started_at_epoch": time.time() - elapsed,
            "duration_sec": round(elapsed, 3),
            "corpus_size": args.corpus_size,
            "operations": operation_count,
            "operation_p50_ms": round(_percentile(operation_ms, 0.50), 3),
            "operation_p95_ms": round(_percentile(operation_ms, 0.95), 3),
            "operation_p99_ms": round(_percentile(operation_ms, 0.99), 3),
            "operation_max_ms": round(max(operation_ms or [0.0]), 3),
            "heartbeat_samples": len(heartbeat_gaps),
            "heartbeat_p50_ms": round(_percentile(heartbeat_gaps, 0.50), 3),
            "heartbeat_p95_ms": round(_percentile(heartbeat_gaps, 0.95), 3),
            "heartbeat_p99_ms": round(_percentile(heartbeat_gaps, 0.99), 3),
            "heartbeat_max_ms": round(max(heartbeat_gaps or [0.0]), 3),
            "slow_ui_events": slow_ui_events,
            "process_cpu_average_percent": round(cpu / max(0.001, elapsed) * 100.0, 3),
            "rss_bytes_final": _rss_bytes(),
            "render_requests": render.requests,
            "render_prefetches": render.prefetches,
            "preview_probe_completed": bool(preview_probe["completed"]),
            "search": win._search_worker.diagnostics(),
            "ui_monitor": {
                "samples": win._ui_loop_samples,
                "max_gap_ms": round(float(win._ui_loop_max_gap_ms), 3),
                "slow_gaps": win._ui_loop_slow_gaps,
            },
            "errors": errors,
            "metrics": metrics,
            "workspace": str(workspace),
        }
        _write_json(output, summary)
        trace("summary_written")
        try:
            output.with_suffix(".partial.json").unlink(missing_ok=True)
        except OSError:
            pass
        conn.close()
        trace("connection_closed")
        app.quit()
        trace("app_quit_requested")

    QTimer.singleShot(max(1, int(args.duration_sec * 1000)), finish)
    trace("event_loop_begin")
    exit_code = app.exec()
    trace(f"event_loop_done code={exit_code}")
    if not finished:
        finish()
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
