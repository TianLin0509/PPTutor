"""indexer 单元测试：建库 / FTS 命中 / 增量改删 / 并行 / .ppt 登记。"""
from __future__ import annotations

import fixtures_gen as fx

from pptx_finder import db, indexer
from pptx_finder.text_tokenize import tokenize


def _mk(p, bodies):
    fx.make_pptx(p, [{"body": b} for b in bodies])


def _fts_files(conn, word):
    rows = conn.execute(
        "SELECT DISTINCT file_id FROM pages_fts WHERE pages_fts MATCH ?",
        (tokenize(word),),
    ).fetchall()
    return {r[0] for r in rows}


def test_index_and_fts(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _mk(docs / "a.pptx", ["算力 ALPHA", "BETA"])
    _mk(docs / "b.pptx", ["GAMMA 昇腾"])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    prog = []
    summary = indexer.update_index(
        conn, [str(docs)],
        progress_cb=lambda d, t, c: prog.append((d, t)), workers=1,
    )
    s = db.stats(conn)
    assert s["file_count"] == 2
    assert s["page_count"] == 3
    assert summary["indexed"] == 2
    assert prog and prog[-1][0] == prog[-1][1]  # 进度走到满
    # “算力”只在 a，“昇腾”只在 b
    assert len(_fts_files(conn, "算力")) == 1
    assert len(_fts_files(conn, "昇腾")) == 1
    assert _fts_files(conn, "算力") != _fts_files(conn, "昇腾")


def test_incremental_modify(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    a = docs / "a.pptx"
    _mk(a, ["算力 OLD"])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    assert _fts_files(conn, "算力")

    # 重写为不同内容（size 变 → 触发重索引）
    _mk(a, ["全新内容 ZZTOP 昇腾算力集群部署方案细节"])
    indexer.update_index(conn, [str(docs)], workers=1)
    assert db.stats(conn)["file_count"] == 1  # 无重复
    assert _fts_files(conn, "zztop")  # 新内容可搜
    # 旧的孤立词 OLD 应已消失
    assert not _fts_files(conn, "OLD")


def test_incremental_delete(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _mk(docs / "a.pptx", ["AAA"])
    b = docs / "b.pptx"
    _mk(b, ["BBB"])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    assert db.stats(conn)["file_count"] == 2

    b.unlink()
    summary = indexer.update_index(conn, [str(docs)], workers=1)
    assert summary["deleted"] == 1
    assert db.stats(conn)["file_count"] == 1
    assert not _fts_files(conn, "BBB")


def test_ppt_filename_only(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "old.ppt").write_bytes(b"\xd0\xcf\x11\xe0 fake ppt binary content")
    _mk(docs / "new.pptx", ["现代格式"])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)

    row = db.get_file_by_path(conn, str(docs / "old.ppt"))
    assert row is not None
    assert row["status"] == "filename_only"
    assert row["ext"] == ".ppt"
    # .ppt 不索引内容
    assert db.stats(conn)["page_count"] == 1  # 只有 new.pptx 的 1 页


def test_parallel_workers(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(3):
        _mk(docs / f"d{i}.pptx", [f"内容{i} 并行解析测试"])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    summary = indexer.update_index(conn, [str(docs)], workers=2)
    assert summary["indexed"] == 3
    assert db.stats(conn)["file_count"] == 3
    assert len(_fts_files(conn, "并行解析测试")) == 3


def test_db_maintain_optimizes_fts_without_error(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(
        conn,
        path=str(tmp_path / "a.pptx"),
        name="a.pptx",
        ext=".pptx",
        size=1,
        mtime=1,
        content_hash="h",
        page_count=1,
        status="ok",
        error="",
        indexed_at=1,
    )
    db.replace_pages(conn, fid, [(1, "hello world", "hello world")])
    conn.commit()

    result = db.maintain(conn)

    assert result["error"] == ""
    assert result["fts_optimized"] >= 1
    assert result["checkpointed"] is True


def test_two_stage_filename_searchable_before_content(tmp_path):
    """两阶段渐进：文件名先可搜，内容解析后才可搜。"""
    from pptx_finder import search as sm
    docs = tmp_path / "d"
    docs.mkdir()
    p = docs / "算力方案报告.pptx"
    _mk(p, ["昇腾 集群 部署"])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    # 阶段 1：仅登记文件名（pending）
    indexer._register_pending(conn, p, p.stat())
    conn.commit()
    r1 = sm.search(conn, "算力方案")
    assert r1 and r1[0].name_hit          # 文件名立即可搜
    assert not sm.search(conn, "昇腾")    # 内容此时还搜不到
    # 阶段 2：解析升级
    indexer._write_result(conn, indexer._index_one(str(p)))
    conn.commit()
    r2 = sm.search(conn, "昇腾")
    assert r2 and r2[0].hits              # 内容现在可搜


def test_index_one_records_exact_content_hash(tmp_path):
    """解析阶段记录真实 sha256，用于识别完全相同副本。"""
    p = tmp_path / "x.pptx"
    _mk(p, ["内容"])
    res = indexer._index_one(str(p))
    assert res["status"] == "ok"
    assert res["content_hash"].startswith("sha256:")
    assert len(res["content_hash"]) == len("sha256:") + 64
