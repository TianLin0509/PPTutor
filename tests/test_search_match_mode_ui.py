"""界面上的「模糊匹配 / 精确匹配」下拉：切换即重搜、零结果可一键退回模糊、选择会被记住。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
import pytest

import fixtures_gen as fx

from pptx_finder import db, indexer
import pptx_finder.ui.main_window as main_window_mod
from pptx_finder.ui.main_window import MainWindow


class _Stub(QObject):
    rendered = Signal(int, str)

    def request(self, req_id: int, path: str, page_no: int, cache_key=None):
        self.rendered.emit(req_id, "")


@pytest.fixture
def saved(monkeypatch):
    """不落盘：测试共用一个数据目录，真写 ui.json 会把模式带进后面的测试。"""
    calls: list[bool] = []
    monkeypatch.setattr(main_window_mod, "set_search_exact_match", calls.append)
    monkeypatch.setattr(main_window_mod, "get_search_exact_match", lambda: False)
    return calls


def _win(qtbot, tmp_path):
    docs = tmp_path / "d"
    docs.mkdir()
    fx.make_pptx(docs / "品牌手册.pptx", [{"body": "brand transfer training"}])
    fx.make_pptx(docs / "接入网.pptx", [{"body": "RAN 架构 演进"}])
    fx.make_pptx(docs / "算力方案.pptx", [{"body": "昇腾 集群 算力 部署"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    win = MainWindow(conn=conn, render_worker=_Stub(), do_index=False)
    qtbot.addWidget(win)
    return win


def _result_names(win) -> list[str]:
    return sorted(win._results[i].name for i in range(len(win._results)))


def test_default_is_fuzzy_and_switching_to_exact_reruns_search(qtbot, tmp_path, saved):
    win = _win(qtbot, tmp_path)
    assert win.match_mode_combo.currentText() == "模糊匹配"
    win.search_box.setText("RAN")
    win._do_search()
    assert _result_names(win) == ["品牌手册.pptx", "接入网.pptx"]

    win.match_mode_combo.setCurrentText("精确匹配")

    assert _result_names(win) == ["接入网.pptx"]
    assert saved == [True]
    assert "精确匹配" in win.query_hint.text()


def test_exact_mode_zero_results_offers_one_click_back_to_fuzzy(qtbot, tmp_path, saved):
    win = _win(qtbot, tmp_path)
    win.match_mode_combo.setCurrentText("精确匹配")
    win.search_box.setText("算力方按")   # 错字：精确下没有结果
    win._do_search()
    btn = win._sugg_btns["fuzzy"]
    assert not btn.isHidden()

    btn.click()

    assert win.match_mode_combo.currentText() == "模糊匹配"
    assert _result_names(win) == ["算力方案.pptx"]
    assert saved == [True, False]


def test_fuzzy_mode_zero_results_does_not_offer_switch(qtbot, tmp_path, saved):
    win = _win(qtbot, tmp_path)
    win.search_box.setText("完全不存在的词")
    win._do_search()
    assert win._sugg_btns["fuzzy"].isHidden()


def test_exact_mode_suppresses_unverifiable_history_hint(qtbot, tmp_path, saved):
    """历史版本补搜没有原文可做整词核验，精确模式下会冒出 brand 之类的假命中，宁可不提示。"""
    class _HistoryManager:
        def search_history_details(self, _query: str, limit: int = 200):
            raise AssertionError("exact mode must not run substring-only history FTS")

    conn = db.connect(tmp_path / "h.db")
    db.init_db(conn)
    win = MainWindow(conn=conn, render_worker=_Stub(), version_mgr=_HistoryManager(), do_index=False)
    qtbot.addWidget(win)
    win.match_mode_combo.setCurrentText("精确匹配")

    win._kick_history_search("RAN")

    assert win._history_hint_pending_query == ""
    assert not win._history_hint_timer.isActive()


def test_remembered_exact_mode_is_restored_on_startup(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(main_window_mod, "get_search_exact_match", lambda: True)
    monkeypatch.setattr(main_window_mod, "set_search_exact_match", lambda _v: None)
    win = _win(qtbot, tmp_path)
    assert win.match_mode_combo.currentText() == "精确匹配"
    win.search_box.setText("RAN")
    win._do_search()
    assert _result_names(win) == ["接入网.pptx"]
