"""自定义索引根 + UNC：对抗性压测（代码审查补充用例）。

对应审查要点：
1. validate_index_root 对不可达 UNC 的探测耗时分布（UI 线程影响定量）
2. 50 个根（嵌套/重复形态/不可达混合）下 _resolve_index_roots 合并去重正确性
3. 嵌套 + 重复根的 os.walk 扫描放大量、update_index 幂等性、FTS 重复行证据
4. 离线根删除通道保护：根目录消失 → 行保留；根恢复但文件真没了 → 正常删除
5. UNC 根离线时已登记行不被误删（模拟网络盘断连后重扫）
6. 设置对话框「保存」的异步性：validate 慢时 UI 线程必须立即返回

纪律：只用 tmp_path 临时目录与不可达 UNC（127.0.0.1 不存在共享 / .invalid 保留域），
不索引真实磁盘与真实网络路径；PPTX_FINDER_DATA_DIR 由 fixture 隔离。
"""
from __future__ import annotations

import os
import shutil
import time
import uuid

import pytest

from pptx_finder import config, db, indexer, search
import pptx_finder.scanner as scanner_mod
import pptx_finder.ui.settings_dialog as settings_dialog_mod
from pptx_finder.scanner import iter_ppt_files
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.delenv("PPTX_FINDER_ROOTS", raising=False)


@pytest.fixture
def mgr():
    m = VersionManager()
    yield m
    m.stop()


def _conn(tmp_path, name="i.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


# ---------- 1. 不可达 UNC 校验耗时分布 ----------

def test_validate_unreachable_unc_latency_distribution(tmp_path):
    """validate_index_root 对两类不可达 UNC + 不存在本地目录的耗时分布。

    任何单次探测都不得越过单用例时间预算（30s）；实测分布打印出来供报告引用。
    loopback 拒绝（TCP RST）与不存在主机名（DNS NXDOMAIN）是 Windows 上两条
    完全不同的失败路径，分别计时。
    """
    cases: list[tuple[str, str]] = []
    for i in range(3):  # loopback 上肯定不存在的共享：连接被拒
        cases.append(("loopback-noshare", rf"\\127.0.0.1\pptx_finder_no_such_{i}_9z"))
    for _ in range(2):  # 不存在主机名：.invalid 保留域 + 随机名，绕开 DNS 负缓存
        host = f"pptx-finder-{uuid.uuid4().hex[:12]}.invalid"
        cases.append(("no-such-host", rf"\\{host}\share"))
    for i in range(3):  # 本地不存在目录：对照组
        cases.append(("missing-local-dir", str(tmp_path / f"no-such-{i}")))

    timings: dict[str, list[float]] = {}
    for label, path in cases:
        t0 = time.perf_counter()
        ok, reachable, msg = config.validate_index_root(path)
        dt = time.perf_counter() - t0
        timings.setdefault(label, []).append(dt)
        assert dt < 30.0, f"{label} 单次探测 {dt:.2f}s 越过用例预算"
        if label == "missing-local-dir":
            assert (ok, reachable) == (True, False)  # 本地合法路径不可达 → 第三态
        else:
            assert (ok, reachable) == (True, False)  # 不可达 UNC → 警告但允许保存
            assert "不可达" in msg

    for label, samples in timings.items():
        pretty = ", ".join(f"{s * 1000:.1f}ms" for s in samples)
        print(f"\n[validate-latency] {label}: {pretty}")  # ASCII-only: console is cp1252


# ---------- 2. 50 根混合下 _resolve_index_roots 合并正确性 ----------

def test_resolve_merges_50_mixed_roots(monkeypatch, tmp_path):
    """30 个自定义根（嵌套/尾分隔符/大小写/斜杠变体/不可达 UNC 混合）
    + 2 个固定盘 + 20 个 env 根，验证合并顺序与去重口径。

    口径（修复后固定为预期）：
    - 自定义根与 env 根同口径清洗（去空白/尾分隔符/normcase 去重）；
    - env 的尾分隔符变体清洗后与自定义根 normcase 同 key → 合并去重，不再重复扫描。
    """
    base = tmp_path / "merged"
    sub = base / "sub"
    sub.mkdir(parents=True)
    unc = r"\\127.0.0.1\pptx_finder_stress_share"
    unc_shares = [rf"\\127.0.0.1\pptx_finder_stress_{i:02d}" for i in range(20)]

    custom_raw = [
        str(base),
        str(base) + "\\",               # 尾分隔符 → 清洗去重
        str(base).replace("\\", "/"),   # 斜杠变体 → normcase 去重
        str(base).upper(),              # 大小写变体 → normcase 去重
        str(sub),                       # 嵌套根：保留（合并不做覆盖去重）
        unc,
        unc + "\\",                     # UNC 尾分隔符 → 清洗去重
        "  " + unc + "  ",              # 空白包裹 → 清洗去重
        "",
        "   ",
        *unc_shares,                    # 20 个不可达 UNC：resolve 不做 I/O，必须快
    ]
    assert len(custom_raw) == 30
    config.set_index_roots(custom_raw)
    custom_cleaned = [str(base), str(sub), unc, *unc_shares]
    assert list(config.get_index_roots()) == custom_cleaned

    monkeypatch.setattr(scanner_mod, "fixed_drives", lambda: ["C:\\", "D:\\"])
    env_distinct = [f"E:\\env{i:02d}" for i in range(18)]
    env_trailing_dup = str(base).upper() + "\\"  # 尾分隔符变体：与自定义根不同 key
    env_case_dup = "e:\\env00"                   # 与 env_distinct[0] 同 key
    monkeypatch.setenv(
        "PPTX_FINDER_ROOTS",
        os.pathsep.join([*env_distinct, env_trailing_dup, env_case_dup]),
    )

    t0 = time.perf_counter()
    resolved = MainWindow._resolve_index_roots(None)
    resolve_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[resolve-50-roots] {len(resolved)} roots, {resolve_ms:.2f}ms (must be I/O-free)")

    expected = (
        custom_cleaned
        + ["C:\\", "D:\\"]
        + env_distinct
        # env 尾分隔符变体清洗后与自定义根 normcase 同 key → 被合并去重
    )
    assert resolved == expected
    assert resolve_ms < 1000, "resolve 应纯字符串操作，任何 isdir 都是回归"
    # 无完全相同的字符串重复；env 大小写变体也被清洗去重
    assert len(resolved) == len(set(resolved))
    assert sum(1 for r in resolved if os.path.normcase(r) == "e:\\env00") == 1


# ---------- 3. 嵌套/重复根：扫描放大量与索引幂等性 ----------

def test_nested_and_duplicate_roots_scan_amplification(tmp_path):
    base = tmp_path / "tree"
    sub = base / "sub"
    sub.mkdir(parents=True)
    for name in ("a1.dat", "a2.dat"):
        (base / name).write_text("x", encoding="utf-8")
    (sub / "b1.dat").write_text("x", encoding="utf-8")
    unique_files = 3

    def count(roots):
        return sum(1 for _ in iter_ppt_files(roots, inventory_all=True))

    single = count([str(base)])
    nested = count([str(base), str(sub)])
    dup = count([str(base), str(base)])
    both = count([str(base), str(sub), str(base)])
    print(
        f"\n[scan-amplification] single={single} nested={nested} "
        f"dup={dup} nested+dup={both} (unique={unique_files})"
    )
    assert single == unique_files
    # 嵌套根：sub 子树被走两遍；重复根：整棵树走两遍。iter_ppt_files 无跨根去重。
    assert nested == unique_files + 1
    assert dup == unique_files * 2
    assert both == unique_files * 2 + 1

    # update_index 层：同一批根喂进去，files 表仍应幂等（ON CONFLICT  upsert）
    conn = _conn(tmp_path)
    kw = dict(workers=1, supported_exts=(".pptx",), index_all_files=True)
    indexer.update_index(conn, [str(base), str(sub), str(base)], **kw)
    file_rows = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    fts_rows = conn.execute("SELECT COUNT(*) FROM file_names_fts").fetchone()[0]
    print(f"[scan-amplification] files={file_rows} file_names_fts={fts_rows}")
    assert file_rows == unique_files  # 结果幂等：重复 walk 不产生重复文件行

    # 修复后：扫描循环按 seen 去重（跨根重复枚举直接跳过）+ 批量写按 path/file_id
    # 去重，FTS 与 files 严格一一对应，不再膨胀
    assert fts_rows == file_rows
    hits = search.search(conn, "b1", exts=None)
    assert len([h for h in hits if h.name == "b1.dat"]) == 1  # 搜索结果无重复

    # 第二轮重扫：未变快筛命中，放大量不再产生新写入
    again = indexer.update_index(conn, [str(base), str(sub), str(base)], **kw)
    assert again["filename_only"] == 0
    conn.close()


# ---------- 4. 离线根删除通道保护（本地目录模拟） ----------

def test_offline_root_delete_channel_protection(tmp_path):
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    root_a.mkdir()
    root_b.mkdir()
    for i in range(3):
        (root_a / f"a{i}.dat").write_text("x", encoding="utf-8")
    for i in range(2):
        (root_b / f"b{i}.dat").write_text("x", encoding="utf-8")

    conn = _conn(tmp_path)
    kw = dict(workers=1, supported_exts=(".pptx",), index_all_files=True)
    indexer.update_index(conn, [str(root_a), str(root_b)], **kw)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5

    # A 根整体消失（模拟离线/断连）；B 里真删一个文件（对照：在线根删除通道仍工作）
    shutil.rmtree(root_a)
    (root_b / "b0.dat").unlink()
    summary = indexer.update_index(conn, [str(root_a), str(root_b)], **kw)
    assert summary["deleted"] == 1  # 只有在线根 B 里真删的那个
    names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert names == {"a0.dat", "a1.dat", "a2.dat", "b1.dat"}  # A 的行全部保留

    # A 恢复为空目录（文件确实没了）→ 重扫正常删除
    root_a.mkdir()
    summary2 = indexer.update_index(conn, [str(root_a), str(root_b)], **kw)
    assert summary2["deleted"] == 3
    names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert names == {"b1.dat"}
    conn.close()


# ---------- 5. UNC 根离线时已登记行不被误删 ----------

def test_offline_unc_root_rows_survive_rescan(tmp_path):
    unc = r"\\127.0.0.1\pptx_finder_offline_share_9z"
    local = tmp_path / "local"
    local.mkdir()
    (local / "x.dat").write_text("x", encoding="utf-8")

    conn = _conn(tmp_path)
    unc_file = unc + "\\deck.pptx"
    db.upsert_file(
        conn,
        path=unc_file,
        name="deck.pptx",
        ext=".pptx",
        size=10,
        mtime=1.0,
        content_hash="size:10",
        page_count=0,
        status="filename_only",
        error="",
        indexed_at=time.time(),
    )
    conn.commit()

    t0 = time.perf_counter()
    summary = indexer.update_index(
        conn,
        [unc, str(local)],
        workers=1,
        supported_exts=(".pptx",),
        index_all_files=True,
    )
    dt = time.perf_counter() - t0
    print(f"\n[unc-offline-rescan] update_index total {dt * 1000:.1f}ms (incl. UNC probe)")

    # available_roots 过滤：UNC 离线 → 其下已登记行不进入删除通道
    assert db.get_file_by_path(conn, unc_file) is not None
    assert summary["deleted"] == 0
    # 本地根照常索引
    assert db.get_file_by_path(conn, str(local / "x.dat")) is not None
    conn.close()


# ---------- 6. 设置对话框：保存必须异步，不阻塞 UI 线程 ----------

def _slow_validate_stub(path: str):
    """模拟 SMB 超时期间的 validate：睡眠 1.2s 后返回第三态。"""
    time.sleep(1.2)
    return True, False, "当前不可达：保存后将在其可用时自动纳入索引"


def test_settings_save_is_async_when_validate_slow(qtbot, mgr, monkeypatch):
    """validate 卡在 SMB 超时时，_apply_index_roots 必须立即返回（后台校验）。"""
    monkeypatch.setattr(settings_dialog_mod, "validate_index_root", _slow_validate_stub)
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.index_root_edit.setText(r"\\127.0.0.1\pptx_finder_async_share")
    dlg._add_index_root_network()

    t0 = time.perf_counter()
    dlg._apply_index_roots()
    call_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[settings-save-async] _apply_index_roots returned in {call_ms:.1f}ms")
    assert call_ms < 500, f"保存调用阻塞 UI 线程 {call_ms:.0f}ms（validate 应放后台）"
    assert not dlg._index_roots_save.isEnabled()  # 校验期间防重入

    qtbot.waitUntil(lambda: "已保存" in dlg._index_roots_result.text(), timeout=5000)
    assert config.get_index_roots() == (r"\\127.0.0.1\pptx_finder_async_share",)
    assert dlg._index_roots_save.isEnabled()


def test_settings_inflight_disables_root_editing(qtbot, mgr, monkeypatch):
    """校验在途期间添加/删除/输入框全部禁用——在途编辑会被校验完成后的
    列表重建静默覆盖，修复后直接从交互上杜绝。"""
    monkeypatch.setattr(settings_dialog_mod, "validate_index_root", _slow_validate_stub)
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.index_root_edit.setText(r"\\127.0.0.1\pptx_finder_async_share")
    dlg._add_index_root_network()

    dlg._apply_index_roots()
    assert not dlg._index_roots_save.isEnabled()
    assert not dlg.index_root_edit.isEnabled()
    assert not dlg._index_root_net_add.isEnabled()
    assert not dlg._index_root_local_add.isEnabled()
    assert not dlg._index_root_remove.isEnabled()

    qtbot.waitUntil(lambda: "已保存" in dlg._index_roots_result.text(), timeout=5000)
    assert dlg._index_roots_save.isEnabled()
    assert dlg.index_root_edit.isEnabled()
    assert dlg._index_root_net_add.isEnabled()
    assert dlg._index_root_local_add.isEnabled()
    assert dlg._index_root_remove.isEnabled()
    assert config.get_index_roots() == (r"\\127.0.0.1\pptx_finder_async_share",)
