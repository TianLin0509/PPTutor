"""第二批高 ROI 修复的回归测试。

这些测试刻意覆盖生产审计暴露的失效模式：永久坏文件重试风暴、重解析失败
清空旧索引、启动后的版本补拍延迟，以及版本对象跨文档重复和配额孤儿。
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import fixtures_gen as fx

from pptx_finder import db, indexer, search
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager


def _ok_result(path, text: str = "恢复后的新内容") -> dict:
    st = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "ext": path.suffix.lower(),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "content_hash": "sha256:" + "a" * 64,
        "status": "ok",
        "error": "",
        "page_count": 1,
        "pages": [(1, text, text)],
    }


def test_retry_schema_migrates_in_place_without_clearing_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE files(
             id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
             name TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL,
             mtime REAL NOT NULL, content_hash TEXT, page_count INTEGER DEFAULT 0,
             status TEXT DEFAULT 'ok', error TEXT DEFAULT '', indexed_at REAL DEFAULT 0
           )"""
    )
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES('index_version', ?)", (db.INDEX_VERSION,))
    conn.execute(
        "INSERT INTO files(path,name,ext,size,mtime,status) VALUES(?,?,?,?,?,?)",
        ("C:\\docs\\keep.pdf", "keep.pdf", ".pdf", 1, 1.0, "ok"),
    )
    conn.commit()
    conn.close()

    migrated = db.connect(path)
    db.init_db(migrated)

    cols = {r["name"] for r in migrated.execute("PRAGMA table_info(files)")}
    assert {"parse_failures", "retry_after"} <= cols
    assert migrated.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_parse_error_is_backed_off_but_file_change_bypasses_backoff(tmp_path, monkeypatch):
    p = tmp_path / "permanently-broken.pdf"
    p.write_bytes(b"broken-v1")
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    indexer._mark_index_failure(conn, p, OSError("broken stream"))
    conn.commit()

    calls: list[str] = []

    def parsed(path: str):
        calls.append(path)
        return _ok_result(p)

    monkeypatch.setattr(indexer, "_index_one", parsed)

    # 同一轮/下一次后台对账不能立刻再次打开同一个永久坏文件。
    indexer.update_index(conn, [], scan_iter=iter([p]), workers=1)
    assert calls == []
    row = db.get_file_by_path(conn, str(p))
    assert row["parse_failures"] == 1
    assert row["retry_after"] > row["indexed_at"]

    # 文件真的变化时不受退避影响，立刻给它自愈机会。
    p.write_bytes(b"hydrated-and-changed-v2")
    future = max(p.stat().st_mtime + 2, row["mtime"] + 2)
    os.utime(p, (future, future))
    indexer.update_index(conn, [], scan_iter=iter([p]), workers=1)
    assert calls == [str(p)]
    healed = db.get_file_by_path(conn, str(p))
    assert healed["status"] == "ok"
    assert healed["parse_failures"] == 0
    assert healed["retry_after"] == 0


def test_parse_error_circuit_opens_after_three_unchanged_failures(tmp_path, monkeypatch):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"permanent corruption")
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    for _ in range(indexer.MAX_UNCHANGED_PARSE_FAILURES):
        indexer._mark_index_failure(conn, p, OSError("broken"))
    conn.commit()
    monkeypatch.setattr(
        indexer,
        "_index_one",
        lambda _path: (_ for _ in ()).throw(AssertionError("open circuit must not parse")),
    )

    indexer.update_index(conn, [], scan_iter=iter([p]), workers=1)

    row = db.get_file_by_path(conn, str(p))
    assert row["status"] == "error"
    assert row["parse_failures"] == indexer.MAX_UNCHANGED_PARSE_FAILURES


def test_parse_error_preserves_last_known_good_search_content(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(b"new broken bytes")
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    fid = db.upsert_file(
        conn,
        path=str(p),
        name=p.name,
        ext=".pdf",
        size=10,
        mtime=1.0,
        content_hash="sha256:" + "1" * 64,
        page_count=1,
        status="ok",
        error="",
        indexed_at=1.0,
    )
    db.replace_pages(conn, fid, [(1, "上次成功内容 麒麟芯片", "上 次 成 功 内 容 麒 麟 芯 片")])
    conn.commit()

    st = p.stat()
    indexer._write_result(
        conn,
        {
            "path": str(p),
            "name": p.name,
            "ext": ".pdf",
            "size": st.st_size,
            "mtime": st.st_mtime,
            "content_hash": "sha256:" + "2" * 64,
            "status": "error",
            "error": "PdfStreamError: truncated",
            "page_count": 0,
            "pages": [],
        },
    )
    conn.commit()

    hits = search.search(conn, "麒麟芯片", exts=(".pdf",))
    assert hits and hits[0].path == str(p)
    assert hits[0].status == "error"
    row = db.get_file_by_path(conn, str(p))
    assert row["page_count"] == 1
    assert row["content_hash"] == "sha256:" + "1" * 64


def test_version_reconcile_runs_immediately_before_first_interval(tmp_path):
    mgr = VersionManager(store.connect(tmp_path / "versions.db"))
    events: list[str] = []

    class StopAfterFirstWait:
        def wait(self, _seconds: float) -> bool:
            events.append("wait")
            return True

    mgr._reconcile_stop = StopAfterFirstWait()
    mgr.reconcile_known_docs = lambda: events.append("reconcile") or 0

    mgr._reconcile_loop()

    assert events == ["reconcile", "wait"]


def test_global_object_pool_deduplicates_identical_parts_across_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    p1 = tmp_path / "first.pptx"
    p2 = tmp_path / "second.pptx"
    fx.make_pptx(p1, [{"body": "两份文件共享的内容"}])
    shutil.copy2(p1, p2)
    conn = store.connect(vault.db_path())
    store.init_db(conn)

    v1 = vault.snapshot(conn, str(p1))
    v2 = vault.snapshot(conn, str(p2))
    d1, d2 = vault.doc_id_for(str(p1)), vault.doc_id_for(str(p2))
    hashes = set(vault.manifest_for(d1, v1)["parts"].values())
    hashes.update(vault.manifest_for(d2, v2)["parts"].values())

    global_objects = {p.name for p in vault._global_objects_dir().iterdir() if p.is_file()}
    assert global_objects == hashes
    assert not list(vault._objects_dir(d1).iterdir())
    assert not list(vault._objects_dir(d2).iterdir())
    restored = tmp_path / "restored.pptx"
    assert vault.rebuild_to(d2, v2, str(restored))


def test_legacy_object_migration_is_crash_safe_and_reclaims_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    payload = b"shared legacy object"
    h = vault.xxhash.xxh64(payload).hexdigest()
    for did in ("doc-a", "doc-b"):
        (vault._objects_dir(did) / h).write_bytes(payload)

    result = vault.migrate_legacy_objects()

    assert result["errors"] == 0
    assert result["migrated"] == 1
    assert result["duplicates"] == 1
    assert result["bytes_reclaimed"] >= len(payload)
    assert (vault._global_objects_dir() / h).read_bytes() == payload
    assert not (vault._objects_dir("doc-a") / h).exists()
    assert not (vault._objects_dir("doc-b") / h).exists()


def test_quota_removes_manifest_and_gc_deletes_only_unreferenced_objects(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr("pptx_finder.versioning.manager.KEEP_PER_DOC", 1)
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "v1 old"}])
    mgr = VersionManager()
    v1 = mgr.snapshot_now(str(p))
    d = vault.doc_id_for(str(p))
    assert vault._manifest_path(d, v1).exists()

    fx.make_pptx(p, [{"body": "v2 current changed"}])
    v2 = mgr.snapshot_now(str(p))
    assert v2 and v2 != v1
    assert not vault._manifest_path(d, v1).exists()
    assert vault._manifest_path(d, v2).exists()

    orphan = vault._global_objects_dir() / ("f" * 16)
    orphan.write_bytes(b"orphan")
    orphan_manifest = vault._doc_dir(d) / "versions" / "not-in-db.json"
    orphan_manifest.write_text("{}", encoding="utf-8")
    result = vault.collect_garbage(mgr._conn, dry_run=False)

    assert result["aborted"] is False
    assert result["objects_removed"] >= 1
    assert result["manifests_removed"] >= 1
    assert not orphan.exists()
    out = tmp_path / "current.pptx"
    assert vault.rebuild_to(d, v2, str(out))


def test_gc_aborts_without_deleting_orphans_when_live_recovery_graph_is_broken(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "must remain recoverable"}])
    conn = store.connect(vault.db_path())
    store.init_db(conn)
    vid = vault.snapshot(conn, str(p))
    did = vault.doc_id_for(str(p))
    referenced_hash = next(iter(vault.manifest_for(did, vid)["parts"].values()))
    (vault._global_objects_dir() / referenced_hash).unlink()
    orphan = vault._global_objects_dir() / ("e" * 16)
    orphan.write_bytes(b"do not touch during aborted pass")

    result = vault.collect_garbage(conn, dry_run=False)

    assert result["aborted"] is True
    assert result["errors"] >= 1
    assert orphan.exists()


def test_quota_never_deletes_a_version_used_as_another_docs_branch_base(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr("pptx_finder.versioning.manager.KEEP_PER_DOC", 1)
    source = tmp_path / "source.pptx"
    copied = tmp_path / "copied.pptx"
    fx.make_pptx(source, [{"body": "shared branch base"}])
    mgr = VersionManager()
    base_version = mgr.snapshot_now(str(source))
    shutil.copy2(source, copied)
    assert mgr.snapshot_now(str(copied)) is None  # 建分支，但相同内容无需重复快照
    child_id = vault.doc_id_for(str(copied))
    branch = store.get_branch(mgr._conn, child_id)
    assert branch and branch["branched_from_version_id"] == base_version

    fx.make_pptx(source, [{"body": "source advances beyond quota"}])
    assert mgr.snapshot_now(str(source))

    assert store.get_version(mgr._conn, base_version) is not None
    assert vault._manifest_path(vault.doc_id_for(str(source)), base_version).exists()
