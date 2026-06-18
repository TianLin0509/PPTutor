"""真机量化：免 hash + 流水线对首次索引的提速。隔离 data dir，不碰生产库。

用法: uv run python scripts/bench_index.py [目录]   默认 Desktop
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["PPTX_FINDER_DATA_DIR"] = tempfile.mkdtemp(prefix="pptxbench_")

import xxhash  # noqa: E402

from pptx_finder import db, indexer  # noqa: E402
from pptx_finder.config import db_path, ext_path  # noqa: E402
from pptx_finder.scanner import iter_ppt_files  # noqa: E402

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["USERPROFILE"], "Desktop")


def main() -> None:
    print(f"目标目录: {ROOT}")
    # ① 扫描 walk 列文件
    t0 = time.perf_counter()
    files = [p for p in iter_ppt_files([ROOT]) if p.suffix.lower() == ".pptx"]
    t_scan = time.perf_counter() - t0
    print(f"① 扫描 walk: {len(files)} 个 .pptx，{t_scan:.2f}s")
    if not files:
        print("（该目录没有 .pptx，换个目录：uv run python scripts/bench_index.py <目录>）")
        return

    # ② 旧逻辑的「读全量算 xxh64」总耗时（= 免 hash 省掉的纯 IO）
    t0 = time.perf_counter()
    total_mb = 0.0
    for p in files:
        h = xxhash.xxh64()
        try:
            with open(ext_path(str(p)), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    total_mb += len(chunk) / 1048576
        except OSError:
            pass
    t_hash = time.perf_counter() - t0
    print(f"② [旧] 读全量算 hash: {t_hash:.2f}s（{total_mb:.0f} MB）← 免 hash 直接省掉")

    # ③ 新 update_index 整体（隔离库，多进程流水线）
    conn = db.connect(str(db_path()))
    db.init_db(conn)
    t0 = time.perf_counter()
    summary = indexer.update_index(conn, [ROOT])
    t_total = time.perf_counter() - t0
    pages = db.stats(conn)["page_count"]
    print(f"③ [新] 整体首次索引: {t_total:.2f}s，indexed={summary['indexed']}，pages={pages}")
    conn.close()

    # 估算对比：旧 ≈ 扫描 + 读全量hash + 解析（串行）；新 ≈ 流水线 + 免hash
    old_est = t_total + t_hash + t_scan
    print("\n—— 对比 ——")
    print(f"免 hash 省:                {t_hash:.2f}s")
    print(f"流水线省(walk/解析重叠): ~{t_scan:.2f}s")
    if t_total > 0:
        print(f"新 {t_total:.2f}s  vs  旧(估算) {old_est:.2f}s  →  约 {old_est / t_total:.1f}× 提速")


if __name__ == "__main__":
    main()
