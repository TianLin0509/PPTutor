# -*- coding: utf-8 -*-
"""联想（别名 / 拼写纠错）必须有硬预算，而且严格路径一分钱都不该多付。

背景：v1.5.1 候选版把联想接进了 `search()`，但触发门槛是「严格结果 ≤40 条」，
而打分是纯 Python 的编辑距离滑窗、没有预算也不查取消。真机实测（1,649 份 PPT
的小库）：

    zzqqxx-nonexistent      0.1 ms  →  51,464 ms
    network slice latency   1.0 ms  →  45,291 ms
    汇报.pptx               0.2 ms  →     817 ms

profile 显示 `_osa_distance` 被调 458,054 次、`min()` 一亿六千万次。而整套
1,564 个用例全绿——因为没有任何一条断言延迟。所以这里钉的是**工作量**而不是
墙钟：墙钟随机器快慢飘，工作量不飘。
"""
from __future__ import annotations

import time

import pytest

from pptx_finder import search, search_relax


def test_budget_stops_after_its_operation_allowance():
    budget = search_relax.RelaxBudget(ops=5, seconds=60)
    assert [budget.spend() for _ in range(4)] == [True] * 4
    assert budget.spend() is False
    assert budget.exhausted is True
    assert budget.spend() is False        # 耗尽之后必须一直是 False


def test_budget_stops_on_cancel():
    flag = {"cancelled": False}
    budget = search_relax.RelaxBudget(ops=10_000, seconds=60,
                                      cancel=lambda: flag["cancelled"])
    for _ in range(256):
        assert budget.spend() is True
    flag["cancelled"] = True
    # 取消每 256 次检查一次，所以最多再走一轮就必须停
    for _ in range(257):
        if not budget.spend():
            break
    assert budget.exhausted is True


def test_budget_stops_on_deadline():
    budget = search_relax.RelaxBudget(ops=10_000_000, seconds=0.001)
    time.sleep(0.02)          # 让 deadline 确实过去，别赌时钟粒度
    for _ in range(512):      # 每 256 次查一次表，512 次必然覆盖到
        if not budget.spend():
            break
    assert budget.exhausted is True


def test_fuzzy_name_score_is_bounded_on_a_long_name():
    """长文件名不得触发「全量滑窗」——那正是 51 秒的来源。"""
    calls = {"n": 0}
    real = search_relax._osa_distance

    def counted(left, right):
        calls["n"] += 1
        return real(left, right)

    query = "networkslicelatency"
    name = ("networkslicelatency-" * 40) + "报告.pptx"   # 约 820 字符
    budget = search_relax.RelaxBudget()
    original = search_relax._osa_distance
    search_relax._osa_distance = counted
    try:
        score = search_relax.fuzzy_name_score(query, name, budget=budget)
    finally:
        search_relax._osa_distance = original
    assert score > 0                       # 该召回的还得召回
    assert calls["n"] <= 2_000, f"单个名字打分做了 {calls['n']} 次编辑距离"


def test_fuzzy_name_score_still_catches_a_real_typo():
    assert search_relax.fuzzy_name_score("resmue", "resume.docx") > 0
    assert search_relax.fuzzy_name_score("算力方按", "算力方案汇报.pptx") > 0
    # 型号数字必须原样一致，别把 gpt4 放宽成 gptv
    assert search_relax.fuzzy_name_score("gpt4report", "gptvreport.pptx") == 0


def test_fuzzy_name_score_honours_an_exhausted_budget():
    budget = search_relax.RelaxBudget(ops=1)
    budget.spend()
    assert budget.exhausted
    assert search_relax.fuzzy_name_score("resmue", "resume.docx", budget=budget) == 0.0


# ---- 触发门槛：贵的路径只在「严格几乎没结果」时才跑 ----

def _fake_strict(count):
    def run(conn, query, **kwargs):
        return [
            search.FileResult(
                file_id=i, path=f"D:/x/{i}.pptx", name=f"{i}.pptx", ext=".pptx",
                mtime=0.0, size=1, page_count=1, status="ok", score=1.0,
                name_hit=True,
            )
            for i in range(count)
        ]
    return run


@pytest.mark.parametrize("strict_count,expect_called", [(0, True), (3, True), (4, False), (40, False)])
def test_expensive_paths_only_run_when_strict_is_nearly_empty(
        monkeypatch, strict_count, expect_called):
    """模糊匹配比别名贵几个量级，不能和别名共用 40 这个宽门槛。

    用户边打字边搜，中间态几乎全是低结果——门槛设在 40 等于每敲一个字都付一次
    最贵的路径。
    """
    called = {"fuzzy": False, "suggest": False}

    monkeypatch.setattr(search, "_search_strict", _fake_strict(strict_count))
    monkeypatch.setattr(search, "_fuzzy_ppt_results",
                        lambda *a, **k: called.__setitem__("fuzzy", True) or [])
    monkeypatch.setattr(search, "suggest_queries",
                        lambda *a, **k: called.__setitem__("suggest", True) or [])
    search.search(None, "resmue", limit=200)
    assert called["fuzzy"] is expect_called
    assert called["suggest"] is expect_called


def test_relaxation_never_runs_when_disabled(monkeypatch):
    monkeypatch.setattr(search, "_search_strict", _fake_strict(0))
    monkeypatch.setattr(search, "_fuzzy_ppt_results",
                        lambda *a, **k: pytest.fail("关掉联想之后不该走模糊路径"))
    assert search.search(None, "resmue", limit=200, enable_relaxed=False) == []


def test_suggest_queries_respects_the_shared_budget():
    """建议库要给 1200 页正文的每个词做归一化，必须能被预算叫停。"""
    class _Conn:
        def execute(self, sql, params=()):
            if "FROM files" in sql:
                return [{"name": f"报告{i}.pptx"} for i in range(4000)]
            return [{"raw_text": "算力集群调度" * 50} for _ in range(4000)]

    budget = search_relax.RelaxBudget(ops=20, seconds=60)
    started = time.perf_counter()
    search.suggest_queries(_Conn(), "算力方按", limit=3, budget=budget)
    assert budget.exhausted
    assert (time.perf_counter() - started) < 2.0
