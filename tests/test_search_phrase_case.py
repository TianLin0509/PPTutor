from __future__ import annotations

from PySide6.QtCore import QObject

from pptx_finder import db, search
from pptx_finder.text_tokenize import tokenize
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.result_utils import sort_results

from test_ui import StubRender


def _add_file(conn, *, name: str, text: str, mtime: float) -> None:
    fid = db.upsert_file(
        conn,
        path=f"C:/phrase-case/{name}",
        name=name,
        ext=".pptx",
        size=100,
        mtime=mtime,
        content_hash=f"hash-{name}",
        page_count=1,
        status="ok",
        error="",
        indexed_at=mtime,
    )
    db.replace_pages(conn, fid, [(1, text, tokenize(text))])


def test_unquoted_multiword_query_prioritizes_the_full_phrase(tmp_path):
    conn = db.connect(tmp_path / "phrase.db")
    db.init_db(conn)
    _add_file(conn, name="punctuation.pptx", text="newer AI / SP plan", mtime=300)
    _add_file(conn, name="split.pptx", text="AI platform roadmap then SP delivery", mtime=200)
    _add_file(conn, name="phrase.pptx", text="older but exact AI SP plan", mtime=100)
    conn.commit()

    rows = search.search(conn, "AI SP")

    assert [row.name for row in rows] == [
        "phrase.pptx",
        "punctuation.pptx",
        "split.pptx",
    ]
    assert rows[0].match_kind == "content_phrase"
    assert rows[1].match_kind == "content_exact"
    assert rows[2].match_kind == "partial"


def test_full_phrase_does_not_match_an_ascii_word_prefix(tmp_path):
    conn = db.connect(tmp_path / "phrase-boundary.db")
    db.init_db(conn)
    _add_file(conn, name="prefix.pptx", text="AI SPARK roadmap with a separate SP marker", mtime=300)
    _add_file(conn, name="phrase.pptx", text="older but exact AI SP plan", mtime=100)
    conn.commit()

    rows = search.search(conn, "AI SP")
    by_name = {row.name: row for row in rows}

    assert rows[0].name == "phrase.pptx"
    assert by_name["phrase.pptx"].match_kind == "content_phrase"
    assert by_name["prefix.pptx"].match_kind == "partial"


def test_filename_full_query_match_beats_content_full_phrase(tmp_path):
    conn = db.connect(tmp_path / "phrase-vs-name.db")
    db.init_db(conn)
    _add_file(conn, name="AI-SP.pptx", text="unrelated", mtime=300)
    _add_file(conn, name="notes.pptx", text="exact content AI SP", mtime=100)
    conn.commit()

    rows = search.search(conn, "AI SP")

    assert [row.name for row in rows[:2]] == ["AI-SP.pptx", "notes.pptx"]
    assert rows[0].match_kind == "filename_exact"
    assert rows[1].match_kind == "content_phrase"


def test_case_sensitive_search_filters_content_and_filename_but_default_does_not(tmp_path):
    conn = db.connect(tmp_path / "case.db")
    db.init_db(conn)
    _add_file(conn, name="AI SP filename.pptx", text="unrelated", mtime=400)
    _add_file(conn, name="ai sp filename.pptx", text="unrelated", mtime=300)
    _add_file(conn, name="upper-content.pptx", text="contains AI SP", mtime=200)
    _add_file(conn, name="lower-content.pptx", text="contains ai sp", mtime=100)
    conn.commit()

    default_names = {row.name for row in search.search(conn, "AI SP")}
    sensitive_names = {
        row.name for row in search.search(conn, "AI SP", case_sensitive=True)
    }

    assert default_names == {
        "AI SP filename.pptx",
        "ai sp filename.pptx",
        "upper-content.pptx",
        "lower-content.pptx",
    }
    assert sensitive_names == {"AI SP filename.pptx", "upper-content.pptx"}


def test_case_sensitive_exact_filename_keeps_the_exact_ranking_tier(tmp_path):
    conn = db.connect(tmp_path / "case-exact-name.db")
    db.init_db(conn)
    _add_file(conn, name="AIReport.pptx", text="unrelated", mtime=100)
    conn.commit()

    rows = search.search(conn, "AIReport", case_sensitive=True)

    assert len(rows) == 1
    assert rows[0].name == "AIReport.pptx"
    assert rows[0].match_kind == "filename_exact"


def test_default_relevance_prefers_recent_same_case_filename_token_over_old_content(tmp_path):
    """用户搜 FINAL 时，近期文件名全词命中必须压过旧正文命中。"""
    conn = db.connect(tmp_path / "filename-before-content.db")
    db.init_db(conn)
    _add_file(
        conn,
        name="梦想的一天-FINAL.pptx",
        text="梦想记录",
        mtime=1_725_000_000,  # 约一个月内
    )
    _add_file(
        conn,
        name="一年前的项目总结.pptx",
        text="final final final delivery notes",
        mtime=1_695_000_000,
    )
    conn.commit()

    rows = search.search(conn, "FINAL")

    assert [row.name for row in rows[:2]] == [
        "梦想的一天-FINAL.pptx",
        "一年前的项目总结.pptx",
    ]
    assert rows[0].name_hit is True
    assert rows[0].match_kind == "filename_phrase"
    assert rows[0].case_exact is True
    assert rows[1].case_exact is False
    # UI 的“相关度”二次排序也必须保留同一规则，不能把搜索层的正确顺序打乱。
    assert sort_results(list(reversed(rows)), "relevance")[0].name == "梦想的一天-FINAL.pptx"


def test_default_relevance_uses_case_as_a_ranking_signal_without_filtering(tmp_path):
    """默认仍召回大小写不同的结果，但与查询大小写一致者排在前面。"""
    conn = db.connect(tmp_path / "case-rank.db")
    db.init_db(conn)
    _add_file(conn, name="Alpha-FINAL.pptx", text="unrelated", mtime=100)
    _add_file(conn, name="Beta-final.pptx", text="unrelated", mtime=300)
    conn.commit()

    rows = search.search(conn, "FINAL")

    assert [row.name for row in rows] == ["Alpha-FINAL.pptx", "Beta-final.pptx"]
    assert [row.case_exact for row in rows] == [True, False]


def test_same_case_filename_phrase_beats_case_folded_exact_filename(tmp_path):
    """来源相同时，大小写一致优先于仅 casefold 后的完整文件名。"""
    conn = db.connect(tmp_path / "case-before-quality.db")
    db.init_db(conn)
    _add_file(conn, name="final.pptx", text="unrelated", mtime=400)
    _add_file(conn, name="梦想的一天-FINAL.pptx", text="unrelated", mtime=100)
    conn.commit()

    rows = search.search(conn, "FINAL")

    assert [row.name for row in rows] == ["梦想的一天-FINAL.pptx", "final.pptx"]
    assert [row.case_exact for row in rows] == [True, False]
    assert rows[1].match_kind == "filename_exact"


def test_default_relevance_uses_recency_within_same_source_quality_and_case(tmp_path):
    conn = db.connect(tmp_path / "recent-rank.db")
    db.init_db(conn)
    _add_file(conn, name="Old-FINAL.pptx", text="unrelated", mtime=100)
    _add_file(conn, name="New-FINAL.pptx", text="unrelated", mtime=300)
    conn.commit()

    rows = search.search(conn, "FINAL")

    assert [row.name for row in rows] == ["New-FINAL.pptx", "Old-FINAL.pptx"]


class _CaptureSearchWorker(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[int, str, str, tuple[str, ...] | None, bool]] = []

    def request(
        self,
        req_id: int,
        query: str,
        mode_key: str,
        exts: tuple[str, ...] | None = None,
        case_sensitive: bool = False,
    ) -> None:
        self.requests.append((req_id, query, mode_key, exts, case_sensitive))

    def cancel(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wait(self, _ms: int) -> bool:
        return True


def test_case_sensitive_button_defaults_off_and_reruns_current_query(qtbot, tmp_path):
    conn = db.connect(tmp_path / "case-ui.db")
    db.init_db(conn)
    worker = _CaptureSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = worker

    assert not win.case_sensitive_btn.isChecked()
    assert "大小写" in win.case_sensitive_btn.text()

    win.search_box.setText("AI SP")
    win._do_search()
    assert worker.requests[-1][-1] is False

    win.case_sensitive_btn.setChecked(True)
    assert worker.requests[-1][1] == "AI SP"
    assert worker.requests[-1][-1] is True


def test_case_sensitive_mode_suppresses_unverifiable_history_hint(qtbot, tmp_path):
    class _HistoryManager:
        def search_history_details(self, _query: str, limit: int = 200):
            raise AssertionError("case-sensitive main search must not run case-folded history FTS")

    conn = db.connect(tmp_path / "case-history.db")
    db.init_db(conn)
    win = MainWindow(
        conn=conn,
        render_worker=StubRender(),
        version_mgr=_HistoryManager(),
        do_index=False,
    )
    qtbot.addWidget(win)
    blocked = win.case_sensitive_btn.blockSignals(True)
    win.case_sensitive_btn.setChecked(True)
    win.case_sensitive_btn.blockSignals(blocked)

    win._kick_history_search("AI SP")

    assert win._history_hint_pending_query == ""
    assert not win._history_hint_timer.isActive()
