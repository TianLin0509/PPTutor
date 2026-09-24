"""搜索匹配方式：模糊（默认，精确优先 + 结果少时自动联想）/ 精确（不联想 + 英文数字按整词）。"""
from __future__ import annotations

from pptx_finder import db, namestore, search
from pptx_finder.text_tokenize import contains_exact_word, tokenize

BASE_T = 1_700_000_000


def _add(conn, name: str, text: str, mtime: float = BASE_T):
    fid = db.upsert_file(
        conn, path=f"C:/mode/{name}", name=name, ext=".pptx", size=100, mtime=mtime,
        content_hash=f"h:{name}", page_count=1, status="ok", error="", indexed_at=mtime,
    )
    db.replace_pages(conn, fid, [(1, text, tokenize(text))])
    return fid


def _conn(tmp_path):
    conn = db.connect(tmp_path / "mode.db")
    db.init_db(conn)
    return conn


def _names(rows):
    return sorted(r.name for r in rows)


# ---- 整词判定：只在「针的边缘是英文字母/数字」的那一侧要求边界 ----
def test_exact_word_needs_boundary_only_on_ascii_alnum_edges():
    assert contains_exact_word("ran 架构", "ran")
    assert contains_exact_word("ran架构演进", "ran")          # 中文紧挨着不算粘连
    assert contains_exact_word("5g基站", "5g")
    assert not contains_exact_word("brand transfer", "ran")
    assert not contains_exact_word("15g", "5g")
    assert contains_exact_word("无线接入网规划", "接入网")      # 中文没有词边界，照旧子串
    assert contains_exact_word("brand ran", "ran")              # 第一处粘连，第二处独立
    assert contains_exact_word("massive mimo 天线", "massive mimo")


# ---- 内容搜索 ----
def test_exact_mode_does_not_match_english_inside_longer_words(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "品牌手册.pptx", "brand transfer training")
    _add(conn, "接入网.pptx", "RAN 架构 演进")
    conn.commit()

    fuzzy = search.search(conn, "RAN")
    exact = search.search(conn, "RAN", exact_match=True)
    conn.close()

    assert _names(fuzzy) == ["品牌手册.pptx", "接入网.pptx"]
    assert _names(exact) == ["接入网.pptx"]


def test_exact_mode_keeps_chinese_substring_matching(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "规划.pptx", "无线接入网规划")
    conn.commit()

    rows = search.search(conn, "接入网", exact_match=True)
    conn.close()

    assert _names(rows) == ["规划.pptx"]


def test_exact_mode_applies_to_filenames_too(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "brand-guide.pptx", "正文")
    _add(conn, "RAN-roadmap.pptx", "正文")
    conn.commit()

    rows = search.search(conn, "ran", exact_match=True)
    conn.close()

    assert _names(rows) == ["RAN-roadmap.pptx"]


def test_exact_mode_never_adds_relaxed_results(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _add(conn, "算力方案.pptx", "本页介绍算力方案和部署步骤")
    conn.commit()
    monkeypatch.setattr(search, "suggest_queries", lambda *_a, **_k: [])

    fuzzy = search.search(conn, "算力方按")
    exact = search.search(conn, "算力方按", exact_match=True)
    conn.close()

    assert fuzzy and fuzzy[0].relaxed is True          # 默认模糊：错字也能联想到
    assert exact == []                                  # 精确：错字就是没有


# ---- 「全部文件（仅文件名）」范围 ----
def _store(tmp_path, paths):
    builder = namestore.NameStoreBuilder()
    for p in paths:
        builder.add(p, 1, BASE_T)
    return namestore.NameStore(builder.write(tmp_path / "names.idx"))


def test_all_files_scope_exact_mode_uses_whole_words(tmp_path):
    paths = [r"C:\x\brand.txt", r"C:\x\RAN规划.docx", r"C:\x\ran-notes.md"]
    with _store(tmp_path, paths) as store:
        fuzzy = search.search_names(store, "ran", exists=lambda _p: True)
        exact = search.search_names(store, "ran", exists=lambda _p: True, exact_match=True)
    assert _names(fuzzy) == ["RAN规划.docx", "brand.txt", "ran-notes.md"]
    assert _names(exact) == ["RAN规划.docx", "ran-notes.md"]


def test_all_files_scope_exact_mode_survives_case_nocase_path_functions(tmp_path):
    """审查发现：case: / nocase: / path: 走另一条构造分支，整词规则曾被悄悄跳过。"""
    paths = [r"C:\x\brand.txt", r"C:\x\RAN规划.docx"]
    with _store(tmp_path, paths) as store:
        for q in ("case:RAN", "nocase:ran", "path:ran"):
            rows = search.search_names(store, q, exists=lambda _p: True, exact_match=True)
            assert "brand.txt" not in _names(rows), q
            assert "RAN规划.docx" in _names(rows), q


def test_all_files_scope_exact_mode_never_relaxes(tmp_path):
    with _store(tmp_path, [r"C:\x\算力方案.pptx"]) as store:
        fuzzy = search.search_names(store, "算力方按", exists=lambda _p: True)
        exact = search.search_names(store, "算力方按", exists=lambda _p: True, exact_match=True)
    assert fuzzy and fuzzy[0].relaxed is True
    assert exact == []
