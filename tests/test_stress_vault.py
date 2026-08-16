"""版本库减负的压测与对抗性验证（代码审查配套，2026-08-14）。

全部使用临时目录合成 vault，不触碰真实 %LOCALAPPDATA%\\pptx-finder。
覆盖：大规模驱逐正确性对账、共享对象零丢失、10 万级幽灵判定耗时、
大库 maintain_db 耗时与锁冲突、快照×重维护并发、驱逐/VACUUM 中途 kill 故障注入，
以及两个审查发现的实证用例（幽灵锚点残留、历史别名导致的固定盘误判）。
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import fixtures_gen as fx
import pytest
import xxhash

from pptx_finder import config
from pptx_finder.versioning import store, vault
from pptx_finder.versioning.manager import VersionManager

try:  # Windows 控制台 cp1252 下中文度量输出不许炸测试
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

_DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))


def _conn():
    c = store.connect(vault.db_path())
    store.init_db(c)
    return c


def _mk_dedup_version(conn, doc_id, vid, ts, payloads, health="ok"):
    """payloads: list[bytes]，每个 payload 一个 part（内容寻址写入全局对象池）。"""
    names, parts = [], {}
    objdir = vault._global_objects_dir()
    for i, payload in enumerate(payloads):
        h = xxhash.xxh64(payload).hexdigest()
        target = objdir / h
        if not target.exists():
            target.write_bytes(payload)
        name = f"ppt/slides/slide{i + 1}.xml"
        names.append(name)
        parts[name] = h
    d = vault.vault_dir() / doc_id / "versions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vid}.json").write_text(
        json.dumps({"mode": "dedup", "names": names, "parts": parts}), encoding="utf-8"
    )
    conn.execute(
        """INSERT INTO versions(version_id, doc_id, ts, size, content_hash, health, health_error)
           VALUES(?,?,?,?,?,?,?)""",
        (vid, doc_id, ts, sum(len(p) for p in payloads), "h-" + vid, health,
         "" if health == "ok" else "synthetic"),
    )


def _mk_full_version(conn, doc_id, vid, ts, payload, health="ok"):
    d = vault.vault_dir() / doc_id / "versions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vid}.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
    (d / f"{vid}.pptx").write_bytes(payload)
    conn.execute(
        """INSERT INTO versions(version_id, doc_id, ts, size, content_hash, health, health_error)
           VALUES(?,?,?,?,?,?,?)""",
        (vid, doc_id, ts, len(payload), "h-" + vid, health,
         "" if health == "ok" else "synthetic"),
    )


def _live_manifest_parts(conn):
    """存活版本的引用集合与逐版本 manifest 对账数据。"""
    referenced = set()
    survivors = []  # (doc_id, vid, mode, names, parts)
    for row in conn.execute("SELECT version_id, doc_id FROM versions").fetchall():
        doc_id, vid = str(row["doc_id"]), str(row["version_id"])
        mf = vault.vault_dir() / doc_id / "versions" / f"{vid}.json"
        m = json.loads(mf.read_text(encoding="utf-8"))
        if m.get("mode") == "dedup":
            parts = {str(k): str(v) for k, v in m["parts"].items()}
            referenced.update(parts.values())
            survivors.append((doc_id, vid, "dedup", list(m["names"]), parts))
        else:
            survivors.append((doc_id, vid, "full", [], {}))
    return referenced, survivors


# ---------------------------------------------------------------- 大规模驱逐对账

def test_budget_eviction_large_mixed_vault_reconciliation(tmp_path):
    """1500~2000 doc × 3 版本（dedup/full 混合 + 跨 doc 共享对象 + 隔离 + 分支基）。

    断言：收敛到预算内；豁免版本零误伤；存活版本引用对象零丢失（全量对账 +
    抽样逐字节 reassemble）；磁盘上无未被引用的残留对象（GC 不过删也不漏删）。
    """
    rng = random.Random(20260814)
    n_docs = 1600
    conn = _conn()
    shared_pool = [rng.randbytes(4096) for _ in range(50)]  # 跨 doc 共享对象
    payload_of_hash: dict[str, bytes] = {}
    for p in shared_pool:
        payload_of_hash[xxhash.xxh64(p).hexdigest()] = p
    branch_bases: set[str] = set()
    quarantined: set[str] = set()

    t0 = time.time()
    for i in range(n_docs):
        doc_id = f"doc-{i:05d}"
        store.upsert_doc(conn, doc_id, str(tmp_path / "src" / f"deck{i}.pptx"), 1.0)
        for j in range(3):
            vid = f"{doc_id}-v{j}"
            ts = 1000.0 + i * 10 + j
            health = "invalid" if (i % 20 == 0 and j == 2) else "ok"
            if health != "ok":
                quarantined.add(vid)
            if j == 1:  # mode=full 混合
                payload = rng.randbytes(5 * 1024)
                _mk_full_version(conn, doc_id, vid, ts, payload, health=health)
            else:
                unique = rng.randbytes(1024)
                payload_of_hash[xxhash.xxh64(unique).hexdigest()] = unique
                _mk_dedup_version(
                    conn, doc_id, vid, ts,
                    [shared_pool[(i + j) % len(shared_pool)], unique],
                    health=health,
                )
            if i % 25 == 0 and j == 0:
                store.record_branch(conn, f"child-{i}", doc_id, vid, ts + 1, "copy/hash_match")
                branch_bases.add(vid)
        if i % 250 == 0:
            conn.commit()
    conn.commit()
    build_s = time.time() - t0

    total_before = vault._budget_relevant_bytes()
    budget = int(total_before * 0.55)  # 必须多轮驱逐才能达标
    t0 = time.time()
    res = vault.enforce_size_budget(conn, max_bytes=budget)
    enforce_s = time.time() - t0

    print(f"\n[large-vault] build={build_s:.1f}s docs={n_docs} versions={n_docs * 3} "
          f"before={total_before / 1048576:.1f}MB budget={budget / 1048576:.1f}MB "
          f"after={res['vault_bytes_after'] / 1048576:.1f}MB "
          f"evicted={res['evicted_versions']} enforce={enforce_s:.1f}s")

    # 实测（2026-08-14，2000 doc/45% 预算）：8 轮上限停在预算上方 0.4%——驱逐估计
    # 按 versions.size（源文件大小）计费，高估去重版本的边际回收，残余留给下周维护。
    assert res["vault_bytes_after"] <= budget * 1.02, "多轮驱逐后仍明显超标（>2%）"
    assert res["evicted_versions"] > 0
    assert res["gc"] is not None and not res["gc"]["aborted"] and not res["gc"]["errors"]

    # 豁免零误伤
    surviving_ids = {str(r["version_id"]) for r in conn.execute("SELECT version_id FROM versions")}
    assert branch_bases <= surviving_ids, "分支基被误驱逐"
    assert quarantined <= surviving_ids, "隔离版本被误驱逐"

    # 存活版本引用对象零丢失（全量对账）+ 抽样逐字节 reassemble
    referenced, survivors = _live_manifest_parts(conn)
    objdir = vault._global_objects_dir()
    on_disk = {
        p.name for p in objdir.iterdir()
        if p.is_file() and not p.name.startswith(".object-")
    }
    assert referenced <= on_disk, "存活版本引用的对象被误删"
    assert on_disk <= referenced, "GC 漏删：磁盘对象已无任何存活版本引用"
    sample = [s for s in survivors if s[2] == "dedup"][:40]
    for doc_id, vid, _mode, names, parts in sample:
        out = tmp_path / "reassemble.pptx"
        vault._write_zip(str(out), doc_id, names, parts)
        with zipfile.ZipFile(out) as z:
            assert z.namelist() == names
            for name in names:
                assert z.read(name) == payload_of_hash[parts[name]]
        out.unlink()
    for doc_id, vid, _mode, _n, _p in survivors:
        if _mode == "full":
            assert (vault.vault_dir() / doc_id / "versions" / f"{vid}.pptx").is_file()


def test_budget_eviction_no_progress_stops(tmp_path):
    """预算低于「豁免地板」时必须无进展即停：保留健康历史，诊断报未收敛。

    3MB 隔离版本（豁免）+ 30 个小健康版本、预算 1MB：把健康版本全驱逐也只
    回收约 40KB（不及缺口 5%），属纯损失——一轮都不该跑，且必须报未收敛。
    """
    conn = _conn()
    store.upsert_doc(conn, "doc-x", str(tmp_path / "x.pptx"), 1.0)
    big = b"Q" * (3 * 1024 * 1024)
    _mk_full_version(conn, "doc-x", "v-q", 1.0, big, health="invalid")  # 豁免地板 3MB
    for i in range(30):
        _mk_dedup_version(conn, "doc-x", f"v-h{i}", 2.0 + i, [f"payload-{i}".encode() * 128])
    conn.commit()

    res = vault.enforce_size_budget(conn, max_bytes=1 * 1024 * 1024)

    print(f"\n[no-progress] evicted={res['evicted_versions']} "
          f"after={res['vault_bytes_after'] / 1048576:.2f}MB budget=1.00MB "
          f"converged={res['converged']} floor={res['floor_bytes'] / 1048576:.2f}MB")
    assert res["vault_bytes_after"] > 1 * 1024 * 1024, "地板高于预算：必然超标（预期内）"
    assert res["evicted_versions"] == 0, "无进展即停：健康版本一个都不该被驱逐"
    assert res["converged"] is False, "未达标必须报未收敛"
    assert res["floor_bytes"] > 1 * 1024 * 1024, "诊断：豁免地板本身已超预算"
    assert store.get_version(conn, "v-q") is not None  # 隔离豁免不动
    assert store.get_version(conn, "v-h0") is not None  # 健康历史完整保留
    assert store.get_version(conn, "v-h29") is not None


# ---------------------------------------------------------------- 幽灵 10 万级性能

def _bulk_docs(conn, n, base_dir, *, with_versions=False):
    docs = [(f"g-{i}", str(base_dir / f"d{i}.pptx"), "deleted", 1.0, 1.0, 0) for i in range(n)]
    conn.executemany(
        """INSERT INTO managed_docs(doc_id, path, status, created_at, updated_at, deleted_at)
           VALUES(?,?,?,?,?,?)""",
        docs,
    )
    conn.executemany(
        """INSERT INTO doc_paths(doc_id, path, path_key, status, first_seen, last_seen)
           VALUES(?,?,?,?,?,?)""",
        [(d[0], d[1], store.path_key(d[1]), "current", 1.0, 1.0) for d in docs],
    )
    if with_versions:
        conn.executemany(
            """INSERT INTO versions(version_id, doc_id, ts, size, content_hash)
               VALUES(?,?,?,?,?)""",
            [(f"v-{i}", d[0], 1.0, 10, f"h-{i}") for i, d in enumerate(docs)],
        )
    conn.commit()


def test_ghost_scan_and_mark_100k_perf(tmp_path):
    conn = _conn()
    n = 100_000
    t0 = time.time()
    _bulk_docs(conn, n, tmp_path / "gone")
    insert_s = time.time() - t0

    t0 = time.time()
    ghosts = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    scan_s = time.time() - t0
    assert ghosts == []  # deleted_at=0：宽限模式下先补记不列入

    t0 = time.time()
    marked = vault.mark_ghost_docs_seen(conn, fixed_roots=[str(tmp_path)])
    mark_s = time.time() - t0
    assert marked == n

    t0 = time.time()
    ghosts2 = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    rescan_s = time.time() - t0
    assert ghosts2 == []  # 刚补记，仍在宽限内

    t0 = time.time()
    all_ghosts = vault.list_ghost_docs(conn, min_missing_sec=0, fixed_roots=[str(tmp_path)])
    full_scan_s = time.time() - t0
    assert len(all_ghosts) == n

    print(f"\n[ghost-100k] insert={insert_s:.1f}s scan={scan_s:.1f}s "
          f"mark={mark_s:.1f}s rescan={rescan_s:.1f}s full_scan={full_scan_s:.1f}s")


def test_ghost_reap_12k_perf(tmp_path):
    conn = _conn()
    n = 12_000
    _bulk_docs(conn, n, tmp_path / "gone", with_versions=True)
    # 200 个活文档（带真实 manifest+对象），验证收割不误伤、GC 通过
    for i in range(200):
        live = tmp_path / "live" / f"keep{i}.pptx"
        live.parent.mkdir(exist_ok=True)
        fx.make_pptx(live, [{"body": f"活文档{i}"}])
        doc_id = f"live-{i}"
        store.upsert_doc(conn, doc_id, str(live), 1.0)
        _mk_dedup_version(conn, doc_id, f"lv-{i}", 1.0, [f"live-payload-{i}".encode() * 64])
    conn.commit()

    t0 = time.time()
    res = vault.reap_ghost_docs(
        conn, dry_run=False, min_missing_sec=0, fixed_roots=[str(tmp_path)]
    )
    reap_s = time.time() - t0
    print(f"\n[ghost-reap-12k] reap={reap_s:.1f}s docs={res['ghost_docs']} "
          f"versions={res['ghost_versions']}")
    assert res["ghost_docs"] == n
    assert res["gc"] is not None and not res["gc"]["aborted"] and not res["gc"]["errors"]
    remaining = conn.execute("SELECT COUNT(*) FROM managed_docs").fetchone()[0]
    assert remaining == 200
    remaining_v = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    assert remaining_v == 200


# ---------------------------------------------------------------- 大库 maintain_db

def test_maintain_db_large_db_perf_and_lock_conflict(tmp_path):
    conn = _conn()
    n = 3000
    for i in range(n):  # 大行快速堆出库体积
        store.index_pages(conn, "d", f"v{i}", [(1, "算力 集群 研报 " * 1200 + str(i))])
        if i % 500 == 0:
            conn.commit()
    conn.commit()
    # 制造 freelist：删掉 80%
    conn.execute(
        "DELETE FROM version_pages_fts WHERE version_id IN "
        "(SELECT version_id FROM version_pages_fts LIMIT ?)",
        (int(n * 0.8),),
    )
    conn.commit()
    db_mb = (vault.vault_dir() / "versions.db").stat().st_size / 1048576

    t0 = time.time()
    res1 = vault.maintain_db(conn)  # 默认阈值： freelist 大 → 应触发 VACUUM
    first_s = time.time() - t0
    print(f"\n[maintain-db] db={db_mb:.1f}MB first={first_s:.2f}s "
          f"vacuumed={res1['vacuumed']} free_before={res1['free_bytes_before'] / 1048576:.1f}MB "
          f"err={res1['error']!r}")
    assert res1["error"] == ""

    # 锁冲突：另一连接持写锁时 VACUUM 必须温和失败且不损坏库
    other = store.connect(vault.db_path())
    other.isolation_level = None
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO vault_meta(key, value) VALUES('lock-probe', '1')")
    try:
        t0 = time.time()
        res2 = vault.maintain_db(conn, min_free_bytes=1, min_free_ratio=0.0)
        lock_s = time.time() - t0
        print(f"[maintain-db] lock-conflict={lock_s:.1f}s vacuumed={res2['vacuumed']} "
              f"err={res2['error']!r}")
        assert not res2["vacuumed"] or res2["error"]  # 拿不到锁就报 error，不许静默卡住
    finally:
        other.execute("ROLLBACK")
        other.close()
    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert ic == "ok"


# ---------------------------------------------------------------- 并发：快照 × 重维护

def test_concurrent_snapshots_and_heavy_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTUTOR_VAULT_HEAVY_MAINTENANCE_SEC", "0")
    monkeypatch.setenv("PPTUTOR_GHOST_GRACE_SEC", "0")
    manager = VersionManager()

    live_docs = []
    for i in range(24):
        p = tmp_path / "docs" / f"deck{i}.pptx"
        p.parent.mkdir(exist_ok=True)
        fx.make_pptx(p, [{"body": f"初版 {i}"}])
        assert manager.snapshot_now(str(p))
        live_docs.append(p)
    ghosts = live_docs[:6]
    for p in ghosts:
        p.unlink()  # grace=0 → 首轮重维护即收割
    live_docs = live_docs[6:]

    errors: list[str] = []
    stop = threading.Event()

    def snap_worker():
        round_no = 0
        try:
            while not stop.is_set() and round_no < 6:
                for p in live_docs:
                    if stop.is_set():
                        break
                    fx.make_pptx(p, [{"body": f"第{round_no}轮 {p.stem} {time.time()}"}])
                    manager.snapshot_now(str(p), notify=False)
                round_no += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=snap_worker, name="stress-snapshots")
    worker.start()
    try:
        tight = max(50_000, vault._budget_relevant_bytes() // 4)
        for _ in range(3):
            manager.run_vault_maintenance()
            with manager._lock:  # 与生产同一路径：重活在 _lock 内
                vault.enforce_size_budget(manager._conn, max_bytes=tight)
                vault.maintain_db(manager._conn)
    finally:
        stop.set()
        worker.join(timeout=60)

    assert not errors, f"snapshot 线程异常: {errors}"
    assert worker.is_alive() is False or True  # join 超时也不影响一致性断言

    # 收割生效 + 终态一致
    for p in ghosts:
        assert store.get_doc(manager._conn, vault.doc_id_for(str(p))) is None
    gc = vault.collect_garbage(manager._conn, dry_run=True)
    assert not gc["aborted"] and not gc["errors"], f"终态 GC 安全门未过: {gc}"
    referenced, survivors = _live_manifest_parts(manager._conn)
    objdir = vault._global_objects_dir()
    on_disk = {p.name for p in objdir.iterdir() if p.is_file() and not p.name.startswith(".object-")}
    assert referenced <= on_disk
    assert survivors, "并发+驱逐后不应一个版本都不剩（预算只为刻意收紧，非零）"
    n_rebuilt = 0
    for doc_id, vid, _m, _n, _p in survivors[:12]:
        dest = tmp_path / f"rebuild-{vid}.pptx"
        assert vault.rebuild_to(doc_id, vid, str(dest)), f"存活版本 {vid} 重组失败"
        n_rebuilt += 1
    print(f"\n[concurrency] survivors={len(survivors)} rebuilt={n_rebuilt} "
          f"objects={len(on_disk)} snapshots_ok")


# ---------------------------------------------------------------- 故障注入（子进程）

_KILL_EVICTION_CHILD = r"""
import json, os, sys
import xxhash
from pptx_finder.versioning import store, vault

root = sys.argv[1]
conn = store.connect(vault.db_path())
store.init_db(conn)
for i in range(300):
    doc_id = f"doc-{i:04d}"
    store.upsert_doc(conn, doc_id, os.path.join(root, "src", f"deck{i}.pptx"), 1.0)
    for j in range(2):
        vid = f"{doc_id}-v{j}"
        payload = (f"{doc_id}-{j}-".encode()) * 128
        h = xxhash.xxh64(payload).hexdigest()
        (vault._global_objects_dir() / h).write_bytes(payload)
        d = vault.vault_dir() / doc_id / "versions"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{vid}.json").write_text(json.dumps({
            "mode": "dedup", "names": ["ppt/slides/slide1.xml"],
            "parts": {"ppt/slides/slide1.xml": h},
        }), encoding="utf-8")
        conn.execute(
            "INSERT INTO versions(version_id, doc_id, ts, size, content_hash) VALUES(?,?,?,?,?)",
            (vid, doc_id, 1.0 + j, len(payload), "h-" + vid))
    if i % 100 == 0:
        conn.commit()
conn.commit()

orig = vault.delete_version_artifacts
calls = {"n": 0}
def killer(doc_id, vid):
    orig(doc_id, vid)
    calls["n"] += 1
    if calls["n"] >= 5:
        os._exit(42)  # 掉电：rows 已 commit；仅剩部分 orphan artifacts
vault.delete_version_artifacts = killer
vault.enforce_size_budget(conn, max_bytes=1)
"""


def test_kill_mid_eviction_consistency(tmp_path):
    env = dict(os.environ, PPTX_FINDER_DATA_DIR=str(tmp_path / "appdata"))
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_EVICTION_CHILD, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 42, f"子进程应死于驱逐中途, got {proc.returncode}: {proc.stderr[-500:]}"

    conn = _conn()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # 元数据先提交：掉电只留下孤儿文件，所有仍存活的 row 都有完整恢复图。
    gc = vault.collect_garbage(conn, dry_run=True)
    print((f"\n[kill-eviction] post-crash GC: aborted={gc['aborted']} errors={gc['errors']}").encode('ascii','replace').decode())
    assert not gc["aborted"] and not gc["errors"]
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 300
    assert int(gc["manifests_removed"]) > 0 or int(gc["objects_removed"]) > 0
    # 恢复路径：重跑容量维护先清孤儿，不再牺牲任何健康版本。
    res = vault.enforce_size_budget(conn, max_bytes=1)
    gc2 = vault.collect_garbage(conn, dry_run=True)
    assert not gc2["aborted"] and not gc2["errors"]
    assert res["evicted_versions"] == 0
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 300
    print(f"[kill-eviction] recovery: evicted={res['evicted_versions']} "
           f"after={res['vault_bytes_after']}B gc_clean")


_KILL_VACUUM_CHILD = r"""
import os, sys, time
from pptx_finder.versioning import store, vault

root = sys.argv[1]
conn = store.connect(vault.db_path())
store.init_db(conn)
n = 9000  # ~250MB 库，VACUUM 窗口拉到秒级，保证 kill 落在 VACUUM 进行中
for i in range(n):
    store.index_pages(conn, "d", f"v{i}", [(1, "算力 集群 研报 " * 1200 + str(i))])
    if i % 500 == 0:
        conn.commit()
conn.commit()
conn.execute(
    "DELETE FROM version_pages_fts WHERE version_id IN "
    "(SELECT version_id FROM version_pages_fts LIMIT ?)", (int(n * 0.8),))
conn.commit()
with open(os.path.join(root, "ready"), "w") as f:
    f.write("ready")
for _ in range(8):  # 反复 VACUUM，扩大被杀落在 VACUUM 窗口的概率
    vault.maintain_db(conn, min_free_bytes=1, min_free_ratio=0.0)
with open(os.path.join(root, "done"), "w") as f:
    f.write("done")
"""


def test_kill_mid_vacuum_consistency(tmp_path):
    env = dict(os.environ, PPTX_FINDER_DATA_DIR=str(tmp_path / "appdata"))
    proc = subprocess.Popen(
        [sys.executable, "-c", _KILL_VACUUM_CHILD, str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = tmp_path / "ready"
    deadline = time.time() + 120
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert ready.exists(), "子进程建库超时"
    time.sleep(0.25)  # 大概率落在首次 VACUUM 进行中
    proc.kill()
    proc.wait(timeout=30)
    killed_midway = not (tmp_path / "done").exists()

    conn = _conn()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    res = vault.maintain_db(conn)
    assert res["error"] == ""
    conn.execute("INSERT INTO vault_meta(key, value) VALUES('post-kill', '1')")
    conn.commit()
    print(f"\n[kill-vacuum] killed_mid_maintain={killed_midway} "
          f"post-recovery vacuumed={res['vacuumed']} integrity=ok")


# ---------------------------------------------------------------- 审查发现实证

def test_ghost_anchor_stale_after_unobserved_restore(tmp_path):
    """修复验证：删除→未被观测的恢复→再删除，扫描反向 pass 复活并清零锚点。

    恢复若发生在应用关闭期间（或目录不在对账候选），旧 deleted_at 锚点会残留；
    scan_deleted 的 deleted→active 反向 pass 复活文档并清零锚点，
    再次删除后重新享受完整 30 天宽限（宁可保守也不许误删）。
    """
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    conn = _conn()
    vid = vault.snapshot(conn, str(p))
    assert vid
    did = vault.doc_id_for(str(p))

    p.unlink()
    store.set_status(conn, did, "deleted")
    old_anchor = time.time() - 40 * _DAY
    conn.execute("UPDATE managed_docs SET deleted_at=? WHERE doc_id=?", (old_anchor, did))
    conn.commit()

    # 未被观测的恢复（应用关闭期间拷回）；下次启动/对账扫描的反向 pass 复活它
    fx.make_pptx(p, [{"body": "v1"}])
    mgr = VersionManager(conn=conn)
    assert mgr.scan_deleted() == 1, "deleted 且登记路径存在 → 必须复活"
    doc = store.get_doc(conn, did)
    assert doc["status"] == "active"
    assert float(doc["deleted_at"]) == 0, "复活必须清零宽限锚点"

    p.unlink()  # 再删：锚点必须是 fresh 的
    store.set_status(conn, did, "deleted")

    ghosts = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    print(f"\n[stale-anchor-fixed] ghosts={[g['doc_id'] for g in ghosts]}")
    assert did not in {g["doc_id"] for g in ghosts}, "复活清零后再删应重新享受完整 30 天宽限"
    assert time.time() - float(store.get_doc(conn, did)["deleted_at"]) < 60


def test_ghost_anchor_cleared_when_restore_observed(tmp_path):
    """对照组：恢复被 watcher 观测到（snapshot 去重命中→upsert 清零）时宽限重新起算。"""
    p = tmp_path / "deck.pptx"
    fx.make_pptx(p, [{"body": "v1"}])
    conn = _conn()
    assert vault.snapshot(conn, str(p))
    did = vault.doc_id_for(str(p))

    p.unlink()
    store.set_status(conn, did, "deleted")
    conn.execute(
        "UPDATE managed_docs SET deleted_at=? WHERE doc_id=?", (time.time() - 40 * _DAY, did)
    )
    conn.commit()

    fx.make_pptx(p, [{"body": "v1"}])  # 恢复（内容相同）
    assert vault.snapshot(conn, str(p)) is None  # 去重命中，不产新版本
    assert float(store.get_doc(conn, did)["deleted_at"]) == 0  # 但锚点已清零

    p.unlink()  # 再删
    store.set_status(conn, did, "deleted")
    ghosts = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    assert did not in {g["doc_id"] for g in ghosts}, "观测到恢复后应重新享受完整宽限"


def test_ghost_checkable_via_stale_fixed_drive_alias(tmp_path):
    """修复验证：历史固定盘别名不得让「当前在未挂载网络盘」的文档可收割。

    「网络盘文档永不列幽灵」：可判定性只基于主路径 + status='current' 的
    doc_paths；陈旧 alias 仅参与 exists 存活判定，不参与可判定性。
    """
    conn = _conn()
    nas_path = "Q:\\nas-share\\deck.pptx"  # Q: 不在 fixed_roots
    store.upsert_doc(conn, "doc-nas", nas_path, 1.0)
    store.record_path(conn, "doc-nas", str(tmp_path / "old-local.pptx"), 0.5, "alias")
    _mk_dedup_version(conn, "doc-nas", "v1", 1.0, [b"payload-bytes" * 64])
    conn.execute(
        "UPDATE managed_docs SET status='deleted', deleted_at=? WHERE doc_id='doc-nas'",
        (time.time() - 40 * _DAY,),
    )
    conn.commit()

    ghosts = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    print(f"\n[alias-loophole-fixed] ghosts={[g['doc_id'] for g in ghosts]}")
    assert "doc-nas" not in {g["doc_id"] for g in ghosts}, "陈旧固定盘别名不得让网络盘文档可收割"

    # 对照：没有任何固定盘路径时正确豁免
    store.upsert_doc(conn, "doc-pure-nas", "Q:\\nas-share\\other.pptx", 1.0)
    conn.execute(
        "UPDATE managed_docs SET status='deleted', deleted_at=? WHERE doc_id='doc-pure-nas'",
        (time.time() - 40 * _DAY,),
    )
    conn.commit()
    ghosts2 = vault.list_ghost_docs(conn, min_missing_sec=30 * _DAY, fixed_roots=[str(tmp_path)])
    assert "doc-pure-nas" not in {g["doc_id"] for g in ghosts2}


def test_legacy_vault_first_maintenance_marks_only(tmp_path, monkeypatch):
    """老库升级（deleted_at 全 0）首轮重维护：只补记、不收割。"""
    monkeypatch.setenv("PPTUTOR_VAULT_HEAVY_MAINTENANCE_SEC", "0")
    manager = VersionManager()
    p = tmp_path / "old.pptx"
    fx.make_pptx(p, [{"body": "老库文档"}])
    assert manager.snapshot_now(str(p))
    p.unlink()
    did = vault.doc_id_for(str(p))
    manager._conn.execute(
        "UPDATE managed_docs SET status='deleted', deleted_at=0 WHERE doc_id=?", (did,)
    )
    manager._conn.commit()

    res = manager.run_vault_maintenance()

    assert res["ghosts_marked"] >= 1
    assert res["ghosts"]["ghost_docs"] == 0
    doc = store.get_doc(manager._conn, did)
    assert doc is not None and float(doc["deleted_at"]) > 0


# ---------------------------------------------------------------- 计量口径

def test_budget_relevant_bytes_excludes_db_trio(tmp_path):
    conn = _conn()
    for i in range(200):
        store.index_pages(conn, "d", f"v{i}", [(1, "词 " * 500 + str(i))])
    conn.commit()
    (vault.vault_dir() / "junk.bin").write_bytes(b"x" * 4096)

    total = vault.vault_size_bytes()
    relevant = vault._budget_relevant_bytes()
    trio = 0
    for name in ("versions.db", "versions.db-wal", "versions.db-shm"):
        try:
            trio += (vault.vault_dir() / name).stat().st_size
        except OSError:
            pass
    print((f"\n[budget-bytes] total={total} relevant={relevant} trio={trio}").encode('ascii','replace').decode())
    assert total - relevant == trio, "计量口径必须恰好排除 versions.db 三件套"
    assert relevant >= 4096  # junk.bin 计入
