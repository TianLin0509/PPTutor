from __future__ import annotations

from PySide6.QtCore import QObject

from pptx_finder import db, search
from pptx_finder.text_tokenize import tokenize
from pptx_finder.ui.main_window import MainWindow

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


def test_full_phrase_beats_separator_compacted_filename_match(tmp_path):
    conn = db.connect(tmp_path / "phrase-vs-name.db")
    db.init_db(conn)
    _add_file(conn, name="AI-SP.pptx", text="unrelated", mtime=300)
    _add_file(conn, name="notes.pptx", text="exact content AI SP", mtime=100)
    conn.commit()

    rows = search.search(conn, "AI SP")

    assert [row.name for row in rows[:2]] == ["notes.pptx", "AI-SP.pptx"]
    assert rows[0].match_kind == "content_phrase"


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
