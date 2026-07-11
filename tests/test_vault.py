"""vault 核心测试：快照↔重组往返一致性（命脉）、增量、恢复旧版、跨版本搜。"""
from __future__ import annotations

import fixtures_gen as fx
import pytest

from pptx_finder.parser import parse_pptx
from pptx_finder.text_tokenize import build_fts_match
from pptx_finder.versioning import store, vault


@pytest.fixture(autouse=True)
def _isolated_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def _conn():
    c = store.connect(vault.db_path())
    store.init_db(c)
    return c


def test_snapshot_rebuild_roundtrip(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "第一版 算力 集群"}, {"body": "第二页 内容"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(p))
    assert vid
    out = tmp_path / "restored.pptx"
    assert vault.rebuild_to(vault.doc_id_for(str(p)), vid, str(out))
    # 去重重组：内容(逐页文本)一致、能正常打开（字节可能因重新压缩而不同）
    d0, d1 = parse_pptx(str(p)), parse_pptx(str(out))
    assert d1.status == "ok"
    assert d1.page_count == d0.page_count
    assert [pg.raw_text for pg in d1.pages] == [pg.raw_text for pg in d0.pages]


def test_rebuild_failure_keeps_existing_destination_byte_for_byte(tmp_path):
    """缺对象/坏快照不能先截断用户当前文件，再返回失败。"""
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "可恢复版本"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(p))
    did = vault.doc_id_for(str(p))
    manifest = vault.manifest_for(did, vid)
    missing_hash = next(iter(manifest["parts"].values()))
    (vault._global_objects_dir() / missing_hash).unlink()

    dest = tmp_path / "current.pptx"
    original = b"CURRENT USER FILE MUST SURVIVE"
    dest.write_bytes(original)

    assert vault.rebuild_to(did, vid, str(dest)) is False
    assert dest.read_bytes() == original


def test_dedup_reuses_unchanged_parts(tmp_path):
    """两版只差一页，objects 大量复用——不是翻倍存。"""
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "页1 固定"}, {"body": "页2 固定"}, {"body": "页3 原始"}])
    conn = _conn()
    vault.snapshot(conn, str(p))
    n1 = len(list(vault._global_objects_dir().glob("*")))
    fx.make_pptx(p, [{"body": "页1 固定"}, {"body": "页2 固定"}, {"body": "页3 改了几个字"}])
    vault.snapshot(conn, str(p))
    n2 = len(list(vault._global_objects_dir().glob("*")))
    assert n2 > n1            # 确有新增（变化的 part）
    assert n2 < n1 * 2        # 但远非翻倍（未变 part 复用）


def test_snapshot_skips_unchanged(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "内容"}])
    conn = _conn()
    assert vault.snapshot(conn, str(p))          # 首次记
    assert vault.snapshot(conn, str(p)) is None   # 没变 → 跳过


def test_versions_accumulate_and_restore_old(tmp_path):
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "旧内容 OLDWORD"}])
    conn = _conn()
    v1 = vault.snapshot(conn, str(p))
    fx.make_pptx(p, [{"body": "新内容 NEWWORD 昇腾"}])  # 改内容（size 变）
    v2 = vault.snapshot(conn, str(p))
    assert v2 and v2 != v1
    did = vault.doc_id_for(str(p))
    assert len(store.list_versions(conn, did)) == 2

    # 恢复旧版 → 内容确实是旧的
    out = tmp_path / "old.pptx"
    assert vault.rebuild_to(did, v1, str(out))
    txt = "".join(pg.raw_text for pg in parse_pptx(str(out)).pages)
    assert "OLDWORD" in txt and "NEWWORD" not in txt


def test_cross_version_search_finds_deleted_content(tmp_path):
    """以前某版有、现在删了的内容，仍能在历史版本里搜到。"""
    p = tmp_path / "a.pptx"
    fx.make_pptx(p, [{"body": "含有 量子计算 章节"}])
    conn = _conn()
    vault.snapshot(conn, str(p))
    fx.make_pptx(p, [{"body": "改成 经典方案 删掉了那段"}])  # “量子计算”从当前版消失
    vault.snapshot(conn, str(p))

    hits = store.search_versions(conn, build_fts_match("量子计算"))
    assert hits, "历史版本里应能搜到已删除的内容"
    did = vault.doc_id_for(str(p))
    assert any(h["doc_id"] == did for h in hits)
