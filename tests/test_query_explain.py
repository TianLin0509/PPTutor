from __future__ import annotations

from pptx_finder.query_explain import explain_query, suggestion_keys


def test_explain_query_lists_scope_terms_and_and_rule():
    exp = explain_query('"算力集群" 昇腾 2026', "content")

    assert "范围：仅内容" in exp.summary
    assert "同页包含：昇腾 + 2026" in exp.summary
    assert "精确短语：算力集群" in exp.summary
    assert "多条件为 AND" in exp.summary


def test_short_ascii_term_is_explained():
    exp = explain_query("AI 预算", "all")

    assert exp.short_ascii_terms == ["AI"]
    assert "短英文/数字按完整词匹配：AI" in exp.summary


def test_suggestion_keys_offer_scope_recovery_and_filename_fallback():
    keys = suggestion_keys('"昇腾" 集群', "content")

    assert "unquote" in keys
    assert "fewer" in keys
    assert "allmode" in keys
    assert "filename" in keys
