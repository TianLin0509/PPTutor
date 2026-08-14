"""任意文件名（Everything 式）功能的压测与对抗性边界测试。

覆盖：
- 10 万文件临时树的盘点端到端：耗时 / DB 体积 / 内存峰值 / 文件计数对账；
- 百万行 file_names_fts 下名称搜索延迟（含 name_limit=10 万 vs SQLite 变量上限）；
- 盘点中途 kill 进程的崩溃一致性与重启自愈；
- 开关横跳（开→扫→关 purge→开重扫）的状态收敛；
- index_single 对 ~$/.tmp/.crdownload/0 字节/无扩展名/.gitignore 的处理；
- _scan_known_index_changes SQL 收窄后的语义等价性。

绝不索引真实磁盘：全部使用 pytest 临时目录树。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

import fixtures_gen as fx

from pptx_finder import db, indexer, search
from pptx_finder.ui.main_window import _scan_known_index_changes

REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")

CONTENT_EXTS = (".pptx", ".ppt")  # 与「文档搜索关闭」时的生产口径一致


def _rss_bytes() -> int:
    """当前进程 WorkingSetSize（Windows）；失败返回 -1。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD)] + [
                (f, ctypes.c_size_t) for f in (
                    "PageFaultCount", "PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        ok = ctypes.windll.kernel32.K32GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(pmc),
            pmc.cb,
        )
        return int(pmc.WorkingSetSize) if ok else -1
    except Exception:  # noqa: BLE001
        return -1


def _mk(path: Path, data: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _build_big_tree(root: Path) -> dict:
    """10 万文件混合树。返回各类文件计数清单用于对账。"""
    manifest = {
        "junk": 0, "deep": 0, "lock": 0, "pruned": 0,
        "pptx": 0, "ppt": 0, "docx": 0,
    }
    t0 = time.perf_counter()
    junk_exts = [".dat", ".log", ".tmp", ".crdownload", ".zzz", ".DAT", ""]
    # 94,733 个杂项文件：100 个区域目录 × 每层 10 个子目录
    n_junk = 0
    for area in range(100):
        for sub in range(10):
            d = root / f"area_{area:03d}" / f"sub_{sub:02d}"
            d.mkdir(parents=True, exist_ok=True)
            for i in range(95):
                ext = junk_exts[(area + sub + i) % len(junk_exts)]
                name = f"quarterly report {area:03d}-{sub:02d}-{i:03d}{ext}"
                _mk(d / name)
                n_junk += 1
    # 少量特殊文件名（中文 / 纯扩展名 / 无扩展名 / 超长名 / 大写扩展名）
    specials = [
        "年度总结 2026.final", ".gitignore", "README", "Makefile",
        "σ 希腊字母 τ 实验数据.dat",
        "长文件名" + "非" * 100 + ".dat",
        "UPPERCASE REPORT.DAT",
    ]
    for i, name in enumerate(specials):
        _mk(root / f"area_{i:03d}" / name)
        n_junk += 1
    manifest["junk"] = n_junk

    # 20 层嵌套：10 条链 × 叶子 20 个文件 = 200（短目录名控制总长在 MAX_PATH 内）
    for chain in range(10):
        d = root / "deep" / f"c{chain}"
        for lvl in range(19):
            d = d / f"l{lvl:02d}"
        for i in range(20):
            _mk(d / f"deep report {chain}-{i}.dat")
            manifest["deep"] += 1

    # ~$ 锁文件（scanner 必须跳过）
    for i in range(50):
        _mk(root / f"area_{i:03d}" / f"~$lock report {i}.pptx")
        manifest["lock"] += 1
    # 剪枝目录（node_modules / $ 前缀）
    for i in range(500):
        _mk(root / "area_000" / "node_modules" / f"dep{i}.js")
        manifest["pruned"] += 1
    for i in range(100):
        _mk(root / "$RECYCLE.BIN" / f"del{i}.dat")
        manifest["pruned"] += 1

    # 内容类型
    for i in range(12):
        fx.make_pptx(root / "area_000" / f"deck {i}.pptx", [{"body": f"算力 {i}"}])
        manifest["pptx"] += 1
    for i in range(25):
        _mk(root / "area_001" / f"old {i}.ppt", b"old ppt")
        manifest["ppt"] += 1
    for i in range(30):
        _mk(root / "area_002" / f"notes {i}.docx", b"fake docx")
        manifest["docx"] += 1

    manifest["build_s"] = time.perf_counter() - t0
    manifest["expected_indexed"] = (
        manifest["junk"] + manifest["deep"]
        + manifest["pptx"] + manifest["ppt"] + manifest["docx"]
    )
    return manifest


@pytest.fixture(scope="session")
def big_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("bigtree") / "data"
    root.mkdir(parents=True)
    manifest = _build_big_tree(root)
    return root, manifest


@pytest.fixture(scope="session")
def big_db(big_tree, tmp_path_factory):
    """对 10 万树跑一次完整盘点（session 级复用）。返回 (db_path, summary, manifest, timing)。"""
    root, manifest = big_tree
    db_path = tmp_path_factory.mktemp("bigdb") / "index.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    rss0 = _rss_bytes()
    t0 = time.perf_counter()
    summary = indexer.update_index(
        conn, [str(root)], workers=1,
        supported_exts=CONTENT_EXTS, index_all_files=True,
    )
    elapsed = time.perf_counter() - t0
    rss1 = _rss_bytes()
    size_bytes = os.path.getsize(db_path)
    conn.close()
    return {
        "db_path": str(db_path), "summary": summary, "manifest": manifest,
        "elapsed_s": elapsed, "rss_delta_mb": (rss1 - rss0) / 1e6 if rss0 > 0 else -1,
        "db_size_mb": size_bytes / 1e6,
    }


# ---------- 10 万文件树：端到端盘点 ----------

@pytest.mark.slow
def test_inventory_100k_tree(big_tree, big_db):
    root, manifest = big_tree
    summary = big_db["summary"]
    expected = manifest["expected_indexed"]

    # 文件计数对账：磁盘实际文件数 == 清单
    disk_files = sum(1 for p in root.rglob("*") if p.is_file())
    assert disk_files == expected + manifest["lock"] + manifest["pruned"]

    assert summary["scanned"] == expected
    assert summary["indexed"] == manifest["pptx"]
    assert summary["skipped_ppt"] == manifest["ppt"]
    assert summary["filename_only"] == expected - manifest["pptx"] - manifest["ppt"]
    assert summary["deleted"] == 0

    conn = db.connect(big_db["db_path"])
    try:
        files_n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        fts_n = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
        assert files_n == expected
        assert fts_n == files_n  # files 与 FTS 一一对应
        status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        assert status.get("ok") == manifest["pptx"]
        assert status.get("filename_only") == expected - manifest["pptx"]
        # ~$ 锁文件与剪枝目录不入库
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE name LIKE '~$%'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE '%node_modules%'").fetchone()[0] == 0
        # 盘点行可按名搜到（含中文名/无扩展名/纯扩展名文件）
        assert any(r.name.startswith("年度总结") for r in search.search(
            conn, "年度总结", exts=None, name_limit=search.ANY_FILE_NAME_LIMIT))
        assert any(r.name == ".gitignore" for r in search.search(
            conn, "gitignore", exts=None))
        # 任意文件名模式口径：exts=None 能跨扩展名命中
        hits = search.search(conn, "deep report 3-7", exts=None)
        assert [r.name for r in hits] == ["deep report 3-7.dat"]
    finally:
        conn.close()
    print(
        f"\n[100k inventory] build {manifest['build_s']:.1f}s "
        f"scan {big_db['elapsed_s']:.1f}s ({expected / big_db['elapsed_s']:.0f} files/s) "
        f"DB {big_db['db_size_mb']:.1f}MB RSS-delta {big_db['rss_delta_mb']:.0f}MB"
    )


@pytest.mark.slow
def test_inventory_100k_incremental_rescan(big_tree, big_db):
    """未变更重扫：(size,mtime) 快筛下不应重写任何盘点行。"""
    root, manifest = big_tree
    conn = db.connect(big_db["db_path"])
    t0 = time.perf_counter()
    again = indexer.update_index(
        conn, [str(root)], workers=1,
        supported_exts=CONTENT_EXTS, index_all_files=True,
    )
    elapsed = time.perf_counter() - t0
    conn.close()
    assert again["scanned"] == manifest["expected_indexed"]
    assert again["filename_only"] == 0
    assert again["indexed"] == 0 and again["deleted"] == 0
    print(f"\n[100k incremental rescan] {elapsed:.1f}s (first scan was much slower)")


@pytest.mark.slow
def test_inventory_100k_delete_channel(big_tree, big_db):
    """删除通道：盘点行与内容行都能被正确回收，且不误删。"""
    root, manifest = big_tree
    (root / "area_002" / "README").unlink()  # 无扩展名盘点行
    (root / "area_000" / "deck 0.pptx").unlink()  # 内容行
    conn = db.connect(big_db["db_path"])
    summary = indexer.update_index(
        conn, [str(root)], workers=1,
        supported_exts=CONTENT_EXTS, index_all_files=True,
    )
    assert summary["deleted"] == 2
    files_n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    fts_n = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
    assert files_n == manifest["expected_indexed"] - 2
    assert fts_n == files_n
    assert search.search(conn, "README", exts=None) == []
    conn.close()


# ---------- 内存：all_indexed / seen 随盘点规模的实际开销 ----------

def _fill_filename_only(conn, n: int, *, start: int = 0, common: str = "report") -> float:
    """走真实 _write_filename_only_batch 灌 n 行，返回耗时。"""
    t0 = time.perf_counter()
    batch = []
    for i in range(start, start + n):
        batch.append((
            f"C:/inv/d{i % 997:03d}/quarterly {common} {i}.dat",
            f"quarterly {common} {i}.dat", ".dat", 1, 1.0, 1.0,
        ))
        if len(batch) >= 200:
            indexer._write_filename_only_batch(conn, batch)
            batch.clear()
            conn.commit()
    if batch:
        indexer._write_filename_only_batch(conn, batch)
        conn.commit()
    return time.perf_counter() - t0


def _fill_direct(conn, n: int, *, start: int = 0, common: str = "report") -> None:
    """直插灌库（跳过真实写路径）：只用于快速构造百万行搜索延迟场景。

    真实写入路径的吞吐由 test_batch_write_throughput_stable 单独测量；
    百万行灌库走直插纯粹是为了夹具构建速度。
    """
    from pptx_finder.text_tokenize import normalize, tokenize
    batch_files, batch_fts = [], []
    for i in range(start, start + n):
        path = f"C:/inv/d{i % 997:03d}/quarterly {common} {i}.dat"
        name = f"quarterly {common} {i}.dat"
        batch_files.append((path, name, normalize(name), ".dat", 1, 1.0, "size:1", 0,
                            "filename_only", "", 0, 0, 1.0))
        batch_fts.append((tokenize(name), i + 1))  # 首批从 0 开始：AUTOINCREMENT 与 start+i 对齐
        if len(batch_files) >= 5000:
            conn.executemany(
                "INSERT INTO files(path,name,name_norm,ext,size,mtime,content_hash,"
                "page_count,status,error,parse_failures,retry_after,indexed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch_files)
            conn.executemany(
                "INSERT INTO file_names_fts(content,file_id) VALUES(?,?)", batch_fts)
            conn.commit()
            batch_files, batch_fts = [], []
    if batch_files:
        conn.executemany(
            "INSERT INTO files(path,name,name_norm,ext,size,mtime,content_hash,"
            "page_count,status,error,parse_failures,retry_after,indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch_files)
        conn.executemany(
            "INSERT INTO file_names_fts(content,file_id) VALUES(?,?)", batch_fts)
        conn.commit()


HUGE_ROWS = 1_000_000


@pytest.fixture(scope="session")
def huge_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("hugedb") / "huge.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    t0 = time.perf_counter()
    # 200k 含常见词 report；50k 含 sigma；其余唯一化。注意 file_id 按插入序 1..N。
    _fill_direct(conn, 200_000, start=0, common="report")
    _fill_direct(conn, 50_000, start=200_000, common="sigma")
    _fill_direct(conn, HUGE_ROWS - 250_000, start=250_000, common="x")
    elapsed = time.perf_counter() - t0
    n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
    assert n == HUGE_ROWS and fts == HUGE_ROWS
    # file_id 对齐自检：直插假设 id == 行序
    lo, hi = conn.execute("SELECT MIN(id), MAX(id) FROM files").fetchone()
    assert (int(lo), int(hi)) == (1, HUGE_ROWS)
    conn.close()
    print(f"\n[fill huge db] {HUGE_ROWS:,} rows in {elapsed:.1f}s -> {HUGE_ROWS / elapsed:.0f} rows/s")
    return str(db_path)


def test_batch_write_throughput_stable(tmp_path):
    """真实 _write_filename_only_batch 的分档吞吐：修复后应近似平直。

    旧实现批内 `DELETE FROM file_names_fts WHERE file_id=?` 的 file_id 是
    UNINDEXED 列，每次删除全表扫 → 表越大每批越慢（实测 2116→60 rows/s 衰减）。
    修复后仅对确已存在的 file_id 发 DELETE，首轮盘点是纯插入，分档吞吐不得显著衰减。
    """
    conn = db.connect(tmp_path / "w.db")
    db.init_db(conn)
    rates = []
    start = 0
    for _seg in range(4):
        dt = _fill_filename_only(conn, 10_000, start=start)
        start += 10_000
        rates.append(10_000 / dt)
    conn.close()
    print(f"\n[batch write] per-10k-segment rows/s: {[f'{r:.0f}' for r in rates]}")
    assert rates[-1] >= rates[0] * 0.5  # 无显著退化（修复前末段跌到首段的 ~3%）


@pytest.mark.slow
def test_all_indexed_memory_1m(huge_db):
    """db.all_indexed 百万行内存实测（实现者已自曝该点，这里给定量）。"""
    conn = db.connect(huge_db)
    rss0 = _rss_bytes()
    tracemalloc.start()
    t0 = time.perf_counter()
    existing = db.all_indexed(conn)
    elapsed = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = _rss_bytes()
    assert len(existing) == HUGE_ROWS
    print(
        f"\n[all_indexed 1M rows] tracemalloc peak {peak / 1e6:.0f}MB "
        f"RSS-delta {(rss1 - rss0) / 1e6:.0f}MB load {elapsed:.1f}s"
    )
    del existing
    conn.close()


@pytest.mark.slow
def test_all_indexed_stats_projection_memory_1m(huge_db):
    """M3 修复：update_index 实际使用的轻量投影，峰值内存应显著低于全量 Row。"""
    conn = db.connect(huge_db)
    tracemalloc.start()
    t0 = time.perf_counter()
    existing = db.all_indexed_stats(conn)
    elapsed = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(existing) == HUGE_ROWS
    sample = next(iter(existing.values()))
    # 消费方兼容：与 sqlite3.Row 一样的 ["字段"] 访问
    assert sample["status"] == "filename_only"
    assert int(sample["size"]) == 1
    print(
        f"\n[all_indexed_stats 1M rows] tracemalloc peak {peak / 1e6:.0f}MB "
        f"load {elapsed:.1f}s (full Row ~709MB)"  # ASCII-only: console is cp1252
    )
    assert peak < 500e6  # 全量 Row 实测约 700MB；投影必须明显更低
    del existing
    conn.close()


# ---------- 百万行 FTS 名称搜索延迟 ----------

@pytest.mark.slow
def test_fts_1m_rare_term_latency(huge_db):
    conn = db.connect(huge_db)
    t0 = time.perf_counter()
    hits = search.search(conn, "zxqwv 不存在", exts=None,
                         name_limit=search.ANY_FILE_NAME_LIMIT)
    elapsed = (time.perf_counter() - t0) * 1000
    conn.close()
    assert hits == []
    print(f"\n[1M fts rare term] {elapsed:.0f}ms")
    assert elapsed < 3000


@pytest.mark.slow
def test_fts_1m_common_term_limit_3000(huge_db):
    """sigma 命中 5 万行，默认 3000 截断：召回被截断但延迟可测。"""
    conn = db.connect(huge_db)
    t0 = time.perf_counter()
    hits = search.search(conn, "sigma", exts=None, name_limit=3000)
    elapsed = (time.perf_counter() - t0) * 1000
    conn.close()
    assert len(hits) == 200  # UI limit 截断；候选只有 3000（5 万召回被截断）
    print(f"\n[1M fts sigma(50k matches) limit3000] {elapsed:.0f}ms")


@pytest.mark.slow
def test_fts_1m_common_term_30k_under_var_cap(huge_db):
    """30k < SQLite 32766 变量上限：完整链路可走通，量化 Python 侧验证成本。"""
    conn = db.connect(huge_db)
    t0 = time.perf_counter()
    hits = search.search(conn, "sigma", exts=None, name_limit=30_000)
    elapsed = (time.perf_counter() - t0) * 1000
    conn.close()
    assert len(hits) == 200
    print(f"\n[1M fts sigma limit30000] {elapsed:.0f}ms (full python verify + IN query)")


@pytest.mark.slow
def test_fts_1m_any_filename_100k_limit_works(huge_db):
    """「任意文件名」模式 name_limit=10 万：20 万命中远超 SQLite 变量上限 32766。

    修复前：search.py 的 `SELECT * FROM files WHERE id IN (...)` 未分批，100k 候选
    全部进 IN 列表直接 OperationalError。修复后按 1 万分批合并，常见词稳定返回。
    """
    conn = db.connect(huge_db)  # 20 万行含 report
    t0 = time.perf_counter()
    hits = search.search(conn, "report", exts=None, name_limit=search.ANY_FILE_NAME_LIMIT)
    elapsed = (time.perf_counter() - t0) * 1000
    conn.close()
    assert len(hits) == 200  # UI limit 截断；候选 10 万按 mtime DESC,id 确定性截断
    assert all(r.name_hit for r in hits)
    print(f"\n[1M fts report(200k matches) limit100k] {elapsed:.0f}ms (batched IN)")


# ---------- 崩溃注入：盘点跑到一半杀进程 ----------

CRASH_CHILD = r"""
import sys, time
sys.path.insert(0, {src!r})
from pptx_finder import db, indexer
conn = db.connect(sys.argv[1])
db.init_db(conn)
summary = indexer.update_index(
    conn, [sys.argv[2]], workers=1,
    supported_exts=(".pptx", ".ppt"), index_all_files=True,
)
print("DONE", summary["scanned"], flush=True)
"""


@pytest.mark.slow
def test_crash_mid_inventory_then_recover(tmp_path):
    root = tmp_path / "data"
    for area in range(40):
        d = root / f"a{area:02d}"
        d.mkdir(parents=True)
        for i in range(400):
            _mk(d / f"crash report {area:02d}-{i:03d}.dat")
    expected = 16_000
    db_path = tmp_path / "crash.db"
    child = tmp_path / "child.py"
    child.write_text(CRASH_CHILD.format(src=REPO_SRC), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(child), str(db_path), str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # 等到至少落了几批（commit 节奏 200/批）再杀
    deadline = time.time() + 120
    killed_rows = 0
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            conn = db.connect(db_path)
            n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
            if n >= 2000:
                killed_rows = n
                proc.kill()
                break
        except sqlite3.Error:
            time.sleep(0.1)
    out, _ = proc.communicate(timeout=30)
    assert killed_rows >= 2000, f"child not killed mid-scan (rows={killed_rows}): {out[-500:]}"
    assert "DONE" not in out
    print(f"\n[crash injection] committed rows at kill: {killed_rows}")

    # 重启打开（WAL 恢复）：files 与 FTS 不得错位
    conn = db.connect(db_path)
    files_n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    fts_n = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
    orphan_fts = conn.execute(
        "SELECT COUNT(*) FROM file_names_fts f LEFT JOIN files ON files.id=f.file_id "
        "WHERE files.id IS NULL").fetchone()[0]
    files_no_fts = conn.execute(
        "SELECT COUNT(*) FROM files f LEFT JOIN file_names_fts t ON t.file_id=f.id "
        "WHERE t.file_id IS NULL").fetchone()[0]
    assert files_n >= killed_rows  # 观察到 kill 生效之间可能又落了一批（写路径变快后窗口变大）
    assert files_n % 200 == 0  # WAL 回滚到最后一次 commit 边界（盘点批 200 行一提交）
    assert fts_n == files_n and orphan_fts == 0 and files_no_fts == 0
    assert files_n < expected  # 确实没跑完

    # 自愈：重跑补齐，无重复行、无错位
    summary = indexer.update_index(
        conn, [str(root)], workers=1,
        supported_exts=CONTENT_EXTS, index_all_files=True,
    )
    assert summary["scanned"] == expected
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == expected
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == expected
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT path FROM files GROUP BY path HAVING COUNT(*)>1)"
    ).fetchone()[0]
    assert dup == 0
    hits = search.search(conn, "crash report 39-399", exts=None)
    assert [r.name for r in hits] == ["crash report 39-399.dat"]
    conn.close()


# ---------- 开关横跳：开→扫→关（purge）→开（重扫）的状态收敛 ----------

def test_toggle_churn_converges(tmp_path):
    root = tmp_path / "data"
    for i in range(3000):
        _mk(root / f"d{i % 30:02d}" / f"churn file {i:04d}.xyz")
    fx.make_pptx(root / "deck.pptx", [{"body": "算力"}])
    _mk(root / "old.ppt", b"old")
    _mk(root / "notes.docx", b"fake docx")
    conn = db.connect(tmp_path / "churn.db")
    db.init_db(conn)
    kw = dict(workers=1, supported_exts=CONTENT_EXTS, index_all_files=True)

    indexer.update_index(conn, [str(root)], **kw)
    n_on = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert n_on == 3003
    assert search.search(conn, "churn file 0001", exts=None)

    # 关：purge 清掉非内容盘点行；.ppt/.docx（SUPPORTED_EXTS）保留
    removed = indexer.purge_non_content_filename_only(conn)
    assert removed == 3000
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3
    assert search.search(conn, "churn file 0001", exts=None) == []
    # 幂等：再 purge 一次删 0 行
    assert indexer.purge_non_content_filename_only(conn) == 0

    # 再开：重扫把盘点行补回来
    indexer.update_index(conn, [str(root)], **kw)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == n_on
    assert search.search(conn, "churn file 0001", exts=None)
    # FTS 无重复/无幽灵
    assert conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0] == n_on
    conn.close()


def test_inventory_row_of_content_ext_refreshes_stat_when_doc_search_off(tmp_path):
    """对抗交错：docx 已有内容行 → 文档搜索关闭 + 任意文件开 → 文件被修改。

    守卫拆字段后：status/page_count/content_hash 等内容字段仍被保护（不降级），
    但 size/mtime/indexed_at 正常刷新——stat 不再永远冻结在旧值。
    """
    root = tmp_path / "data"
    root.mkdir()
    target = root / "keep.docx"
    target.write_bytes(b"v1")
    conn = db.connect(tmp_path / "s.db")
    db.init_db(conn)
    db.upsert_file(
        conn, path=str(target), name=target.name, ext=".docx", size=2, mtime=1.0,
        content_hash="size:2", page_count=3, status="ok", error="", indexed_at=1.0,
    )
    conn.commit()
    st = target.stat()
    # 文档搜索关（supported 只含 pptx/ppt）+ 任意文件开
    indexer.update_index(
        conn, [str(root)], workers=1,
        supported_exts=CONTENT_EXTS, index_all_files=True,
    )
    row = db.get_file_by_path(conn, str(target))
    assert row["status"] == "ok" and row["page_count"] == 3  # 未被降级
    assert row["content_hash"] == "size:2"  # 内容指纹受守卫保护
    assert abs(float(row["mtime"]) - float(st.st_mtime)) <= 1e-6  # stat 已刷新
    assert int(row["size"]) == int(st.st_size)
    conn.close()


# ---------- 竞态：扫描进行中关开关（purge）→ 盘点行残留 ----------

@pytest.mark.slow
def test_purge_races_inflight_inventory_scan(tmp_path):
    """扫描跑到一半执行 purge（= 扫描进行中用户在设置里关开关）：
    关开关那一下的即时 purge 清不掉在途扫描之后继续写入的行——
    修复后 update_index 收尾复检开关（index_all_files_provider），补一次 purge，
    残留必须为 0。throttle 门控保证「中途翻开关」确定性地发生在扫描进行中。
    """
    import threading

    root = tmp_path / "data"
    for i in range(12000):
        _mk(root / f"d{i % 40:02d}" / f"race file {i:05d}.xyz")
    conn = db.connect(tmp_path / "race.db")
    db.init_db(conn)
    done = threading.Event()
    switch = {"on": True}  # 模拟设置开关的实时状态
    box: dict = {}

    calls = {"n": 0}

    def throttle():
        # 先放扫描跑过首批提交（200/批），再在扫描线程内等主线程把开关翻掉，
        # 消除「扫描先跑完」的时序偶然
        calls["n"] += 1
        if calls["n"] < 400:
            return
        waited = 0.0
        while switch["on"] and waited < 30.0:
            time.sleep(0.01)
            waited += 0.01

    def run_scan():
        box["summary"] = indexer.update_index(
            conn, [str(root)], workers=1,
            supported_exts=CONTENT_EXTS, index_all_files=True,
            index_all_files_provider=lambda: switch["on"],
            throttle_cb=throttle,
        )
        done.set()

    t = threading.Thread(target=run_scan, daemon=True)
    t.start()
    # 等扫描落了一批后执行 purge（模拟用户中途关开关）；轮询用独立连接
    deadline = time.time() + 60
    purged = 0
    poll = db.connect(tmp_path / "race.db")
    try:
        while time.time() < deadline:
            n = poll.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            if n >= 200:
                switch["on"] = False  # 关开关（扫描线程随即从 throttle 放行）
                break
            time.sleep(0.01)
        assert not switch["on"], "扫描首批提交迟迟不可见"
        for _ in range(50):  # 即时 purge 需等扫描提交间隙拿写锁
            try:
                purged = indexer.purge_non_content_filename_only(poll)
                break
            except sqlite3.OperationalError:
                time.sleep(0.1)
    finally:
        poll.close()
    t.join(timeout=120)
    assert done.is_set(), "扫描线程未结束"
    leftover = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='filename_only'"
    ).fetchone()[0]
    print(
        f"\n[purge race] purged {purged} mid-scan, "
        f"scan wrote {box['summary']['filename_only']} total, leftover: {leftover}"
    )
    assert purged > 0  # 中途 purge 确实发生
    assert box["summary"]["filename_only"] > purged  # purge 之后扫描仍在写入（竞态存在）
    assert leftover == 0  # 收尾复检补清：不得有残留
    conn.close()


# ---------- index_single：watcher 增量边界 ----------

def test_index_single_edge_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    conn = db.connect(tmp_path / "e.db")
    db.init_db(conn)
    kw = dict(supported_exts=CONTENT_EXTS, index_all_files=True)

    cases = {
        "partial.crdownload": b"x",      # 浏览器半成品
        "cache.tmp": b"x",               # 临时文件
        "zero.dat": b"",                 # 0 字节
        "README": b"x",                  # 无扩展名
        ".gitignore": b"x",              # 纯扩展名文件（suffix=''）
        "年度报告 2026.final": b"x",      # 中文名
    }
    for name, data in cases.items():
        p = root / name
        p.write_bytes(data)
        assert indexer.index_single(conn, str(p), **kw) is True, name
        row = db.get_file_by_path(conn, str(p))
        assert row["status"] == "filename_only", name

    # ~$ 锁文件：与 scanner 口径一致，index_single 也直接跳过（不登记、不删旧行）
    lock = root / "~$lock.tmp"
    lock.write_bytes(b"x")
    assert indexer.index_single(conn, str(lock), **kw) is False
    assert db.get_file_by_path(conn, str(lock)) is None

    # 无扩展名 / 中文名可按名搜到
    assert any(r.name == "README" for r in search.search(conn, "README", exts=None))
    assert search.search(conn, "年度报告", exts=None)[0].name == "年度报告 2026.final"

    # 删除通道：文件消失 → index_single 删行 + 清 FTS
    (root / "cache.tmp").unlink()
    assert indexer.index_single(conn, str(root / "cache.tmp"), **kw) is True
    assert db.get_file_by_path(conn, str(root / "cache.tmp")) is None
    assert search.search(conn, "cache", exts=None) == []
    conn.close()


def test_index_single_content_ext_upgrade_and_guard(tmp_path):
    """docx：盘点行 → 文档搜索开后 watcher 升级；内容行不被盘点降级。"""
    root = tmp_path / "data"
    root.mkdir()
    conn = db.connect(tmp_path / "g.db")
    db.init_db(conn)
    target = root / "a.docx"
    target.write_bytes(b"v1")
    # 文档搜索关 + 任意文件开：登记盘点行
    assert indexer.index_single(
        conn, str(target), supported_exts=CONTENT_EXTS, index_all_files=True) is True
    assert db.get_file_by_path(conn, str(target))["status"] == "filename_only"
    conn.close()


# ---------- 启动 reconcile 的 SQL 收窄：语义等价 ----------

def test_scan_known_index_changes_ignores_inventory_rows(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    junk = root / "note.dat"
    junk.write_bytes(b"v1")
    pptx = root / "a.pptx"
    fx.make_pptx(pptx, [{"body": "算力"}])
    conn = db.connect(tmp_path / "r.db")
    db.init_db(conn)
    # 盘点行（.dat）+ pending 内容行（.pptx）
    db.upsert_file(
        conn, path=str(junk), name=junk.name, ext=".dat", size=2, mtime=1.0,
        content_hash="size:2", page_count=0, status="filename_only", error="",
        indexed_at=1.0,
    )
    db.upsert_file(
        conn, path=str(pptx), name=pptx.name, ext=".pptx", size=2, mtime=1.0,
        content_hash="size:2", page_count=0, status="pending", error="",
        indexed_at=1.0,
    )
    conn.commit()
    conn.close()
    junk.write_bytes(b"v1-changed-larger")  # 盘点行对应的文件有变化

    payload = _scan_known_index_changes(
        str(tmp_path / "r.db"), supported_exts=CONTENT_EXTS)
    paths = payload["paths"] + payload["pending_paths"]
    # 盘点行不进 reconcile（与收窄前 Python 过滤语义一致）；pending pptx 要续建
    assert str(junk) not in paths
    assert any(p.endswith("a.pptx") for p in paths)
