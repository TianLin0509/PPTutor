"""Run a realistic UI stress flow without touching the user's real index.

The script creates a temporary deck set, indexes it through MainWindow, then
drives rapid search/sort/select/clear/facet/preview/close operations while
tracking the Qt event-loop max gap.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QObject, QEventLoop, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import fixtures_gen as fx  # noqa: E402
from pptx_finder.ui.main_window import MainWindow  # noqa: E402


class AsyncRender(QObject):
    rendered = Signal(int, str)

    def __init__(self, png: Path):
        super().__init__()
        self.png = str(png)
        self.requests: list[tuple[int, str, int, int, int]] = []
        self.prefetches: list[tuple[str, int, int, int]] = []
        self.stopped = False

    def request(
        self,
        req_id: int,
        path: str,
        page_no: int,
        cache_key: str | None = None,
        long_edge: int = 0,
        priority: int = 0,
    ) -> None:
        self.requests.append((req_id, path, page_no, int(long_edge), int(priority)))
        QTimer.singleShot(5, lambda req_id=req_id: self.rendered.emit(req_id, self.png))

    def prefetch(
        self,
        path: str,
        page_no: int,
        cache_key: str | None = None,
        long_edge: int = 0,
        priority: int = 0,
    ) -> None:
        self.prefetches.append((path, page_no, int(long_edge), int(priority)))

    def prewarm(self) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True

    def wait(self, _ms: int) -> bool:
        return True


class AsyncThumb(QObject):
    thumb_rendered = Signal(str, int, str)

    def __init__(self, png: Path):
        super().__init__()
        self.png = str(png)
        self.requests: list[tuple[str, int, int]] = []
        self.clears = 0
        self.stopped = False

    def request(self, path: str, page: int, priority: int = 0) -> None:
        self.requests.append((path, page, int(priority)))
        QTimer.singleShot(5, lambda path=path, page=page: self.thumb_rendered.emit(path, page, self.png))

    def clear(self) -> None:
        self.clears += 1

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True

    def wait(self, _ms: int) -> bool:
        return True


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def gap_stats(gaps: list[float]) -> tuple[float, float]:
    if not gaps:
        return 0.0, 0.0
    ordered = sorted(gaps)
    return round(max(gaps), 2), round(ordered[int(len(ordered) * 0.95)], 2)


def make_decks(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    topics = [
        "算力 集群 AI 预算 路线图",
        "客户 汇报 增长 版本 复盘",
        "芯片 训练 推理 平台 成本",
        "年度 规划 项目 风险 里程碑",
    ]
    for i in range(36):
        body = topics[i % len(topics)]
        fx.make_pptx(
            root / f"stress_{i:02d}_算力方案.pptx",
            [
                {"body": f"{body} 第{i}号 首页"},
                {"body": f"快速搜索 压力测试 详情 预览 第{i}号"},
                {"body": f"版本 管理 导出 恢复 缩略图 第{i}号"},
            ],
        )


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="pptutor_stress_data_"))
    decks = Path(tempfile.mkdtemp(prefix="pptutor_stress_decks_"))
    os.environ["PPTX_FINDER_DATA_DIR"] = str(data_dir)
    os.environ["PPTX_FINDER_ROOTS"] = str(decks)
    make_decks(decks)

    app = QApplication.instance() or QApplication(sys.argv)
    preview_png = data_dir / "preview.png"
    thumb_png = data_dir / "thumb.png"
    pm = QPixmap(320, 180)
    pm.fill()
    pm.save(str(preview_png))
    tm = QPixmap(96, 72)
    tm.fill()
    tm.save(str(thumb_png))

    gaps: list[float] = []
    last = {"t": time.perf_counter()}
    timer = QTimer()
    timer.setInterval(20)

    def tick() -> None:
        now = time.perf_counter()
        gaps.append((now - last["t"]) * 1000)
        last["t"] = now

    timer.timeout.connect(tick)
    timer.start()

    render = AsyncRender(preview_png)
    thumb = AsyncThumb(thumb_png)
    win = MainWindow(render_worker=render, thumb_worker=thumb, do_index=True, workers=2)
    win.resize(1180, 740)
    win.show()

    deadline = time.time() + 25
    while time.time() < deadline:
        pump(100)
        if win._indexer is not None and win._indexer.isFinished():
            break
    pump(300)
    if win._indexer is None or not win._indexer.isFinished():
        raise RuntimeError("indexing did not finish within stress deadline")
    startup_max_gap, startup_p95_gap = gap_stats(gaps)
    gaps.clear()
    last["t"] = time.perf_counter()

    queries = [
        "算力", "AI", "预算", "客户", "版本", "芯片", "不存在的词",
        "项目 风险", "快速搜索", "训练 推理", "",
    ] * 4
    for q in queries:
        win.search_box.setText(q)
        win._do_search()
        pump(35)
    pump(1000)

    win.search_box.setText("算力")
    win._do_search()
    pump(800)
    if win.result_list.count() <= 0:
        raise RuntimeError("expected stress query to return results")

    for idx in range(3):
        win.sort_combo.setCurrentIndex(idx)
        pump(120)

    win._toggle_facet()
    pump(120)
    win._toggle_facet()
    pump(120)

    rows = min(24, win.result_list.count())
    for i in range(rows):
        win.result_list.setCurrentRow(i)
        pump(25)
    pump(600)

    win.search_box.clear()
    win._do_search()
    pump(300)
    win.search_box.setText("版本")
    win._do_search()
    pump(700)

    screenshot = ROOT / "artifacts" / "stress_user_flow.png"
    screenshot.parent.mkdir(exist_ok=True)
    win.grab().save(str(screenshot))
    interaction_max_gap, interaction_p95_gap = gap_stats(gaps)

    result = {
        "ok": True,
        "indexed_files": len(list(decks.glob("*.pptx"))),
        "final_results": win.result_list.count(),
        "render_requests": len(render.requests),
        "render_prefetches": len(render.prefetches),
        "thumb_requests": len(thumb.requests),
        "thumb_clears": thumb.clears,
        "startup_event_loop_max_gap_ms": startup_max_gap,
        "startup_event_loop_p95_gap_ms": startup_p95_gap,
        "interaction_event_loop_max_gap_ms": interaction_max_gap,
        "interaction_event_loop_p95_gap_ms": interaction_p95_gap,
        "screenshot": str(screenshot),
        "data_dir": str(data_dir),
        "deck_dir": str(decks),
    }

    timer.stop()
    win._shutdown()
    app.quit()

    report = ROOT / "artifacts" / "stress_user_flow.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
