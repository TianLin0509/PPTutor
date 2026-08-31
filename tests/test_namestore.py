# -*- coding: utf-8 -*-
"""全盘文件名索引（平铺文件 + 线性扫描）。

这一层替掉的是 SQLite 里那 175 万行盘点数据。它自己不认识 PPT、不解析内容，
唯一的职责是：给一串词，回答「哪些文件的名字同时含这些词」，且答案必须与
原来 SQLite 那条路逐条一致——归一化口径、多词「与」的语义、大小写折叠。
"""
from __future__ import annotations

import os
import struct

import pytest

from pptx_finder import namequery, namestore
from pptx_finder.text_tokenize import normalize


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def _build(entries, dest=None):
    b = namestore.NameStoreBuilder()
    for path, size, mtime in entries:
        b.add(path, size, mtime)
    return b.write(dest)


def _paths(store, ordinals):
    return [store.entry(i)[0] for i in ordinals]


SAMPLE = [
    (r"C:\work\reports\2026 年度总结.pptx", 1024, 1_700_000_000),
    (r"C:\work\reports\Q1 Report FINAL.pptx", 2048, 1_700_000_100),
    (r"C:\work\src\main.py", 512, 1_700_000_200),
    (r"C:\work\src\utils\helper.py", 256, 1_700_000_300),
    (r"C:\photos\IMG_0042.PNG", 4096, 1_700_000_400),
]


def test_roundtrip_every_field(tmp_path):
    """存进去什么，取出来必须一模一样——路径、名字、大小、时间。"""
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        assert store.count == len(SAMPLE)
        got = [store.entry(i) for i in range(store.count)]
    for (want_path, want_size, want_mtime), (p, name, size, mtime, is_dir) in zip(SAMPLE, got):
        assert p == want_path
        assert name == os.path.basename(want_path)
        assert size == want_size
        assert mtime == want_mtime
        assert is_dir is False


def test_directories_are_stored_once(tmp_path):
    """省下那 1 GB 的根据：同目录下 N 个文件，目录字符串只能存一份。"""
    entries = [(rf"C:\deep\nested\project\build\out\file{i}.dat", 1, 1_700_000_000)
               for i in range(500)]
    path = _build(entries)
    raw = path.read_bytes()
    assert raw.count(rb"C:\deep\nested\project\build\out") == 1


def test_single_term_search(tmp_path):
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        assert _paths(store, store.search(["report"])) == [
            r"C:\work\reports\Q1 Report FINAL.pptx"]
        assert _paths(store, store.search(["总结"])) == [
            r"C:\work\reports\2026 年度总结.pptx"]


def test_search_is_case_and_width_insensitive(tmp_path):
    """与 SQLite 那条路同一套 normalize：大小写、全半角都不该影响命中。"""
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        for query in ("REPORT", "report", "RePoRt", "ｒｅｐｏｒｔ"):
            assert _paths(store, store.search([query])) == [
                r"C:\work\reports\Q1 Report FINAL.pptx"], query
        assert _paths(store, store.search(["img_0042"])) == [r"C:\photos\IMG_0042.PNG"]
        assert _paths(store, store.search([".png"])) == [r"C:\photos\IMG_0042.PNG"]


def test_multiple_terms_are_and_not_or(tmp_path):
    """多词必须同时出现在**同一个文件名**里，和内容搜索的口径一致。"""
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        assert _paths(store, store.search(["q1", "final"])) == [
            r"C:\work\reports\Q1 Report FINAL.pptx"]
        # report 有、总结 也有，但不在同一个文件名里 → 一条都不该出
        assert store.search(["report", "总结"]) == []


def test_search_matches_name_only_not_directory(tmp_path):
    """匹配只看**这一条记录自己的名字**，不看它的父目录。

    否则一个叫 reports 的文件夹会把里面每个文件都拖成命中，结果全是噪音。
    （文件夹本身要被搜到，靠的是 add_dir 给它单独立一条记录，不是靠前缀匹配。）"""
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        assert _paths(store, store.search(["reports"])) == []


def test_scope_filters_by_directory_prefix(tmp_path):
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        assert _paths(store, store.search([".py"], scope=r"C:\work\src")) == [
            r"C:\work\src\main.py", r"C:\work\src\utils\helper.py"]
        assert store.search([".py"], scope=r"C:\photos") == []
        # 盘符大小写不该影响
        assert len(store.search([".py"], scope=r"c:\WORK\src")) == 2


def test_repeated_needle_in_one_name_counts_once(tmp_path):
    """`aaa.aaa` 里 `aa` 出现多次，不能把同一个文件报成多条。"""
    path = _build([(r"C:\x\aaaa.aaaa", 1, 1_700_000_000)])
    with namestore.NameStore(path) as store:
        assert len(store.search(["aa"])) == 1


def test_limit_caps_recall(tmp_path):
    entries = [(rf"C:\x\common{i}.txt", 1, 1_700_000_000) for i in range(50)]
    path = _build(entries)
    with namestore.NameStore(path) as store:
        assert len(store.search(["common"])) == 50
        assert len(store.search(["common"], limit=10)) == 10


def test_empty_and_whitespace_queries_return_nothing(tmp_path):
    path = _build(SAMPLE)
    with namestore.NameStore(path) as store:
        assert store.search([]) == []
        assert store.search([""]) == []
        assert store.search(["   "]) == []


def test_empty_index_is_valid(tmp_path):
    path = _build([])
    with namestore.NameStore(path) as store:
        assert store.count == 0
        assert store.search(["anything"]) == []


def test_newline_in_a_name_cannot_split_a_record(tmp_path):
    r"""名字里走私进来的 '\n' 是分隔符，必须当场换掉——否则一条记录裂成两条，
    后面所有记录的偏移全部错位。Windows 不允许这种名字，但我们索引的是别人的盘。"""
    path = _build([
        (r"C:\x\before.txt", 1, 1_700_000_000),
        ("C:\\x\\ev\nil.txt", 2, 1_700_000_001),
        (r"C:\x\after.txt", 3, 1_700_000_002),
    ])
    with namestore.NameStore(path) as store:
        assert store.count == 3
        assert store.entry(2)[1] == "after.txt"
        assert store.entry(2)[2] == 3
        assert "\n" not in store.entry(1)[1]


def test_non_utf8_names_survive(tmp_path):
    """Windows 上确实存在解不出合法 UTF-8 的文件名（孤立代理项）。

    这类名字会让 OpenCC 直接抛 UnicodeEncodeError——真机上足以把整轮扫盘炸在
    半路。归一化前要先剥掉（与 SQLite 那条路同一个 sqlite_safe_text），但**原始
    名字要原样留着**，否则交回去的路径打不开那个文件。
    """
    weird = "C:\\x\\bad\udce9name.txt"
    path = _build([(weird, 7, 1_700_000_000), (r"C:\x\ok.txt", 8, 1_700_000_001)])
    with namestore.NameStore(path) as store:
        assert store.count == 2
        assert store.entry(0)[0] == weird      # 路径原样交回，能真的打开
        assert store.entry(0)[2] == 7
        assert store.entry(1)[1] == "ok.txt"
        assert _paths(store, store.search(["ok.txt"])) == [r"C:\x\ok.txt"]
        # 代理项被剥掉后，名字里可搜的部分照常能命中
        assert _paths(store, store.search(["name.txt"])) == [weird]


def test_huge_file_size_is_stored_as_u64(tmp_path):
    """size: 查询依赖该字段，大于 4 GiB 不能再被钉成 u32 上限。"""
    size = 8 * 1024 ** 3
    path = _build([(r"C:\x\huge.iso", size, 1_700_000_000)])
    with namestore.NameStore(path) as store:
        assert store.entry(0)[2] == size


def test_missing_file_raises_namestore_error(tmp_path):
    with pytest.raises(namestore.NameStoreError):
        namestore.NameStore(tmp_path / "nope.idx")


def test_wrong_magic_raises_not_crashes(tmp_path):
    bogus = tmp_path / "bogus.idx"
    bogus.write_bytes(b"NOTANINDEX" + b"\0" * 200)
    with pytest.raises(namestore.NameStoreError):
        namestore.NameStore(bogus)


def test_version_mismatch_is_refused(tmp_path):
    """格式换代时必须明确拒绝旧文件，不能拿旧偏移当新格式读。"""
    path = _build(SAMPLE)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, 8, namestore.FORMAT_VERSION + 1)
    path.write_bytes(bytes(raw))
    with pytest.raises(namestore.NameStoreError):
        namestore.NameStore(path)


def test_truncated_file_is_refused(tmp_path):
    path = _build(SAMPLE)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(namestore.NameStoreError):
        namestore.NameStore(path)


def test_write_is_atomic_and_leaves_no_temp(tmp_path):
    """半个索引比没有索引更糟：必须写临时文件再原子改名。"""
    path = _build(SAMPLE)
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".names-")]
    assert leftovers == []
    assert path.is_file()


def test_rebuild_switches_the_pointer_to_a_new_file(tmp_path):
    """重建必须换一个新文件名再拨指针，不能就地覆盖。

    Windows 上正在被 mmap 的文件替换不掉（WinError 5）。就地覆盖的后果是：
    用户搜过一次「全部文件」之后，此后每轮建库都静默地装不上新索引——
    索引永远停在那一刻，而且**不报错**。
    """
    first = _build(SAMPLE)
    assert namestore.current_data_path() == first

    second = _build([(r"C:\only\one.txt", 1, 1_700_000_000)])
    assert second != first, "重建必须写到新文件名"
    assert namestore.current_data_path() == second
    with namestore.NameStore() as store:          # 不传路径 = 跟着指针走
        assert store.count == 1
        assert store.entry(0)[0] == r"C:\only\one.txt"


def test_rebuild_succeeds_while_a_reader_holds_the_old_index(tmp_path):
    """有人正开着旧索引时，重建照样要成功——这正是就地覆盖会炸的场景。"""
    _build(SAMPLE)
    reader = namestore.NameStore()                # 持有 mmap 不放
    try:
        newer = _build([(r"C:\new\only.txt", 1, 1_700_000_000)])
        assert namestore.current_data_path() == newer
        assert reader.count == len(SAMPLE)        # 老读者继续读老数据，不受影响
        with namestore.NameStore() as fresh:
            assert fresh.count == 1
    finally:
        reader.close()
    # 老读者退出后，下一次重建把陈旧文件顺手清掉
    _build([(r"C:\new\again.txt", 1, 1_700_000_000)])
    stale = [p.name for p in (tmp_path / "appdata").iterdir()
             if p.name.startswith("names-")]
    assert len(stale) == 1


def test_missing_pointer_reports_no_index(tmp_path):
    with pytest.raises(namestore.NameStoreError):
        namestore.NameStore()


def test_pointer_pointing_outside_the_data_dir_is_refused(tmp_path):
    """指针只允许写同目录下的文件名——不能被诱导去打开别处的文件。"""
    _build(SAMPLE)
    namestore.pointer_path().write_text(r"..\..\evil.idx", encoding="utf-8")
    assert namestore.current_data_path() is None


def test_normalization_matches_the_sqlite_path(tmp_path):
    """索引与查询必须调同一个归一化函数——两套口径就等于搜不到。

    这里用的是 namequery.fold（normalize + 去变音符号），不是通用的 normalize：
    按名字找文件要忽略变音符号（打 resume 找 résumé），而 PPT 内容搜索不折。
    """
    names = ["Ünïcödé.txt", "全形ＡＢＣ.txt", "繁體中文.txt", "MiXeD.TXT"]
    assert namequery.fold("Ünïcödé.txt") == "unicode.txt"       # 变音符号被折掉
    assert namequery.fold("繁體中文.txt") == "繁体中文.txt"        # 繁简仍照旧
    path = _build([(rf"C:\x\{n}", 1, 1_700_000_000) for n in names])
    with namestore.NameStore(path) as store:
        for n in names:
            assert store.norm_name(names.index(n)) == namequery.fold(n)
            assert len(store.search([n])) == 1


# ---------------------------------------------------------------- 与扫描的接线

def test_scan_feeds_every_file_to_the_name_sink(tmp_path, monkeypatch):
    """收料口必须拿到「本轮扫到的全部文件」，而不是「内容有变化的那些」。

    平铺索引是整份重建的：漏掉任何一个——哪怕只是因为它内容没变——那个文件
    就会从「全部文件」搜索里凭空消失。这条最容易在增量优化时被写错，所以钉死。
    """
    import fixtures_gen as fx
    from pptx_finder import db, indexer

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "deck.pptx", [{"body": "内容"}])
    (docs / "notes.txt").write_text("x", encoding="utf-8")
    (docs / "photo.png").write_bytes(b"\x89PNG")

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)

    def run():
        seen = []
        indexer.update_index(
            conn, [str(docs)], workers=1, supported_exts=(".pptx", ".ppt"),
            compute_content_hash=False,
            name_sink=lambda p, size, mtime: seen.append(os.path.basename(p)),
        )
        return sorted(seen)

    want = ["deck.pptx", "notes.txt", "photo.png"]
    assert run() == want
    # 第二轮什么都没改：内容索引会走「未变化」快路，但收料口仍须拿到全部三个
    assert run() == want
    conn.close()


def test_name_sink_is_optional(tmp_path, monkeypatch):
    """不传收料口时扫描行为与从前逐字节相同。"""
    import fixtures_gen as fx
    from pptx_finder import db, indexer

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "deck.pptx", [{"body": "内容"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    summary = indexer.update_index(
        conn, [str(docs)], workers=1, supported_exts=(".pptx", ".ppt"),
        compute_content_hash=False,
    )
    assert summary["indexed"] == 1
    conn.close()


def test_name_sink_path_writes_no_inventory_rows_to_sqlite(tmp_path, monkeypatch):
    """整件事的目的：那 175 万行盘点数据从此不进 SQLite。

    走收料口时，非内容类型的文件一行都不该落库——落了就等于两套都在付钱，
    1.19 GB 一分没省。
    """
    import fixtures_gen as fx
    from pptx_finder import db, indexer

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "deck.pptx", [{"body": "内容"}])
    for i in range(30):
        (docs / f"noise{i}.dat").write_bytes(b"x")

    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    collected = []
    indexer.update_index(
        conn, [str(docs)], workers=1, supported_exts=(".pptx", ".ppt"),
        compute_content_hash=False,
        name_sink=lambda p, size, mtime: collected.append(p),
    )
    assert len(collected) == 31            # 全部文件都进了平铺索引

    rows = conn.execute(
        "SELECT COUNT(*) FROM files WHERE lower(ext)='.dat'").fetchone()[0]
    assert rows == 0, "非内容类型不该再写进 SQLite"
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert total == 1                      # 只剩那份 PPT
    conn.close()


# ---------------------------------------------------------------- 文件夹

def test_folders_are_searchable_entries(tmp_path):
    """Everything 能按名字搜到文件夹，我们也要能。"""
    b = namestore.NameStoreBuilder()
    b.add_dir(r"C:\work\季度汇报", 1_700_000_000)
    b.add(r"C:\work\季度汇报\deck.pptx", 100, 1_700_000_001)
    with namestore.NameStore(b.write()) as store:
        hits = [store.entry(i) for i in store.search(["季度汇报"])]
    assert len(hits) == 1
    path, name, size, mtime, is_dir = hits[0]
    assert path == r"C:\work\季度汇报"
    assert name == "季度汇报"
    assert is_dir is True
    assert size == 0


def test_folder_and_file_can_share_a_name(tmp_path):
    """同名的文件夹和文件都要出现，且各自标对。"""
    b = namestore.NameStoreBuilder()
    b.add_dir(r"C:\x\backup", 1_700_000_000)
    b.add(r"C:\x\backup.zip", 10, 1_700_000_000)
    with namestore.NameStore(b.write()) as store:
        got = {e[1]: e[4] for e in (store.entry(i) for i in store.search(["backup"]))}
    assert got == {"backup": True, "backup.zip": False}


def test_drive_root_is_not_registered_as_a_folder(tmp_path):
    r"""C:\ 没有可搜的名字，登记它只会得到一条空名字记录。"""
    b = namestore.NameStoreBuilder()
    b.add_dir("C:\\", 1_700_000_000)
    b.add_dir(r"C:\real", 1_700_000_000)
    with namestore.NameStore(b.write()) as store:
        assert store.count == 1
        assert store.entry(0)[1] == "real"


def test_scan_reports_every_directory_once(tmp_path, monkeypatch):
    from pptx_finder.scanner import iter_ppt_files

    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "c").mkdir()
    (root / "a" / "f.txt").write_text("x", encoding="utf-8")
    seen = []
    list(iter_ppt_files([str(root)], inventory_all=True, dir_cb=seen.append))
    assert sorted(os.path.basename(d) for d in seen) == ["a", "b", "c", "tree"]
    assert len(seen) == len(set(seen))     # 每个目录恰好一次


def test_scan_dir_cb_respects_pruning(tmp_path):
    """被剪掉的目录不能混进来——否则 node_modules 之类会重新回到结果里。"""
    from pptx_finder.scanner import iter_ppt_files

    root = tmp_path / "tree"
    (root / "keep").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    seen = []
    list(iter_ppt_files([str(root)], inventory_all=True, dir_cb=seen.append))
    names = [os.path.basename(d) for d in seen]
    assert "keep" in names
    assert "node_modules" not in names
    assert "pkg" not in names


# ---------------------------------------------------------------- 增量层（实时更新）

def test_overlay_is_a_second_index_of_the_same_format(tmp_path):
    """新文件先进增量层，搜索时与全量一起扫。

    Everything 拿不到 NTFS 变更日志时走的就是这条路（目录变更通知 → 重列该目录）。
    我们不提权，所以照搬它这套备用机制。
    """
    b = namestore.NameStoreBuilder()
    b.add(r"C:\a\old.txt", 1, 1_700_000_000)
    b.write()
    o = namestore.NameStoreBuilder()
    o.add(r"C:\a\brand-new.txt", 1, 1_700_000_900)
    o.write(kind=namestore.OVERLAY)

    main = namestore.open_store(namestore.MAIN)
    overlay = namestore.open_store(namestore.OVERLAY)
    try:
        assert main.count == 1 and overlay.count == 1
        assert _paths(main, main.search(["brand-new"])) == []      # 全量里还没有
        assert _paths(overlay, overlay.search(["brand-new"])) == [r"C:\a\brand-new.txt"]
    finally:
        main.close()
        overlay.close()


def test_overlay_and_main_use_separate_pointers(tmp_path):
    """两套索引各有各的指针，重建其中一套不能把另一套指没了。"""
    namestore.NameStoreBuilder().write()
    namestore.NameStoreBuilder().write(kind=namestore.OVERLAY)
    main_before = namestore.current_data_path(namestore.MAIN)
    overlay_before = namestore.current_data_path(namestore.OVERLAY)
    assert main_before and overlay_before and main_before != overlay_before

    namestore.NameStoreBuilder().write(kind=namestore.OVERLAY)
    assert namestore.current_data_path(namestore.MAIN) == main_before
    assert namestore.current_data_path(namestore.OVERLAY) != overlay_before


def test_main_rebuild_does_not_delete_the_overlay_file(tmp_path):
    r"""names- 是 names-overlay- 的前缀。清理陈旧全量文件时不能把增量层顺手删了
    ——那会让刚发现的新文件凭空消失，而且不报错。"""
    namestore.NameStoreBuilder().write(kind=namestore.OVERLAY)
    overlay = namestore.current_data_path(namestore.OVERLAY)
    for _ in range(2):
        namestore.NameStoreBuilder().write()
    assert overlay.is_file()
    assert namestore.current_data_path(namestore.OVERLAY) == overlay


def test_discard_removes_a_kind_entirely(tmp_path):
    """全量刷新后要把增量层作废——它记的变化已经并进全量了。"""
    namestore.NameStoreBuilder().write()
    namestore.NameStoreBuilder().write(kind=namestore.OVERLAY)
    namestore.discard(namestore.OVERLAY)
    assert namestore.current_data_path(namestore.OVERLAY) is None
    assert namestore.current_data_path(namestore.MAIN) is not None
    assert namestore.open_store(namestore.OVERLAY) is None


def test_open_store_returns_none_instead_of_raising(tmp_path):
    """降级要安静：没有索引就是没结果，不能变成异常打到搜索线程。"""
    assert namestore.open_store(namestore.MAIN) is None
    assert namestore.open_store(namestore.OVERLAY) is None


def test_reconcile_pipeline_makes_a_new_file_searchable(tmp_path, monkeypatch):
    """端到端：目录被标脏 → 对账重建增量层 → 新文件立刻能搜到、删掉的立刻消失。

    对账逻辑就是 Everything 的备用机制：重列这个目录，而不是去改那份只读的全量索引。
    """
    from pptx_finder import search as search_mod
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "docs"
    docs.mkdir()
    stale = docs / "was-here.txt"
    stale.write_text("x", encoding="utf-8")

    b = namestore.NameStoreBuilder()
    b.add(str(stale), 1, 1_700_000_000)
    b.write()

    fresh = docs / "appeared-just-now.txt"
    fresh.write_text("x", encoding="utf-8")
    subdir = docs / "new-folder"
    subdir.mkdir()

    totals = MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    assert totals["dirs"] == 1
    assert totals["added"] >= 2          # 新文件 + 新文件夹

    stores = [s for s in (namestore.open_store(k) for k in namestore.KINDS) if s]
    try:
        names = {r.name for r in search_mod.search_names(stores, "appeared")}
        assert names == {"appeared-just-now.txt"}
        folders = search_mod.search_names(stores, "new-folder")
        assert [r.is_dir for r in folders] == [True]
        # 全量里的老文件仍在
        assert {r.name for r in search_mod.search_names(stores, "was-here")} == {
            "was-here.txt"}
    finally:
        for s in stores:
            s.close()


def test_reconcile_drops_overlay_entries_whose_file_vanished(tmp_path, monkeypatch):
    """增量层不能只涨不消：重建时顺手把已经不存在的条目扔掉。"""
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doomed = docs / "temporary.txt"
    doomed.write_text("x", encoding="utf-8")

    MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    overlay = namestore.open_store(namestore.OVERLAY)
    try:
        assert overlay.count >= 1
    finally:
        overlay.close()

    doomed.unlink()
    MainWindow._reconcile_inventory_dirs_sync((str(docs),))
    overlay = namestore.open_store(namestore.OVERLAY)
    try:
        assert all(overlay.entry(i)[1] != "temporary.txt"
                   for i in range(overlay.count))
    finally:
        overlay.close()


def test_reconcile_survives_a_directory_that_disappeared(tmp_path, monkeypatch):
    """目录本身刚被删掉：跳过即可，不能让整批对账抛异常。"""
    from pptx_finder.ui.main_window import MainWindow

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    totals = MainWindow._reconcile_inventory_dirs_sync(
        (str(tmp_path / "never-existed"),))
    assert totals["dirs"] == 0


def test_header_has_room_for_every_section(tmp_path):
    """头部必须装得下所有段的偏移表。

    装不下时 `header[:len(packed)] = packed` 会把 bytearray 撑长，头部悄悄盖掉
    第一个段的开头——**不报错**，只是所有偏移都错位、搜出来的东西是乱的。
    加段的时候最容易踩，所以钉一条。
    """
    assert namestore._HEADER_STRUCT.size <= namestore.HEADER_SIZE
    path = _build(SAMPLE)
    raw = path.read_bytes()
    # 第一个段必须正好从头部之后开始，中间不许有重叠
    with namestore.NameStore(path) as store:
        first = min(off for off, _ in store._span.values())
        assert first >= namestore.HEADER_SIZE
        assert len(raw) == max(off + ln for off, ln in store._span.values())


def test_directory_norms_are_precomputed_at_build_time(tmp_path):
    """目录的归一化形式存在索引里，查询时不再跑 NFKC/OpenCC。

    放到查询时算的话，25 万个目录每次都要现算一遍——真机实测头一条路径查询
    要为此多花约 2.7 秒。
    """
    path = _build([(r"C:\Work\軟體開發\Deck.pptx", 1, 1_700_000_000)])
    with namestore.NameStore(path) as store:
        # 繁体转简体 + 小写 + 反斜杠统一成正斜杠，全部在建库时完成
        assert store.dir_norm(0) == "c:/work/软体开发"
        assert store._dirs_matching("软体开发") == {0}
        assert store._dirs_matching("nope") == set()


# ---- 预筛必须是「必要条件」：比真正的判定更严就会静默漏结果 ----

PREFILTER_TRAP = [
    (r"D:\p\README.md", 1, 1_700_000_000),
    (r"D:\p\readme.txt", 2, 1_700_000_001),
    (r"D:\p\Readme.rst", 3, 1_700_000_002),
    (r"D:\p\café-menu.pdf", 4, 1_700_000_003),
    (r"D:\p\CAFÉ-BILL.pdf", 5, 1_700_000_004),
    (r"D:\p\cafe-plain.pdf", 6, 1_700_000_005),
    (r"D:\p\résumé.pdf", 7, 1_700_000_006),
    (r"D:\p\resume.docx", 8, 1_700_000_007),
    (r"D:\p\naïve-approach.md", 9, 1_700_000_008),
    (r"D:\p\MyReportX.txt", 10, 1_700_000_009),
    (r"D:\p\Report-Final.pptx", 11, 1_700_000_010),
]


@pytest.mark.parametrize("query", [
    "case:README", "case:Readme", "case:Report", "case:MyReport",
    "case:readme", "nocase:readme", "case:RESUME*", "case:Report-*",
    "*café*", "*CAFÉ*", "*cafe*", "café", "cafe",
    "*résumé*", "résumé", "resume", "*naïve*", "naive",
    "ww:readme", "ww:café",
])
def test_index_scan_agrees_with_per_record_match(query):
    """索引扫描的结果必须与逐条判定完全一致。

    两处真实的漏结果就是这样被抓出来的，都是「预筛比判定更严」：
      · `case:README` 拿保留大小写的针去扫**归一化过**的名字块 → 恒 0 条；
      · `*café*` 的通配符片段用 normalize()（不折变音符号）去筛，而名字块用
        fold()（折了）→ 同样恒 0 条。
    两者都不报错，只是搜不到——所以这条对账必须留着。
    """
    path = _build(PREFILTER_TRAP)
    with namestore.NameStore(path) as store:
        parsed = namequery.parse(query)
        scanned = set(store.search(parsed, limit=1000))
        expected = {i for i in range(store.count)
                    if parsed.match(store.record_for(i))}
        assert scanned == expected, (
            f"{query!r} 漏 {sorted(store.entry(i)[1] for i in expected - scanned)}"
            f" 多 {sorted(store.entry(i)[1] for i in scanned - expected)}")


def test_case_sensitive_term_actually_finds_something():
    """光对账还不够：如果两边一起错成空集也算「一致」。这条钉住真实答案。"""
    path = _build(PREFILTER_TRAP)
    with namestore.NameStore(path) as store:
        names = {store.entry(i)[1]
                 for i in store.search(namequery.parse("case:README"), limit=10)}
        assert names == {"README.md"}
        names = {store.entry(i)[1]
                 for i in store.search(namequery.parse("*café*"), limit=10)}
        assert names == {"café-menu.pdf", "CAFÉ-BILL.pdf", "cafe-plain.pdf"}


def test_pointer_replace_retries_when_a_reader_holds_it(monkeypatch, tmp_path):
    """指针文件被读的那一瞬间 Windows 不让替换，必须退让重试而不是放弃整轮建库。

    Python 的 open() 不带 FILE_SHARE_DELETE，所以 current_data_path 读指针的几十
    微秒里 os.replace 会撞 WinError 5（PermissionError）。真机压测「3 个搜索线程 +
    连续换库 8 次」实测 8 次里失败 1 次；失败的后果是整轮全盘扫描白干、索引停在
    上一份，而且用户完全看不出来。
    """
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        # 只拦指针那一次；数据文件每轮都是新名字，压根不会被占住
        if str(dst) == str(namestore.pointer_path()) and calls["n"] < 3:
            calls["n"] += 1
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(namestore.os, "replace", flaky)
    monkeypatch.setattr(namestore, "_POINTER_RETRY_SLEEP", 0)
    path = _build(SAMPLE)
    assert calls["n"] == 3                       # 确实撞上了，不是没触发
    assert namestore.current_data_path() == path
    with namestore.NameStore(path) as store:
        assert store.count == len(SAMPLE)


def test_pointer_falls_back_to_in_place_write_when_replace_never_wins():
    """一直抢不到 replace 就就地重写，绝不能让这一轮建库白干。

    os.replace 需要对目标拿 DELETE 权限，而 Python 的 open() 不带
    FILE_SHARE_DELETE——读者一多就永远抢不到空档。真机压测：6 个线程死循环搜 +
    连续换库 40 次，纯重试仍失败 6 次。装不上是**永久性**的错（此后一直搜旧快照），
    而就地写最坏只是某一次查询读到半截名字、当次降级成空结果。
    """
    real_replace = os.replace

    def always_locked(src, dst):
        if str(dst) == str(namestore.pointer_path()):
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch_sleep = 0
    b = namestore.NameStoreBuilder()
    for path, size, mtime in SAMPLE:
        b.add(path, size, mtime)
    import unittest.mock as _mock
    with _mock.patch.object(namestore.os, "replace", always_locked),          _mock.patch.object(namestore, "_POINTER_RETRY_SLEEP", monkeypatch_sleep):
        dest = b.write()
    assert namestore.current_data_path() == dest
    with namestore.NameStore(dest) as store:
        assert store.count == len(SAMPLE)


def test_pointer_file_names_are_fixed_width_so_a_torn_read_cannot_alias():
    """就地重写的安全前提：两代文件名长度恒等，半截读到的只能是旧名/新名/不存在的名。

    长度一旦不等，半截读就可能拼出一个**存在但不该指向**的文件名。
    """
    a = namestore.NameStoreBuilder()
    a.add(r"D:\p.txt", 1, 1_700_000_000)
    first = a.write()
    b = namestore.NameStoreBuilder()
    b.add(r"D:\p.txt", 1, 1_700_000_001)
    second = b.write()
    assert len(first.name) == len(second.name)
    assert first.name != second.name
