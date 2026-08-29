"""v1.2.9：「任意文件名」的目录级实时对账 + 默认值回退 + 版本库迁移字节校验。

watcher 只对 PPT / Word / PDF 做实时索引，非内容扩展名此前完全没有实时通道——
新建与删除要等下一次完整扫描（最坏一周）才反映到搜索结果里。本组用例锁住新的
「watcher 上报目录 → 后台按目录 scandir 对账」通道，以及它必须守住的边界：
内容行绝不被盘点降级、排除目录不进对账、churn 目录只合并成一次。
"""
from __future__ import annotations

import os

import fixtures_gen as fx

from pptx_finder import config, db, indexer, namestore, search
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.watcher import _Handler

CONTENT_EXTS = (".pptx", ".ppt")  # 与「文档搜索关闭」时的生产口径一致


def _conn(tmp_path, name="i.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


# ---------- watcher：非内容扩展名只上报目录，内容扩展名照旧走实时索引 ----------

def test_watcher_reports_directory_for_non_content_files(tmp_path):
    saved, content, dirs = [], [], []
    handler = _Handler(
        saved.append,
        on_content_saved=content.append,
        roots=[str(tmp_path)],  # tmp_path 在 %TEMP% 下：声明为显式根才不被剪枝
        allowed_exts=(".pptx", ".ppt"),
        on_other_dir_changed=dirs.append,
    )

    handler._trigger(str(tmp_path / "photo.jpg"))
    handler._trigger(str(tmp_path / "archive.zip"))
    handler._trigger(str(tmp_path / "deck.pptx"))

    # 非内容扩展名：只上报目录，不排防抖定时器（_timers 里不该有它们）
    assert dirs == [str(tmp_path), str(tmp_path)]
    assert str(tmp_path / "photo.jpg") not in handler._timers
    # 内容扩展名：照旧进防抖队列，不上报目录
    assert str(tmp_path / "deck.pptx") in handler._timers


def test_watcher_churn_collapses_to_one_directory(tmp_path):
    """同一目录 churn 一千次，消费侧只需要处理一个目录名（成本与事件数无关）。"""
    seen: set[str] = set()
    handler = _Handler(
        lambda _p: None,
        roots=[str(tmp_path)],
        allowed_exts=(".pptx",),
        on_other_dir_changed=seen.add,
    )
    for i in range(1000):
        handler._trigger(str(tmp_path / f"cache_{i}.tmp"))
    assert seen == {str(tmp_path)}


def test_watcher_reports_directory_on_delete(tmp_path):
    dirs = []
    handler = _Handler(
        lambda _p: None,
        roots=[str(tmp_path)],
        allowed_exts=(".pptx",),
        on_other_dir_changed=dirs.append,
    )

    class _Evt:
        is_directory = False
        src_path = str(tmp_path / "gone.png")

    handler.on_deleted(_Evt())
    assert dirs == [str(tmp_path)]


def test_watcher_other_dir_callback_never_escapes(tmp_path):
    """盘点是尽力而为：回调抛异常绝不能把 watcher 线程带崩。"""
    def _boom(_d):
        raise RuntimeError("boom")

    handler = _Handler(
        lambda _p: None, roots=[str(tmp_path)], allowed_exts=(".pptx",),
        on_other_dir_changed=_boom)
    handler._trigger(str(tmp_path / "x.bin"))  # 不抛即通过


# ---------- indexer：目录级对账 ----------

def test_reconcile_dir_adds_and_removes_inventory_rows(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.zzz").write_text("x", encoding="utf-8")
    (docs / "b.png").write_bytes(b"img")
    conn = _conn(tmp_path)

    res = indexer.reconcile_inventory_dir(
        conn, str(docs), allowed_exts=CONTENT_EXTS)
    assert res["added"] == 2
    assert {r[0] for r in conn.execute("SELECT name FROM files")} == {"a.zzz", "b.png"}
    assert [r.name for r in search.search(conn, "a", exts=None)] == ["a.zzz"]

    (docs / "b.png").unlink()
    (docs / "c.log").write_text("y", encoding="utf-8")
    res = indexer.reconcile_inventory_dir(
        conn, str(docs), allowed_exts=CONTENT_EXTS)
    assert res["added"] == 1 and res["removed"] == 1
    assert {r[0] for r in conn.execute("SELECT name FROM files")} == {"a.zzz", "c.log"}
    # 删掉的行连文件名 FTS 一起走，不留幽灵
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == 2
    conn.close()


def test_reconcile_dir_is_not_recursive(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "top.zzz").write_text("x", encoding="utf-8")
    (docs / "sub" / "deep.zzz").write_text("x", encoding="utf-8")
    conn = _conn(tmp_path)

    indexer.reconcile_inventory_dir(conn, str(docs), allowed_exts=CONTENT_EXTS)

    names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert names == {"top.zzz"}  # 子目录由它自己的目录事件负责
    conn.close()


def test_reconcile_dir_never_touches_content_rows(tmp_path):
    """PPT / Word / PDF 的内容行由既有实时通道负责，对账绝不能降级或删除它们。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "deck.pptx", [{"body": "算力 ALPHA"}])
    conn = _conn(tmp_path)
    indexer.update_index(conn, [str(docs)], workers=1, supported_exts=CONTENT_EXTS)
    before = dict(conn.execute(
        "SELECT status, page_count FROM files WHERE name='deck.pptx'").fetchone())

    # 源文件被删掉也不归盘点对账管（内容通道有自己的删除语义）
    indexer.reconcile_inventory_dir(conn, str(docs), allowed_exts=CONTENT_EXTS)
    row = conn.execute(
        "SELECT status, page_count FROM files WHERE name='deck.pptx'").fetchone()
    assert dict(row) == before
    assert [r.name for r in search.search(conn, "算力")] == ["deck.pptx"]
    conn.close()


def test_reconcile_dir_skips_excluded_directories(tmp_path):
    for name in ("node_modules", ".git", "__pycache__"):
        d = tmp_path / name
        d.mkdir()
        (d / "junk.zzz").write_text("x", encoding="utf-8")
    conn = _conn(tmp_path)
    for name in ("node_modules", ".git", "__pycache__"):
        res = indexer.reconcile_inventory_dir(
            conn, str(tmp_path / name), allowed_exts=CONTENT_EXTS)
        assert res["skipped"] == 1, name
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    conn.close()


def test_reconcile_dir_missing_directory_is_safe(tmp_path):
    conn = _conn(tmp_path)
    res = indexer.reconcile_inventory_dir(
        conn, str(tmp_path / "nope"), allowed_exts=CONTENT_EXTS)
    assert res["skipped"] == 1 and res["removed"] == 0
    conn.close()


def test_reconcile_dir_skips_unchanged_rows(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.zzz").write_text("x", encoding="utf-8")
    conn = _conn(tmp_path)
    assert indexer.reconcile_inventory_dir(
        conn, str(docs), allowed_exts=CONTENT_EXTS)["added"] == 1
    # 第二轮无变化：不重复写库、也不重复插 FTS 行
    assert indexer.reconcile_inventory_dir(
        conn, str(docs), allowed_exts=CONTENT_EXTS)["added"] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == 1
    conn.close()


# ---------- 默认值：全文件盘点常开；关闭路径仍不该触发全盘重扫 ----------

def test_index_all_files_defaults_on():
    """2026-08-28 改为常开：开关撤掉了，能力必须默认就在。"""
    assert config.DEFAULT_INDEX_ALL_FILES is True
    assert config.get_index_all_files() is True


def test_feature_signature_any_file_downgrade_needs_no_rescan():
    on = "documents=0;smart_grouping=0;any_file=1"
    off = "documents=0;smart_grouping=0;any_file=0"
    assert config.feature_signature_needs_rescan(on, off) is False
    assert config.feature_signature_needs_rescan(on, on) is False
    # 开启盘点、或其它能力变化，仍然必须重扫
    assert config.feature_signature_needs_rescan(off, on) is True
    assert config.feature_signature_needs_rescan(
        on, "documents=1;smart_grouping=0;any_file=0") is True
    assert config.feature_signature_needs_rescan(
        "documents=0;smart_grouping=0", "documents=1;smart_grouping=0") is True


# ---------- 版本库迁移：字节校验 ----------

def _seed_vault(root):
    root.mkdir(parents=True, exist_ok=True)
    conn = store.connect(root / "versions.db")
    store.init_db(conn)
    store.upsert_doc(conn, "doc", str(root / "deck.pptx"), 1.0)
    conn.commit()
    conn.close()
    objs = root / "_objects"
    objs.mkdir(exist_ok=True)
    (objs / "abc123").write_bytes(b"P" * 4096)
    return root


def test_migrate_vault_verifies_bytes_and_keeps_source_on_truncation(tmp_path, monkeypatch):
    """下一句就是 rmtree(src)：内容被截断时必须回滚目标、保住源。"""
    src = _seed_vault(tmp_path / "src")
    dst = tmp_path / "dst"
    real_copy = vault.shutil.copy2

    def _truncating_copy(a, b, *args, **kwargs):
        real_copy(a, b, *args, **kwargs)
        os.truncate(b, 10)  # 模拟目标盘写满 / 网络盘截断：个数对、内容短
        return b

    monkeypatch.setattr(vault.shutil, "copy2", _truncating_copy)
    try:
        vault.migrate_vault_dir(src, dst)
    except RuntimeError as exc:
        assert "不完整" in str(exc)
    else:
        raise AssertionError("截断的迁移必须失败")
    assert src.is_dir() and (src / "_objects" / "abc123").stat().st_size == 4096
    assert not dst.exists()


def test_migrate_vault_succeeds_on_clean_copy(tmp_path):
    src = _seed_vault(tmp_path / "src")
    dst = tmp_path / "dst"
    res = vault.migrate_vault_dir(src, dst)
    assert res["files"] >= 2
    assert (dst / "_objects" / "abc123").stat().st_size == 4096
    assert (dst / "versions.db").is_file()
    assert not src.exists()


# ---------- 版本库体检快照 ----------

def test_vault_health_snapshot_unavailable_without_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_vault_dir_no_create", lambda: tmp_path / "nope")
    snap = vault.vault_health_snapshot()
    assert snap["available"] is False and snap["vault_bytes"] == 0


def test_vault_health_snapshot_counts_ghost_docs(tmp_path, monkeypatch):
    root = _seed_vault(tmp_path / "vault")
    monkeypatch.setattr(vault, "_vault_dir_no_create", lambda: root)
    monkeypatch.setattr(vault, "_fixed_drive_roots", lambda: [])
    snap = vault.vault_health_snapshot()
    assert snap["available"] is True
    assert snap["docs"] == 1
    assert snap["ghost_docs"] == 1  # deck.pptx 从来没被创建过 → 源文件不存在
    assert snap["vault_bytes"] >= 4096


# ---------- 重维护：幽灵探测复用（锁外探一次，锁内两处共用） ----------

def _vault_with_ghost(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vault, "_vault_dir_no_create", lambda: root)
    monkeypatch.setattr(vault, "vault_dir", lambda: root)
    monkeypatch.setattr(vault, "_fixed_drive_roots", lambda: [])
    conn = store.connect(root / "versions.db")
    store.init_db(conn)
    return root, conn


def test_reap_reuses_probe_and_rechecks_before_deleting(tmp_path, monkeypatch):
    """探测在锁外做、删除在锁内做：中间文件被恢复时，必须复核后放弃删除。"""
    root, conn = _vault_with_ghost(tmp_path, monkeypatch)
    target = tmp_path / "back.pptx"
    store.upsert_doc(conn, "doc", str(target), 1.0)
    store.set_status(conn, "doc", "deleted")
    conn.execute("UPDATE managed_docs SET deleted_at=? WHERE doc_id='doc'", (1.0,))
    conn.commit()

    probe = vault.list_ghost_docs(conn)
    assert [g["doc_id"] for g in probe] == ["doc"]

    target.write_bytes(b"restored")  # 探测之后、收割之前，源文件回来了
    res = vault.reap_ghost_docs(conn, dry_run=False, ghosts=probe)

    assert res["ghost_docs"] == 0
    assert store.get_doc(conn, "doc") is not None  # 没被误删
    conn.close()


def test_mark_ghost_docs_seen_accepts_precomputed_probe(tmp_path, monkeypatch):
    root, conn = _vault_with_ghost(tmp_path, monkeypatch)
    store.upsert_doc(conn, "doc", str(tmp_path / "gone.pptx"), 1.0)
    conn.commit()

    probe = vault.list_ghost_docs(conn)
    assert vault.mark_ghost_docs_seen(conn, ghosts=probe) == 1
    assert float(store.get_doc(conn, "doc")["deleted_at"]) > 0
    # 已确认过缺失的不重置计时
    assert vault.mark_ghost_docs_seen(conn, ghosts=vault.list_ghost_docs(conn)) == 0
    conn.close()


def test_enforce_size_budget_accepts_measured_bytes(tmp_path, monkeypatch):
    """体积测量放锁外：传进来的口径必须被采信，不再自己重新遍历一遍目录。"""
    root, conn = _vault_with_ghost(tmp_path, monkeypatch)
    calls = []
    real = vault._budget_relevant_bytes
    monkeypatch.setattr(
        vault, "_budget_relevant_bytes",
        lambda: (calls.append(1), real())[1])

    res = vault.enforce_size_budget(conn, max_bytes=10 * 1024 * 1024, measured_bytes=123)
    assert res["vault_bytes_before"] == 123
    assert calls == []  # 没超预算 → 一次目录遍历都不做
    conn.close()


# ---------- 主窗接线：目录入队 → 后台对账 → 结果可搜（bug 最爱藏在接线处） ----------

def test_main_window_dirty_dir_queue_and_reconcile(qtbot, tmp_path, monkeypatch):
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.zzz").write_text("x", encoding="utf-8")
    conn = _conn(tmp_path, "ui.db")
    win = MainWindow(conn=conn, do_index=False, index_all_files_enabled=True)
    qtbot.addWidget(win)

    # watcher 侧只会丢一个目录名进来；churn 一千次也只应留下一个
    for _ in range(1000):
        win.note_inventory_dir_dirty(str(docs))
    assert win._dirty_inventory_dirs == {str(docs)}

    # 直接验同步对账语义：结果进的是平铺增量层，不再写 SQLite
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    res = MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    assert res.get("error") is None
    assert res["added"] == 1 and res["dirs"] == 1
    stores = [s for s in (namestore.open_store(k) for k in namestore.KINDS) if s]
    try:
        assert [r.name for r in search.search_names(stores, "note")] == ["note.zzz"]
    finally:
        for s in stores:
            s.close()
    # 盘点行一行都不该进 SQLite——那正是被换掉的 1.19 GB
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE lower(ext)='.zzz'").fetchone()[0] == 0

    # 开关关掉后：不再入队，脏目录清空，定时器停
    win.apply_feature_flags(index_all_files_enabled=False)
    win.note_inventory_dir_dirty(str(docs))
    assert win._dirty_inventory_dirs == set()
    assert not win._inventory_reconcile_timer.isActive()
    conn.close()


def test_main_window_dirty_dir_queue_is_bounded(qtbot, tmp_path, monkeypatch):
    """一次解压大包能变动上万个目录：集合必须有上限，不能无界堆内存。"""
    from pptx_finder.ui import theme
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "apply_to_app", lambda *a, **k: None)
    conn = _conn(tmp_path, "ui2.db")
    win = MainWindow(conn=conn, do_index=False, index_all_files_enabled=True)
    qtbot.addWidget(win)
    for i in range(MainWindow._INVENTORY_DIRS_MAX + 500):
        win.note_inventory_dir_dirty(str(tmp_path / f"d{i}"))
    assert len(win._dirty_inventory_dirs) == MainWindow._INVENTORY_DIRS_MAX
    assert win._inventory_dirs_overflowed is True
    conn.close()
