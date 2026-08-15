"""v1.3.1 版本管理专项加固：留底开销 / 内存上限 / 失败可解释 / WAL 回缩。

这一组用例守的是三条产品底线：
  1. 用户按下 Ctrl+S 之后我们抢走的 CPU 必须尽量少（留底不是免费的）
  2. 托盘常驻数周内存不能只增不减
  3. 失败要说人话——尤其「文件正被 PowerPoint 打开」这个最常见的情形
"""
from __future__ import annotations

import os
import zipfile

import pytest

import fixtures_gen as fx

from pptx_finder import renderer
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    # 对账线程的「常用目录补漏」会去扫用户真实文档：测试里必须关掉
    monkeypatch.setenv("PPTUTOR_VERSION_RECONCILE_COMMON_DIRS", "0")


def _deck(tmp_path, name="d.pptx", blob_kb=64):
    docs = tmp_path / "decks"
    docs.mkdir(exist_ok=True)
    path = docs / name
    fx.make_pptx(path, [{"body": "算力 模型"}, {"body": "第二页"}])
    if blob_kb:
        blob = os.urandom(blob_kb * 1024)
        tmp = path.with_suffix(".t.pptx")
        with zipfile.ZipFile(path) as s, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as d:
            for it in s.infolist():
                d.writestr(it, s.read(it.filename))
            d.writestr("ppt/media/image1.png", blob)
        tmp.replace(path)
    return path


# ---------- 留底开销：自检重组不再重新压缩 ----------

def test_verify_rebuild_uses_stored_compression(tmp_path):
    """保真自检的临时包写完就删，为它把几十 MB 图片再 DEFLATE 一遍纯属白烧 CPU。

    50 MB 稿实测这一步 1436 ms → 145 ms，整次留底 2.0 s → 0.7 s。
    """
    deck = _deck(tmp_path)
    doc_id = vault.doc_id_for(str(deck))
    names, parts = vault._dedup_store(doc_id, str(deck))

    written: list[int] = []
    real = vault._write_zip

    def spy(dest, did, ns, ps, *, compression=zipfile.ZIP_DEFLATED):
        written.append(compression)
        return real(dest, did, ns, ps, compression=compression)

    vault._write_zip = spy
    try:
        assert vault._verify(doc_id, names, parts) is True
    finally:
        vault._write_zip = real
    assert written == [zipfile.ZIP_STORED]


def test_restore_output_stays_compressed(tmp_path):
    """恢复出来的文件是要交回用户磁盘的，必须照常压缩，不能跟着自检一起变 STORED。"""
    deck = _deck(tmp_path)
    conn = store.connect(vault.db_path())
    store.init_db(conn)
    vid = vault.snapshot(conn, str(deck))
    assert vid
    doc_id = vault.doc_id_for(str(deck))
    out = tmp_path / "restored.pptx"

    assert vault.rebuild_to(doc_id, vid, str(out)) is True
    with zipfile.ZipFile(out) as z:
        modes = {i.compress_type for i in z.infolist() if not i.is_dir()}
    assert zipfile.ZIP_DEFLATED in modes
    conn.close()


def test_verify_still_catches_a_missing_object(tmp_path):
    """换压缩方式不能削弱自检本身：对象被删掉时必须仍然判失败。"""
    deck = _deck(tmp_path)
    doc_id = vault.doc_id_for(str(deck))
    names, parts = vault._dedup_store(doc_id, str(deck))
    assert vault._verify(doc_id, names, parts) is True

    victim = vault._object_path(doc_id, parts[names[0]])
    victim.unlink()
    vault._VERIFIED_OBJECT_PATHS.clear()
    assert vault._verify(doc_id, names, parts) is False


# ---------- 内存：模块级缓存必须有上限 ----------

def test_verified_object_cache_is_bounded(monkeypatch):
    """对象池有几万个文件（生产实测 48,775），这张表不设上限就是常驻内存只增不减。"""
    monkeypatch.setattr(vault, "_VERIFIED_OBJECT_CAP", 32)
    vault._VERIFIED_OBJECT_PATHS.clear()
    for i in range(500):
        vault._verified_mark(f"C:/vault/_objects/{i:016x}")
    assert len(vault._VERIFIED_OBJECT_PATHS) == 32
    # LRU 语义：最近标记的还在，最老的已被淘汰
    assert vault._verified_hit("C:/vault/_objects/00000000000001f3")
    assert not vault._verified_hit("C:/vault/_objects/0000000000000000")
    vault._VERIFIED_OBJECT_PATHS.clear()


def test_verified_cache_forget_on_missing_object(tmp_path):
    obj = tmp_path / "obj"
    obj.write_bytes(b"hello")
    import xxhash

    h = xxhash.xxh64(b"hello").hexdigest()
    assert vault._object_is_valid(obj, h) is True
    assert vault._verified_hit(str(obj)) is True
    obj.unlink()
    assert vault._object_is_valid(obj, h) is False
    assert vault._verified_hit(str(obj)) is False


def test_render_failure_table_is_swept(monkeypatch):
    """渲染失败熔断表只在读取时判 TTL，过期项此前永远留着——翻过的每一页都占一条。"""
    renderer._failed_until.clear()
    monkeypatch.setattr(renderer, "_failed_swept_at", 0.0)
    now = renderer.time.monotonic()
    for i in range(50):
        renderer._failed_until[(f"C:/x/{i}.pptx", 1, "k", 1920)] = now - 10  # 全部已过期
    renderer._failed_until[("C:/live.pptx", 1, "k", 1920)] = now + 999      # 未过期
    renderer._sweep_failed_until()
    assert list(renderer._failed_until) == [("C:/live.pptx", 1, "k", 1920)]
    renderer._failed_until.clear()


# ---------- 失败要说人话 ----------

def test_restore_reports_locked_target(tmp_path):
    """目标被 PowerPoint 独占是最常见的失败；此时原文件毫发无损，必须讲清楚。"""
    deck = _deck(tmp_path)
    mgr = VersionManager(index_roots=[str(deck.parent)])
    vid = mgr.snapshot_now(str(deck), notify=False)
    assert vid

    def _boom(*_a, **_k):
        raise PermissionError(13, "The process cannot access the file")

    original = os.replace
    os.replace = _boom
    try:
        ok = mgr.restore_to(str(deck), vid)
    finally:
        os.replace = original
    assert ok is False
    assert mgr.last_restore_error() == vault.REBUILD_ERR_LOCKED
    assert deck.exists()  # 原文件没被动过
    mgr.stop()


def test_restore_reports_missing_recovery_point(tmp_path):
    deck = _deck(tmp_path)
    mgr = VersionManager(index_roots=[str(deck.parent)])
    assert mgr.restore_to(str(deck), "no-such-version") is False
    assert mgr.last_restore_error() == vault.REBUILD_ERR_MISSING
    mgr.stop()


def test_successful_restore_clears_error(tmp_path):
    deck = _deck(tmp_path)
    mgr = VersionManager(index_roots=[str(deck.parent)])
    vid = mgr.snapshot_now(str(deck), notify=False)
    assert mgr.restore_to(str(deck), vid) is True
    assert mgr.last_restore_error() == ""
    mgr.stop()


# ---------- WAL 不再长期挂着高水位 ----------

def test_vault_connection_caps_wal_but_index_does_not(tmp_path):
    """版本库设上限（零散小写，收益大）；索引库刻意不设。

    索引库的写入形态是建库时的批量灌入，回缩 WAL 要额外截断文件——A/B 实测
    分档写入吞吐的末/首比从 0.84 掉到 0.72。它的 WAL 只在扫描期短暂变大。
    """
    from pptx_finder import db as index_db

    vconn = store.connect(tmp_path / "v.db")
    assert vconn.execute("PRAGMA journal_size_limit").fetchone()[0] == store.WAL_SIZE_LIMIT_BYTES
    vconn.close()
    iconn = index_db.connect(tmp_path / "i.db")
    assert iconn.execute("PRAGMA journal_size_limit").fetchone()[0] == -1
    iconn.close()


# ---------- 硬底线：留底期间用户仍能写自己的文件 ----------

def test_snapshot_never_locks_the_source_file(tmp_path):
    """PPT Doctor 开着的时候，用户的 PowerPoint 必须照常存得了盘。

    留底先把源文件复制成不可变暂存副本，全程只读源一次且不独占——
    这里直接验「留底刚做完，源文件立刻可写」，并顺带确认暂存不残留。
    """
    deck = _deck(tmp_path)
    mgr = VersionManager(index_roots=[str(deck.parent)])
    assert mgr.snapshot_now(str(deck), notify=False)
    with open(deck, "r+b") as f:      # 拿得到写句柄 = 没有被我们独占
        f.seek(0)
        assert f.read(2) == b"PK"
    tmp_dir = vault.vault_dir() / "_tmp"
    assert not list(tmp_dir.glob("*")) if tmp_dir.is_dir() else True
    mgr.stop()


# ---------- 生涯履历改用 PPT 内嵌创建时间 ----------

def test_parser_reads_embedded_created_at(tmp_path):
    """dcterms:created 才是稿子真正的诞生时间；mtime 会被复制/同步重写。"""
    import zipfile as zf_mod
    from datetime import datetime, timezone

    from pptx_finder.parser import parse_pptx

    deck = _deck(tmp_path, blob_kb=0)
    tmp = deck.with_suffix(".c.pptx")
    core = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dcterms="http://purl.org/dc/terms/">'
        '<dcterms:created>2019-11-15T17:59:11Z</dcterms:created>'
        '</cp:coreProperties>'
    )
    with zf_mod.ZipFile(deck) as s, zf_mod.ZipFile(tmp, "w", zf_mod.ZIP_DEFLATED) as d:
        for it in s.infolist():
            if it.filename != "docProps/core.xml":
                d.writestr(it, s.read(it.filename))
        d.writestr("docProps/core.xml", core)
    tmp.replace(deck)

    got = parse_pptx(str(deck)).created_at
    expect = datetime(2019, 11, 15, 17, 59, 11, tzinfo=timezone.utc).timestamp()
    assert abs(got - expect) < 1


def test_parser_created_at_absent_is_zero(tmp_path):
    """读不到就是 0，绝不能因此让解析失败。"""
    from pptx_finder.parser import parse_pptx

    deck = _deck(tmp_path, blob_kb=0)
    parsed = parse_pptx(str(deck))
    assert parsed.status == "ok"
    assert parsed.created_at >= 0.0


def test_chronicle_buckets_by_created_at_then_mtime():
    """有创建时间就按它分年，没有才退回 mtime——两者混在一起也要各归各年。"""
    from datetime import datetime

    from pptx_finder.report_insights import _chronicle_year_ts
    from pptx_finder.stats import FileStat

    old = datetime(2019, 5, 1).timestamp()
    now = datetime(2026, 5, 1).timestamp()
    with_created = FileStat(name="a", mtime=now, size=1, page_count=1, status="ok",
                            group_id=None, char_count=0, created_at=old)
    without = FileStat(name="b", mtime=now, size=1, page_count=1, status="ok",
                       group_id=None, char_count=0)
    assert datetime.fromtimestamp(_chronicle_year_ts(with_created)).year == 2019
    assert datetime.fromtimestamp(_chronicle_year_ts(without)).year == 2026


def test_created_at_column_survives_filename_only_upsert(tmp_path):
    """仅登记文件名的那一遍不带创建时间，不能把已经读到的好值冲成 0。"""
    from pptx_finder import db as index_db

    conn = index_db.connect(tmp_path / "i.db")
    index_db.init_db(conn)
    index_db.upsert_file(
        conn, path="C:/x/a.pptx", name="a.pptx", ext=".pptx", size=10, mtime=1.0,
        content_hash="h", page_count=3, status="ok", error="", indexed_at=1.0,
        created_at=1234567890.0)
    index_db.upsert_file(
        conn, path="C:/x/a.pptx", name="a.pptx", ext=".pptx", size=11, mtime=2.0,
        content_hash="h2", page_count=0, status="filename_only", error="", indexed_at=2.0)
    row = conn.execute("SELECT created_at FROM files WHERE path='C:/x/a.pptx'").fetchone()
    assert float(row["created_at"]) == 1234567890.0
    conn.close()
