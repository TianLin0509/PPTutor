# -*- coding: utf-8 -*-
"""对象池的压缩存储。

版本库是整个应用最不能出错的地方，所以这一层的设计是「只加不改」：
文件名永远是**未压缩内容**的 xxh64，压缩件只是多一个 .z 后缀的存放形态。
下面每条用例都在钉同一件事——不管零件存成哪种形态，恢复出来的字节必须一模一样。
"""
from __future__ import annotations

import gzip
import io
import os
import zipfile

import fixtures_gen as fx
import pytest

from pptx_finder.versioning import store, vault


@pytest.fixture(autouse=True)
def _isolated_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def _conn():
    c = store.connect(vault.db_path())
    store.init_db(c)
    return c


def _parts_of(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist() if not i.is_dir()}


def test_text_parts_are_stored_packed(tmp_path):
    """.xml / .rels 该压——这是 788 MB 的来源。"""
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "算力 集群 " * 200}])
    vault.snapshot(_conn(), str(p))
    names = [q.name for q in vault._global_objects_dir().iterdir() if q.is_file()]
    assert names, "对象池不该是空的"
    assert any(n.endswith(".z") for n in names), "文本零件一个都没压"


def test_rebuild_is_byte_identical_per_part(tmp_path):
    """命脉：压缩存放不能改变任何一个零件的字节。"""
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "第一页 内容"}, {"body": "第二页 内容"}])
    before = _parts_of(p)
    conn = _conn()
    vid = vault.snapshot(conn, str(p))
    out = tmp_path / "restored.pptx"
    assert vault.rebuild_to(vault.doc_id_for(str(p)), vid, str(out))
    assert _parts_of(out) == before


def test_snapshot_stays_in_dedup_mode(tmp_path):
    """压缩不能把留底逼退成 mode=full。

    每次留底都会先重组一遍做保真自检，自检不过就整份拷贝兜底（mode=full）。
    解压路径一旦有问题，表现不是报错而是**每份稿子都存整包**——版本库不降反涨，
    而且没有任何人会注意到。这条用例专门钉住这个静默退化。
    """
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "第一版"}])
    conn = _conn()
    did = vault.doc_id_for(str(p))
    v1 = vault.snapshot(conn, str(p))
    assert vault.manifest_for(did, v1)["mode"] == "dedup"

    fx.make_pptx(p, [{"body": "第一版"}, {"body": "新加一页"}])
    v2 = vault.snapshot(conn, str(p))
    assert vault.manifest_for(did, v2)["mode"] == "dedup"
    # 未变的零件仍然共享：压缩没有破坏内容寻址的去重
    shared = set(vault.manifest_for(did, v1)["parts"].values()) & set(
        vault.manifest_for(did, v2)["parts"].values()
    )
    assert shared, "两版之间一个零件都没复用，说明去重被压缩破坏了"


def test_object_filename_stays_the_uncompressed_hash(tmp_path):
    """压缩件的名字必须还是「解压后内容」的哈希，否则去重和 GC 全部失准。"""
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "内容寻址"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(p))
    did = vault.doc_id_for(str(p))
    for name, object_hash in vault.manifest_for(did, vid)["parts"].items():
        path = vault._object_path(did, object_hash)
        assert path.is_file(), name
        assert vault._object_hash_of(path) == object_hash
        assert vault._hash_object_file(path) == object_hash
        assert vault._object_is_valid(path, object_hash) is True


def test_incompressible_media_part_is_left_raw(tmp_path):
    """媒体零件本来就是压过的格式，再压一遍只是白烧 CPU。"""
    src = tmp_path / "media.pptx"
    fx.make_pptx(src, [{"body": "带图"}])
    blob = bytes(range(256)) * 64          # 熵高、压不动
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("ppt/media/image1.png", blob)
    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))
    media_hash = vault.manifest_for(did, vid)["parts"]["ppt/media/image1.png"]
    assert vault._object_path(did, media_hash).name == media_hash


def test_existing_raw_object_is_reused_not_rewritten(tmp_path):
    """存量零件一个不动：升级前存下的原始件必须原地复用。"""
    src = tmp_path / "deck.pptx"
    fx.make_pptx(src, [{"body": "老库里已经有的零件"}])
    parts = _parts_of(src)
    name, payload = next(
        (n, b) for n, b in parts.items() if n.casefold().endswith(".xml")
    )
    object_hash = vault.xxhash.xxh64(payload).hexdigest()
    raw = vault._global_objects_dir() / object_hash
    raw.write_bytes(payload)
    stat_before = raw.stat()

    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))

    assert vault.manifest_for(did, vid)["parts"][name] == object_hash
    assert raw.is_file(), "原始件被删了"
    assert raw.stat().st_mtime_ns == stat_before.st_mtime_ns, "原始件被重写了"
    assert not (vault._global_objects_dir() / f"{object_hash}.z").exists(), "多存了一份"
    out = tmp_path / "restored.pptx"
    assert vault.rebuild_to(did, vid, str(out))
    assert _parts_of(out)[name] == payload


def test_oversized_text_part_skips_compression(tmp_path, monkeypatch):
    """超过上限就放弃压缩：不为一个巨大零件在内存里攒压缩缓冲。"""
    monkeypatch.setattr(vault, "_COMPRESS_MAX_BYTES", 1024)
    src = tmp_path / "big.pptx"
    fx.make_pptx(src, [{"body": "x"}])
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("ppt/notesSlides/huge.xml", b"<a>" + b"y" * 20000 + b"</a>")
    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))
    h = vault.manifest_for(did, vid)["parts"]["ppt/notesSlides/huge.xml"]
    assert vault._object_path(did, h).name == h


def test_failed_round_trip_check_falls_back_to_raw(tmp_path, monkeypatch):
    """压缩路径就算彻底坏掉（这里让它吐出根本不是 gzip 的字节），也只能退化成
    「没压」，不能往对象池里放一个解不回原内容的文件。"""
    monkeypatch.setattr(vault, "_pack", lambda data: b"definitely not gzip")
    src = tmp_path / "deck.pptx"
    fx.make_pptx(src, [{"body": "退回原始件"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))
    for h in vault.manifest_for(did, vid)["parts"].values():
        assert vault._object_path(did, h).name == h
    out = tmp_path / "restored.pptx"
    assert vault.rebuild_to(did, vid, str(out))
    assert _parts_of(out) == _parts_of(src)


def test_corrupt_packed_object_is_reported_not_raised(tmp_path):
    """压缩件被截断＝这个对象不可用，要能被体检抓到，而不是抛穿调用栈。"""
    src = tmp_path / "deck.pptx"
    fx.make_pptx(src, [{"body": "会被弄坏的零件"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))
    packed = next(
        q for q in vault._global_objects_dir().iterdir() if q.name.endswith(".z")
    )
    object_hash = vault._object_hash_of(packed)
    packed.write_bytes(packed.read_bytes()[:8])
    vault._verified_forget(str(packed))

    assert vault._object_is_valid(packed, object_hash) is False
    report = vault.audit_repository(conn, deep=True)
    assert report["ok"] is False
    assert vid in report["invalid_versions"]
    assert report["read_errors"] + report["hash_errors"] >= 1


def test_gc_keeps_referenced_packed_objects(tmp_path):
    """GC 的引用集是哈希；压缩件不能因为文件名多了个后缀就被当成孤儿删掉。"""
    src = tmp_path / "deck.pptx"
    fx.make_pptx(src, [{"body": "必须留下"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(src))
    did = vault.doc_id_for(str(src))
    orphan = vault._global_objects_dir() / ("a" * 16 + ".z")
    orphan.write_bytes(gzip.compress(b"nobody references me"))

    result = vault.collect_garbage(conn, dry_run=False)

    assert result["aborted"] is False
    assert not orphan.exists(), "无人引用的压缩件应被回收"
    for h in vault.manifest_for(did, vid)["parts"].values():
        assert vault._object_path(did, h).is_file(), h
    out = tmp_path / "restored.pptx"
    assert vault.rebuild_to(did, vid, str(out))


def test_pack_output_is_reproducible_and_gzip_readable():
    """同样的输入永远得到同样的字节——内容寻址的存储里，可复现比省几字节重要。"""
    payload = b"<p:sld>" + "空频 分析".encode() * 300 + b"</p:sld>"
    blob = vault._pack(payload)
    assert blob is not None
    assert blob == vault._pack(payload)
    assert len(blob) < len(payload) * vault._COMPRESS_MIN_GAIN
    assert gzip.GzipFile(fileobj=io.BytesIO(blob)).read() == payload


def test_pack_declines_when_the_gain_is_too_small():
    """压不动就别压：多一层解压只会让恢复变慢。"""
    assert vault._pack(os.urandom(8192)) is None    # 高熵数据压完只会更大
    assert vault._pack(b"") is None


def test_text_part_is_written_to_disk_exactly_once(tmp_path, monkeypatch):
    """文本零件只能落一次盘。

    先写原始件再写压缩件的话，一份稿子几百个 xml/rels 就等于把留底的写盘次数翻倍——
    真稿实测 12 份稿会从 12.6 s 涨到 16.4 s。这条用例把「只写一次」钉死。
    """
    writes: list[str] = []
    real = vault._write_object_atomic

    def spy(objects, filename, data):
        writes.append(filename)
        return real(objects, filename, data)

    monkeypatch.setattr(vault, "_write_object_atomic", spy)
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "只写一次"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(p))
    did = vault.doc_id_for(str(p))

    text_parts = [
        n for n in vault.manifest_for(did, vid)["names"]
        if n.casefold().endswith((".xml", ".rels"))
    ]
    assert text_parts, "这份稿子里应该有文本零件"
    assert len(writes) == len(set(writes)), f"同一个零件被写了两次: {writes}"
    assert len(writes) <= len(vault.manifest_for(did, vid)["parts"])
