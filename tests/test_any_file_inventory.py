"""任意文件名（Everything 式）搜索：全盘文件名盘点、模式接线、开关清理。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel

import fixtures_gen as fx

from pptx_finder import config, db, indexer, namestore, search
from pptx_finder.models import FileResult
from pptx_finder.scanner import iter_ppt_files
from pptx_finder.ui import search_worker as search_worker_mod
from pptx_finder.ui import theme
from pptx_finder.ui.main_window import ALL_FILES_SCOPE_LABEL, MainWindow, ResultItem
from pptx_finder.ui.search_worker import SearchWorker


def _build_docs(tmp_path):
    """混合扩展名目录：pptx（解析）/ ppt（文件名登记）/ zzz、docx（盘点登记）。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "a.pptx", [{"body": "算力 ALPHA"}])
    (docs / "b.ppt").write_bytes(b"old ppt")
    (docs / "report.zzz").write_text("hello", encoding="utf-8")
    (docs / "notes.docx").write_bytes(b"fake docx")
    (docs / "~$lock.pptx").write_bytes(b"lock")
    (docs / "node_modules").mkdir()
    (docs / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    return docs


def _conn(tmp_path, name="i.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


# ---------- scanner：inventory_all 产出全部文件，剪枝规则不变 ----------

def test_scanner_inventory_all_yields_everything_and_prunes(tmp_path):
    docs = _build_docs(tmp_path)

    names = sorted(p.name for p in iter_ppt_files([str(docs)], inventory_all=True))
    assert names == ["a.pptx", "b.ppt", "notes.docx", "report.zzz"]

    default_names = sorted(p.name for p in iter_ppt_files([str(docs)]))
    # 默认模式行为不变：SUPPORTED_EXTS（pptx/ppt/docx/pdf），~$ 与排除目录仍被剪枝
    assert default_names == ["a.pptx", "b.ppt", "notes.docx"]


# ---------- indexer：盘点登记 / 增量 / 删除通道 ----------

def test_inventory_registers_filename_only_and_searches_by_name(tmp_path):
    docs = _build_docs(tmp_path)
    conn = _conn(tmp_path)
    summary = indexer.update_index(
        conn, [str(docs)], workers=1,
        supported_exts=(".pptx", ".ppt"), index_all_files=True,
    )
    assert summary["indexed"] == 1
    assert summary["skipped_ppt"] == 1
    assert summary["filename_only"] == 2  # report.zzz + notes.docx
    rows = {
        r["name"]: r["status"]
        for r in conn.execute("SELECT name, status FROM files")
    }
    assert rows == {
        "a.pptx": "ok",
        "b.ppt": "filename_only",
        "notes.docx": "filename_only",
        "report.zzz": "filename_only",
    }
    hits = search.search(conn, "report", exts=None)
    assert [(r.name, r.name_hit) for r in hits] == [("report.zzz", True)]
    # 内容统计口径不受盘点行影响
    assert db.stats(conn, exts=(".pptx", ".ppt"))["file_count"] == 2
    conn.close()


def test_inventory_incremental_skip_and_delete_channel(tmp_path):
    docs = _build_docs(tmp_path)
    conn = _conn(tmp_path)
    kwargs = dict(
        workers=1, supported_exts=(".pptx", ".ppt"), index_all_files=True,
    )
    indexer.update_index(conn, [str(docs)], **kwargs)
    again = indexer.update_index(conn, [str(docs)], **kwargs)
    assert again["filename_only"] == 0  # (size, mtime) 快筛：未变不重写

    (docs / "report.zzz").unlink()
    after = indexer.update_index(conn, [str(docs)], **kwargs)
    assert after["deleted"] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert "report.zzz" not in names
    conn.close()


def test_inventory_batch_over_commit_threshold(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    total = indexer.SCAN_COMMIT_EVERY + 30
    for i in range(total):
        (docs / f"alpha part {i:04d}.dat").write_text("x", encoding="utf-8")
    conn = _conn(tmp_path)
    summary = indexer.update_index(
        conn, [str(docs)], workers=1,
        supported_exts=(".pptx", ".ppt"), index_all_files=True,
    )
    assert summary["filename_only"] == total
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == total
    hits = search.search(conn, "alpha", exts=None, limit=total)
    assert len(hits) == total
    conn.close()


def test_inventory_off_leaves_rows_and_purge_cleans_them(tmp_path):
    docs = _build_docs(tmp_path)
    conn = _conn(tmp_path)
    indexer.update_index(
        conn, [str(docs)], workers=1,
        supported_exts=(".pptx", ".ppt"), index_all_files=True,
    )
    # 开关关闭后的普通重扫：不收录、也不误删盘点行（扩展名不在删除通道内）
    indexer.update_index(
        conn, [str(docs)], workers=1,
        supported_exts=(".pptx", ".ppt"), index_all_files=False,
    )
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 4

    removed = indexer.purge_non_content_filename_only(conn)
    assert removed == 1  # 仅 report.zzz；.ppt 与 .docx 属内容扩展名，保留
    names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert names == {"a.pptx", "b.ppt", "notes.docx"}
    # 文件名 FTS 同步清理，不再可被搜到
    assert search.search(conn, "report", exts=None) == []
    conn.close()


def test_inventory_docx_upgrades_to_content_when_enabled(tmp_path):
    """盘点期登记的 docx 文件名行，在内容索引开启后必须重走解析流程。"""
    import zipfile

    docs = tmp_path / "docs"
    docs.mkdir()
    with zipfile.ZipFile(docs / "notes.docx", "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>供应商 对账 明细</w:t></w:r></w:p></w:body></w:document>",
        )
    conn = _conn(tmp_path)
    indexer.update_index(
        conn, [str(docs)], workers=1,
        supported_exts=(".pptx", ".ppt"), index_all_files=True,
    )
    row = conn.execute("SELECT status FROM files WHERE name='notes.docx'").fetchone()
    assert row[0] == "filename_only"

    indexer.update_index(
        conn, [str(docs)], workers=1,
        supported_exts=(".pptx", ".ppt", ".docx"), index_all_files=True,
    )
    row = conn.execute(
        "SELECT status, page_count FROM files WHERE name='notes.docx'").fetchone()
    assert row[0] == "ok" and row[1] == 1
    hits = search.search(conn, "对账", exts=(".docx",))
    assert [r.name for r in hits] == ["notes.docx"]
    conn.close()


# ---------- index_single：watcher 增量 ----------

def test_index_single_registers_any_file_as_filename_only(tmp_path):
    docs = _build_docs(tmp_path)
    conn = _conn(tmp_path)
    target = docs / "new.bin"

    assert indexer.index_single(conn, str(target), index_all_files=True) is False  # 不存在且无旧行
    target.write_bytes(b"x")
    assert indexer.index_single(conn, str(target), index_all_files=True) is True
    row = conn.execute("SELECT status FROM files WHERE name='new.bin'").fetchone()
    assert row[0] == "filename_only"

    other = docs / "other.bin2"
    other.write_bytes(b"x")
    assert indexer.index_single(conn, str(other), index_all_files=False) is False
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE name='other.bin2'").fetchone()[0] == 0
    conn.close()


def test_index_single_never_downgrades_existing_content_row(tmp_path):
    """docx 已有内容行时，盘点路径不得把它降级成 filename_only。"""
    docs = _build_docs(tmp_path)
    conn = _conn(tmp_path)
    target = docs / "keep.docx"
    target.write_bytes(b"v1")
    db.upsert_file(
        conn, path=str(target), name=target.name, ext=".docx", size=1, mtime=1.0,
        content_hash="size:1", page_count=3, status="ok", error="", indexed_at=1.0,
    )
    conn.commit()
    ok = indexer.index_single(
        conn, str(target),
        supported_exts=(".pptx", ".ppt"), index_all_files=True,
    )
    assert ok is False
    row = conn.execute(
        "SELECT status, page_count FROM files WHERE name='keep.docx'").fetchone()
    assert row[0] == "ok" and row[1] == 3
    conn.close()


# ---------- search：文件名召回 limit ----------

def test_search_name_limit_relaxes_filename_recall(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        db.upsert_file(
            conn, path=f"C:/inv/alpha report {i}.dat", name=f"alpha report {i}.dat",
            ext=".dat", size=1, mtime=1.0, content_hash="size:1", page_count=0,
            status="filename_only", error="", indexed_at=1.0,
        )
    conn.commit()
    full = search.search(conn, "alpha", exts=None)
    assert len(full) == 5
    capped = search.search(conn, "alpha", exts=None, name_limit=2)
    assert 0 < len(capped) <= 2  # 候选截断生效
    conn.close()


# ---------- SearchWorker：any_filename 走平铺文件名索引 ----------

def test_search_worker_any_filename_never_touches_sqlite(monkeypatch, qtbot, tmp_path):
    """「全部文件」范围整段绕开 SQLite——那些文件的名字根本不在库里了。

    顺带的好处：这个范围的搜索不再与后台建库抢数据库锁。
    """
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    conn = _conn(tmp_path)

    def boom(*_a, **_kw):
        raise AssertionError("any_filename 不该再走 SQLite 搜索")

    monkeypatch.setattr(search_worker_mod.search_mod, "search", boom)

    real = tmp_path / "report.zzz"
    real.write_text("x", encoding="utf-8")
    b = namestore.NameStoreBuilder()
    b.add(str(real), 10, 1_700_000_000)
    b.write()

    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(1, "report", "any_filename")
        _req, _q, results, _ms, error = blocker.args
    finally:
        worker.stop()
        worker.wait(3000)
    assert error is None
    assert [r.name for r in results] == ["report.zzz"]
    conn.close()


def test_search_worker_any_filename_degrades_when_index_missing(qtbot, tmp_path, monkeypatch):
    """索引还没建好时这个范围空手而归——但绝不能报错，更不能影响 PPT 搜索。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    conn = _conn(tmp_path)
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(1, "report", "any_filename")
        _req, _q, results, _ms, error = blocker.args
    finally:
        worker.stop()
        worker.wait(3000)
    assert error is None
    assert results == []
    conn.close()


def test_search_worker_picks_up_a_rebuilt_index(qtbot, tmp_path, monkeypatch):
    """建库重写索引之后，下一次搜索必须看到新内容，而不是一直用旧 mmap。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    conn = _conn(tmp_path)
    before = tmp_path / "before.zzz"
    before.write_text("x", encoding="utf-8")
    b = namestore.NameStoreBuilder()
    b.add(str(before), 10, 1_700_000_000)
    b.write()

    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(1, "zzz", "any_filename")
        assert [r.name for r in blocker.args[2]] == ["before.zzz"]

        after = tmp_path / "after.zzz"
        after.write_text("x", encoding="utf-8")
        b2 = namestore.NameStoreBuilder()
        b2.add(str(after), 10, 1_700_000_500)
        b2.write()

        with qtbot.waitSignal(worker.searched, timeout=3000) as blocker:
            worker.request(2, "zzz", "any_filename")
        assert [r.name for r in blocker.args[2]] == ["after.zzz"]
    finally:
        worker.stop()
        worker.wait(3000)
    conn.close()


def test_ppt_content_search_is_unaffected_by_the_new_engine(qtbot, tmp_path, monkeypatch):
    """硬约束：PPT 相关的一切行为不受影响。内容搜索照旧走 SQLite。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = _build_docs(tmp_path)
    conn = _conn(tmp_path)
    indexer.update_index(
        conn, [str(docs)], workers=1, supported_exts=(".pptx", ".ppt"))
    worker = SearchWorker(conn=conn)
    worker.start()
    try:
        with qtbot.waitSignal(worker.searched, timeout=5000) as blocker:
            worker.request(1, "算力", "all")
        _req, _q, results, _ms, error = blocker.args
    finally:
        worker.stop()
        worker.wait(3000)
    assert error is None
    assert [r.name for r in results] == ["a.pptx"]
    assert results[0].hits          # 内容命中还在
    assert results[0].file_id > 0   # 仍是真实的库内 id
    conn.close()


# ---------- UI：模式接线 / badge / 开关清理 ----------

class _StubRender(QObject):
    rendered = Signal(int, str)

    def request(self, req_id, path, page_no, cache_key=None):
        self.rendered.emit(req_id, "")


def _win(qtbot, tmp_path, *, index_all_files_enabled):
    conn = _conn(tmp_path, "ui.db")
    win = MainWindow(
        conn=conn,
        render_worker=_StubRender(),
        do_index=False,
        index_all_files_enabled=index_all_files_enabled,
    )
    qtbot.addWidget(win)
    return win


def test_all_files_scope_is_always_offered(qtbot, tmp_path):
    """Everything 档不再挂在任何开关下——搜索范围里永远有它。"""
    win = _win(qtbot, tmp_path, index_all_files_enabled=True)
    items = [win.type_filter.itemText(i) for i in range(win.type_filter.count())]
    assert items == ["PPT", ALL_FILES_SCOPE_LABEL]

    win.apply_feature_flags(document_search_enabled=True)
    items = [win.type_filter.itemText(i) for i in range(win.type_filter.count())]
    assert items == ["PPT", "Word", "PDF", "全部文档", ALL_FILES_SCOPE_LABEL]
    assert items[-1] == ALL_FILES_SCOPE_LABEL, "全部文件必须在最后，不抢 PPT 的默认位"


def test_all_files_scope_wiring(qtbot, tmp_path):
    win = _win(qtbot, tmp_path, index_all_files_enabled=True)
    assert win.type_filter.currentText() == "PPT"      # 默认仍是 PPT
    assert win._mode_key() == "all"

    win.type_filter.setCurrentText(ALL_FILES_SCOPE_LABEL)
    assert win._mode_key() == "any_filename"
    assert win._search_exts() is None                  # 不按扩展名过滤
    # 这个范围只登记了文件名，给「仅内容」这种选择只会误导
    assert win.mode.isEnabled() is False

    win.type_filter.setCurrentText("PPT")
    assert win.mode.isEnabled() is True
    win.mode.setCurrentText("仅内容")
    assert win._mode_key() == "content"
    assert win._search_exts() == (".pptx", ".ppt")


def test_all_files_scope_overrides_a_stale_mode_selection(qtbot, tmp_path):
    """先选了「仅内容」再切到全部文件：口径必须以范围为准，不能去搜不存在的内容。"""
    win = _win(qtbot, tmp_path, index_all_files_enabled=True)
    win.mode.setCurrentText("仅内容")
    win.type_filter.setCurrentText(ALL_FILES_SCOPE_LABEL)
    assert win._mode_key() == "any_filename"


def test_apply_feature_flags_toggle_off_still_purges(qtbot, tmp_path):
    """关闭路径已经不在界面上了，但代码还活着：传 False 仍要清干净盘点行。"""
    win = _win(qtbot, tmp_path, index_all_files_enabled=True)
    db.upsert_file(
        win._conn, path="C:/inv/report.zzz", name="report.zzz", ext=".zzz",
        size=1, mtime=1.0, content_hash="size:1", page_count=0,
        status="filename_only", error="", indexed_at=1.0,
    )
    win._conn.commit()

    win.apply_feature_flags(index_all_files_enabled=False)

    qtbot.waitUntil(lambda: not win._bg_tasks, timeout=5000)
    row = win._conn.execute("SELECT id FROM files WHERE ext='.zzz'").fetchone()
    assert row is None


def test_filename_only_badge_uses_real_ext(qtbot):
    r = FileResult(
        file_id=1, path="C:/a/report.zzz", name="report.zzz", ext=".zzz",
        mtime=0, size=1, page_count=0, status="filename_only", score=1,
        name_hit=True, hits=[],
    )
    it = ResultItem(r, theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(it)
    texts = [lb.text() for lb in it.findChildren(QLabel)]
    assert ".zzz" in texts
    assert ".ppt" not in texts
    assert "仅文件名收录 · 未解析内容" in texts


def test_filename_only_badge_keeps_ppt_wording(qtbot):
    r = FileResult(
        file_id=1, path="C:/a/old.ppt", name="old.ppt", ext=".ppt",
        mtime=0, size=1, page_count=0, status="filename_only", score=1,
        name_hit=True, hits=[],
    )
    it = ResultItem(r, theme.tok("raycast"), theme.highlight_css("raycast"))
    qtbot.addWidget(it)
    texts = [lb.text() for lb in it.findChildren(QLabel)]
    assert ".ppt" in texts
    assert "老格式 · 仅文件名搜索与预览" in texts


# ---------- config：开关与特征签名 ----------

def test_index_all_files_is_always_on(monkeypatch, tmp_path):
    """常开能力，不认 ui.json 里的旧键——否则从前关过的人升级后永远开不回来。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    assert config.get_index_all_files() is True
    assert config.index_feature_signature().endswith("any_file=1")

    config.update_ui_settings(index_all_files=False)     # 老版本留下的值
    assert config.get_index_all_files() is True
    assert config.index_feature_signature().endswith("any_file=1")

    # 显式传参仍然优先：关闭路径（purge / 对账）靠它，测试也靠它
    assert config.index_feature_signature(False, False, False).endswith("any_file=0")


def test_settings_dialog_has_no_all_files_toggle(monkeypatch, qtbot, tmp_path):
    """开关已经撤掉：用户不必先找到它、再等一轮全盘重扫才能用上全文件搜索。"""
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "cfg"))
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager

    dlg = SettingsDialog(VersionManager())
    qtbot.addWidget(dlg)
    assert getattr(dlg, "all_files_feature", None) is None
    # 顺带确认没把别的功能开关误删
    assert dlg.document_feature is not None
    assert dlg.grouping_feature is not None
