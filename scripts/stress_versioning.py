"""版本管理专项压测：内存 / 存储 / 耗时 / 对用户 PowerPoint 的干扰。

回答四个问题（全部用临时目录，绝不碰真实 vault）：
  1. 每次留底要花多久、放大多少 IO —— 用户按下 Ctrl+S 之后我们抢走多少资源
  2. 长时间常驻会不会涨内存 —— 模块级缓存 / Qt 对象 / 连接
  3. 反复保存同一份稿会不会涨存储 —— 去重是否真的生效
  4. 留底期间源文件是否可写 —— 这是「绝不能影响用户正常用 PPT」的硬底线

用法: python scripts/stress_versioning.py [--rounds N] [--mb N]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def make_deck(path: Path, slides: int, blob_mb: float) -> Path:
    """造一份带不可压缩媒体的 pptx：贴近真实大稿（图片占大头、文本占小头）。"""
    import fixtures_gen as fx

    fx.make_pptx(path, [{"body": f"第 {i} 页 算力 模型 训练"} for i in range(1, slides + 1)])
    if blob_mb > 0:
        blob = os.urandom(int(blob_mb * 1024 * 1024))  # 随机数据 = 不可压缩，模拟图片
        tmp = path.with_suffix(".tmp.pptx")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                dst.writestr(item, src.read(item.filename))
            dst.writestr("ppt/media/image1.png", blob)
        tmp.replace(path)
    return path


_PREV_MARKER = b"\xe7\xae\x97\xe5\x8a\x9b"  # 初始锚点「算力」，之后每轮替换上一轮写入的标记


def touch_text(path: Path, marker: str) -> None:
    """改一处正文并重写包——模拟用户编辑后保存（正文变了、媒体一个字节没动）。

    必须替换「上一轮写进去的标记」而不是固定锚点：否则第二轮起内容其实没变化，
    会被留底的内容哈希判定为未改动而跳过，压测就测了个寂寞。
    """
    global _PREV_MARKER
    new = marker.encode("utf-8")
    tmp = path.with_suffix(".tmp.pptx")
    replaced = False
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "ppt/slides/slide1.xml" and _PREV_MARKER in data:
                data = data.replace(_PREV_MARKER, new, 1)
                replaced = True
            dst.writestr(item, data)
    tmp.replace(path)
    if not replaced:
        raise RuntimeError(f"夹具失效：slide1.xml 里找不到锚点 {_PREV_MARKER!r}")
    _PREV_MARKER = new


def proc_stats() -> dict:
    """进程级 RSS / 句柄数 / 线程数：tracemalloc 只看得到 Python 对象，
    看不到未关闭的文件句柄和线程——而那正是「常驻越用越卡」的常见来源。"""
    out = {"rss_mb": 0.0, "handles": 0, "threads": threading.active_count()}
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        h = k32.GetCurrentProcess()
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        # 现代 Windows 把 psapi 的接口转发进 kernel32（K32 前缀），直接调 psapi.dll
        # 在部分环境下拿不到符号——这就是探针一直读回 0 的原因。
        get_mem = getattr(k32, "K32GetProcessMemoryInfo", None)
        if get_mem is None:
            get_mem = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        get_mem.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        get_mem.restype = wintypes.BOOL
        if get_mem(h, ctypes.byref(pmc), pmc.cb):
            out["rss_mb"] = round(pmc.WorkingSetSize / 1048576, 1)
        k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if k32.GetProcessHandleCount(h, ctypes.byref(count)):
            out["handles"] = int(count.value)
    except Exception:  # noqa: BLE001 非 Windows / API 不可用时只报线程数
        pass
    return out


def dir_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=12, help="模拟保存次数")
    ap.add_argument("--mb", type=float, default=8.0, help="稿件媒体大小 MB")
    ap.add_argument("--slides", type=int, default=30)
    ap.add_argument("--with-threads", action="store_true",
                    help="同时启动对账/重维护后台线程，贴近托盘常驻的真实状态")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="pptutor_vstress_"))
    os.environ["PPTX_FINDER_DATA_DIR"] = str(work / "appdata")
    # 只隔离数据目录还不够：对账线程默认会去「常用目录」（桌面/文档等）补漏，
    # 把用户真实的 PPT 也拉进这个临时库——压测数字会被别人的稿件污染，
    # 更不该在压测里去读用户的文件。显式关掉，压测只认自己造的那份稿。
    os.environ["PPTUTOR_VERSION_RECONCILE_COMMON_DIRS"] = "0"
    from pptx_finder.versioning import store, vault
    from pptx_finder.versioning.manager import VersionManager

    decks = work / "decks"
    decks.mkdir(parents=True)
    deck = make_deck(decks / "big.pptx", args.slides, args.mb)
    deck_bytes = deck.stat().st_size
    print(f"稿件: {deck.name} {_mb(deck_bytes)} / {args.slides} 页, 保存 {args.rounds} 次", flush=True)

    mgr = VersionManager(index_roots=[str(decks)])
    if args.with_threads:
        # watch=False：压测自己驱动留底，不要 watchdog 再叠一层非确定性；
        # 但对账循环与重维护线程照常起，这才是托盘常驻时真正长期存在的东西。
        mgr.start(watch=False)
    vault_root = vault.vault_dir()
    report: dict = {"deck_bytes": deck_bytes, "rounds": args.rounds, "saves": []}

    tracemalloc.start()
    gc.collect()
    base_snap = tracemalloc.take_snapshot()
    rss0 = len(vault._VERIFIED_OBJECT_PATHS)
    proc0 = proc_stats()
    report["proc_before"] = proc0

    for i in range(args.rounds):
        touch_text(deck, f"改{i:02d}")
        t0 = time.perf_counter()
        vid = mgr.snapshot_now(str(deck), notify=False)
        dt = time.perf_counter() - t0
        ps = proc_stats()
        report["saves"].append({
            "round": i,
            "sec": round(dt, 3),
            "vid": bool(vid),
            "vault_bytes": dir_bytes(vault_root),
            "verified_cache": len(vault._VERIFIED_OBJECT_PATHS),
            **ps,
        })
        if i % max(1, args.rounds // 12) == 0 or i == args.rounds - 1:
            print(f"  第 {i+1:3d} 次留底 {dt*1000:7.0f} ms  vault={_mb(report['saves'][-1]['vault_bytes']):>9}"
                  f"  verified={report['saves'][-1]['verified_cache']:>5}"
                  f"  RSS={ps['rss_mb']:>7} MB  句柄={ps['handles']:>5}  线程={ps['threads']}", flush=True)

    gc.collect()
    now_snap = tracemalloc.take_snapshot()
    diff = now_snap.compare_to(base_snap, "filename")
    tracemalloc.stop()
    report["top_alloc_growth"] = [
        {"file": str(s.traceback[0].filename).replace(str(ROOT), "."), "kb": round(s.size_diff / 1024, 1)}
        for s in diff[:6] if s.size_diff > 0
    ]
    report["verified_cache_growth"] = len(vault._VERIFIED_OBJECT_PATHS) - rss0
    proc1 = proc_stats()
    report["proc_after"] = proc1
    report["leak_check"] = {
        "rss_delta_mb": round(proc1["rss_mb"] - proc0["rss_mb"], 1),
        "handle_delta": proc1["handles"] - proc0["handles"],
        "thread_delta": proc1["threads"] - proc0["threads"],
    }

    # ---- 存储效率：N 次保存后 vault 相对稿件本身放大了多少 ----
    # 按组件分解：只有 _objects 才是真正的版本内容，其余任何一项显著增长
    # 都说明有东西在偷偷长（历史上「存储暴涨」反馈都出在非 _objects 的部分）。
    breakdown: dict[str, int] = {}
    for child in sorted(vault_root.iterdir()):
        key = child.name if child.name.startswith(("_", "versions.db")) else "doc-dirs"
        breakdown[key] = breakdown.get(key, 0) + (
            dir_bytes(child) if child.is_dir() else child.stat().st_size)
    report["vault_breakdown_mb"] = {k: round(v / 1048576, 2) for k, v in breakdown.items()}
    report["object_count"] = len(list((vault_root / "_objects").glob("*")))
    vb = dir_bytes(vault_root)
    report["vault_bytes_final"] = vb
    report["vault_vs_one_deck"] = round(vb / max(deck_bytes, 1), 2)
    n_ver = store.connect(vault.db_path()).execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    report["versions"] = n_ver

    # ---- 硬底线：留底进行中，源文件必须仍可被写入（否则 PowerPoint 会存不了盘）----
    lock_result = {"writable_during_snapshot": None, "error": ""}
    hold = threading.Event()
    done = threading.Event()

    def _slow_snapshot():
        try:
            orig = vault.parse_pptx

            def slow(p):                      # 把留底拖长，制造重叠窗口
                hold.set()
                time.sleep(2.0)
                return orig(p)
            vault.parse_pptx = slow
            try:
                mgr.snapshot_now(str(deck), notify=False)
            finally:
                vault.parse_pptx = orig
        except Exception as exc:              # noqa: BLE001
            lock_result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    touch_text(deck, "并发")
    t = threading.Thread(target=_slow_snapshot, daemon=True)
    t.start()
    hold.wait(10)
    time.sleep(0.3)                            # 确保确实处在留底中途
    try:
        with open(deck, "r+b") as f:           # 模拟 PowerPoint 写盘
            f.seek(0)
            f.read(4)
            f.seek(0, os.SEEK_END)
            f.write(b"")
        lock_result["writable_during_snapshot"] = True
    except OSError as exc:
        lock_result["writable_during_snapshot"] = False
        lock_result["error"] = f"{type(exc).__name__}: {exc}"
    done.wait(30)
    t.join(timeout=5)
    report["source_lock"] = lock_result

    # ---- 留底暂存不能残留 ----
    tmp_dir = vault_root / "_tmp"
    report["tmp_leftover_files"] = len(list(tmp_dir.glob("*"))) if tmp_dir.is_dir() else 0

    mgr.stop()
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
