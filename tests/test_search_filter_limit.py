"""检索筛选必须在 FTS 截断前生效，避免候选池被其他类型/目录挤满。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pptx_finder import db, search
from pptx_finder.text_tokenize import tokenize


def _add_page(conn, path: Path, ext: str, raw: str) -> None:
    fid = db.upsert_file(
        conn,
        path=str(path),
        name=path.name,
        ext=ext,
        size=1,
        mtime=1.0,
        content_hash=f"hash:{path.name}",
        page_count=1,
        status="ok",
        error="",
        indexed_at=1.0,
    )
    db.replace_pages(conn, fid, [(1, raw, tokenize(raw))])


@pytest.mark.parametrize("filter_kind", ["ext", "scope"])
def test_fts_filter_is_applied_before_global_candidate_limit(tmp_path, filter_kind):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    wanted_root = tmp_path / "wanted"
    noisy_root = tmp_path / "noise"
    wanted_root.mkdir()
    noisy_root.mkdir()
    needle = "截断前筛选唯一词"

    # 3001 个更高相关候选足以填满旧实现的全局 LIMIT 3000。
    noisy_ext = ".pdf" if filter_kind == "ext" else ".pptx"
    noisy_text = " ".join([needle] * 8)
    for i in range(3001):
        _add_page(conn, noisy_root / f"noise-{i}{noisy_ext}", noisy_ext, noisy_text)

    target = wanted_root / "target.pptx"
    _add_page(conn, target, ".pptx", needle)
    conn.commit()

    kwargs = {"exts": (".pptx",)} if filter_kind == "ext" else {"scope": str(wanted_root)}
    results = search.search(conn, needle, **kwargs)

    assert [r.path for r in results] == [str(target)]
