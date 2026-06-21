from __future__ import annotations

from test_ui import _index

from pptx_finder.ui.search_worker import SearchWorker


def test_search_worker_returns_latest_query_results(qtbot, tmp_path):
    conn = _index(tmp_path)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(7, "昇腾", "all")

        req_id, query, results, elapsed_ms, error = blocker.args
        assert req_id == 7
        assert query == "昇腾"
        assert error is None
        assert elapsed_ms >= 0
        assert len(results) == 1
        assert results[0].hits
    finally:
        worker.stop()
        worker.wait(3000)


def test_search_worker_filters_modes(qtbot, tmp_path):
    conn = _index(tmp_path)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(8, "昇腾", "filename")

        _, _, results, _, error = blocker.args
        assert error is None
        assert results == []
    finally:
        worker.stop()
        worker.wait(3000)
