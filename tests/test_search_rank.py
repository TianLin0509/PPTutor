"""search 排序：文件名完全匹配优先 + scanner 排除 Temp/临时目录。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder import db, indexer, scanner, search
from pptx_finder.config import EXCLUDE_DIR_NAMES
from pptx_finder.scanner import iter_ppt_files


def _build(tmp_path, files):
    docs = tmp_path / "docs"
    docs.mkdir()
    for fn, bodies in files.items():
        fx.make_pptx(docs / fn, [{"body": b} for b in bodies])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


def test_name_exact_match_ranks_first(tmp_path):
    """搜「周报」时，文件名正好是「周报.pptx」应居首，盖过内容里反复出现该词的文件。"""
    conn = _build(tmp_path, {
        "周报.pptx": ["封面页"],                                    # 名字完全匹配、内容无关
        "2026年周报汇总最终.pptx": ["周报 周报 周报 详细内容关键词"],   # 含词且内容更相关
    })
    res = search.search(conn, "周报")
    assert res
    assert res[0].name == "周报.pptx"


def test_name_with_ext_query_matches_stem(tmp_path):
    """搜「b.pptx」（带扩展名）时，b.pptx 应居首（q_stem 去扩展名后 == b）。"""
    conn = _build(tmp_path, {
        "b.pptx": ["内容"],
        "ab-table.pptx": ["b 出现 b 出现 b"],
    })
    res = search.search(conn, "b.pptx")
    assert res
    assert res[0].name == "b.pptx"


def test_scanner_skips_temp(tmp_path):
    (tmp_path / "Temp").mkdir()
    fx.make_pptx(tmp_path / "Temp" / "junk.pptx", [{"body": "x"}])
    (tmp_path / "real").mkdir()
    fx.make_pptx(tmp_path / "real" / "keep.pptx", [{"body": "x"}])
    found = {p.name for p in iter_ppt_files([str(tmp_path)])}
    assert "keep.pptx" in found
    assert "junk.pptx" not in found      # Temp 目录被排除，临时文件不进索引


def test_exclude_has_temp_dirs():
    for d in ("temp", "tmp", "local settings"):
        assert d in EXCLUDE_DIR_NAMES


def test_scanner_includes_company_documents_under_appdata_roaming(tmp_path):
    roaming = tmp_path / "Users" / "l00807938" / "AppData" / "Roaming" / "CorpDocs"
    roaming.mkdir(parents=True)
    fx.make_pptx(roaming / "old-project.pptx", [{"body": "legacy"}])

    found = {p.name for p in iter_ppt_files([str(tmp_path)])}

    assert "old-project.pptx" in found


def test_scanner_never_indexes_its_own_appdata_store(tmp_path, monkeypatch):
    own_store = tmp_path / "Users" / "me" / "AppData" / "Local" / "pptx-finder"
    own_store.mkdir(parents=True)
    fx.make_pptx(own_store / "version-object.pptx", [{"body": "internal"}])
    roaming = tmp_path / "Users" / "me" / "AppData" / "Roaming" / "CorpDocs"
    roaming.mkdir(parents=True)
    fx.make_pptx(roaming / "real.pptx", [{"body": "real"}])
    monkeypatch.setattr(scanner, "data_dir", lambda: own_store)

    found = {p.name for p in iter_ppt_files([str(tmp_path)])}

    assert "real.pptx" in found
    assert "version-object.pptx" not in found
