"""方向 09：结果按时间分组（仅最近修改 / 空查询默认视图下生效）。"""
from __future__ import annotations

import datetime
import time

from test_recent import _index_with_mtimes
from test_ui import StubRender

from pptx_finder.models import FileResult
from pptx_finder.ui.main_window import MainWindow, _time_bucket, group_by_time

NOW = datetime.datetime(2026, 6, 19, 12, 0).timestamp()


def _ts(days_ago: int, hour: int = 10) -> float:
    dt = datetime.datetime(2026, 6, 19, hour) - datetime.timedelta(days=days_ago)
    return dt.timestamp()


def _fr(name: str, mtime: float) -> FileResult:
    return FileResult(file_id=1, path=f"C:/{name}", name=name, ext=".pptx",
                      mtime=mtime, size=1, page_count=1, status="ok", score=0, name_hit=False)


def test_bucket_today():
    assert _time_bucket(_ts(0), NOW) == "今天"


def test_bucket_yesterday():
    assert _time_bucket(_ts(1), NOW) == "昨天"


def test_bucket_week():
    assert _time_bucket(_ts(4), NOW) == "本周"


def test_bucket_month():
    assert _time_bucket(_ts(20), NOW) == "本月"


def test_bucket_older():
    assert _time_bucket(_ts(90), NOW) == "更早"


def test_group_preserves_order_and_buckets():
    rs = [_fr("a", _ts(0)), _fr("b", _ts(0)), _fr("c", _ts(90))]
    groups = group_by_time(rs, NOW)
    assert [g[0] for g in groups] == ["今天", "更早"]
    assert [r.name for r in groups[0][1]] == ["a", "b"]


def test_recent_view_inserts_headers(qtbot, tmp_path):
    now = time.time()
    conn = _index_with_mtimes(tmp_path, [
        ("recent.pptx", now), ("ancient.pptx", now - 90 * 86400)])
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("")
    win._do_search()
    qtbot.waitUntil(lambda: win.result_list.count() > 2, timeout=2000)
    assert win.result_list.count() > 2        # 2 文件 + 分组头
    win.sort_combo.setCurrentText("文件名")
    assert win.result_list.count() == 2       # 文件名排序 → 不分组
