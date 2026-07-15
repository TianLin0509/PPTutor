"""Synthetic A/B benchmark for basic versus advanced PPT indexing work."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from soak_feature_tiers import _build_corpus


def _db_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
        if candidate.exists()
    )


def _run_once(root: Path, database: Path, advanced: bool) -> dict:
    from pptx_finder import cluster, db, indexer
    from pptx_finder.config import PPT_EXTS

    conn = db.connect(database)
    db.init_db(conn)
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    summary = indexer.update_index(
        conn,
        [str(root)],
        workers=1,
        supported_exts=PPT_EXTS,
        compute_content_hash=advanced,
    )
    if advanced:
        cluster.compute_groups(conn)
    conn.commit()
    elapsed = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    conn.close()
    return {
        "wall_sec": elapsed,
        "cpu_sec": cpu,
        "db_bytes": _db_bytes(database),
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-size", type=int, default=300)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="pptdoctor-feature-bench-"))
    corpus = workspace / "corpus"
    _build_corpus(corpus, args.corpus_size)
    runs = {"basic": [], "advanced": []}
    order = ["advanced", "basic"] if int(time.time()) % 2 else ["basic", "advanced"]
    for repeat in range(max(1, args.repeats)):
        for mode in (order if repeat % 2 == 0 else list(reversed(order))):
            database = workspace / f"{repeat}-{mode}.db"
            runs[mode].append(_run_once(corpus, database, mode == "advanced"))

    medians = {}
    for mode, samples in runs.items():
        medians[mode] = {
            "wall_sec": statistics.median(sample["wall_sec"] for sample in samples),
            "cpu_sec": statistics.median(sample["cpu_sec"] for sample in samples),
            "db_bytes": int(statistics.median(sample["db_bytes"] for sample in samples)),
        }
    basic = medians["basic"]
    advanced = medians["advanced"]
    payload = {
        "corpus_size": args.corpus_size,
        "repeats": max(1, args.repeats),
        "medians": medians,
        "basic_wall_saving_percent": round(
            max(0.0, 1.0 - basic["wall_sec"] / max(advanced["wall_sec"], 1e-9)) * 100,
            2,
        ),
        "basic_cpu_saving_percent": round(
            max(0.0, 1.0 - basic["cpu_sec"] / max(advanced["cpu_sec"], 1e-9)) * 100,
            2,
        ),
        "runs": runs,
        "workspace": str(workspace),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
