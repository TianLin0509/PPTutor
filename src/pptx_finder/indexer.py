"""索引构建：多进程并行解析 + 增量更新 + 进度回调。

提速要点（spec §4）：
- 解析(解压+XML)与 jieba 分词都在 worker 进程并行完成，主进程只负责写 SQLite（单写者）。
- 增量：以 (size, mtime) 快筛；内容 hash 复核避免 Office 重置 mtime 造成的误判。
- 边建边可搜：每批提交事务，UI 可立即搜索已入库部分。
- 可中断：stop_event 协作式停止。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import xxhash

from . import db
from .config import MAX_PARSE_SIZE, PPT_EXT, ext_path
from .parser import parse_pptx
from .text_tokenize import tokenize

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]
COMMIT_EVERY = 50


def _file_hash(path: str) -> str:
    h = xxhash.xxh64()
    with open(ext_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _index_one(path: str) -> dict[str, Any]:
    """worker：解析 + 逐页分词 + 内容 hash。返回可 pickle 的紧凑结果。"""
    st = os.stat(ext_path(path))
    res: dict[str, Any] = {
        "path": path,
        "name": os.path.basename(path),
        "ext": os.path.splitext(path)[1].lower(),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "content_hash": "",
        "status": "ok",
        "error": "",
        "page_count": 0,
        "pages": [],
    }
    if st.st_size > MAX_PARSE_SIZE:
        res["status"] = "too_large"
        res["content_hash"] = f"size:{st.st_size}"  # 避免读大文件，仅按大小判变化
        return res

    res["content_hash"] = _file_hash(path)
    deck = parse_pptx(path)
    res["status"] = deck.status
    res["error"] = deck.error
    res["page_count"] = deck.page_count
    if deck.status == "ok":
        pages = []
        for pg in deck.pages:
            raw = pg.raw_text
            pages.append((pg.page_no, raw, tokenize(raw)))
        res["pages"] = pages
    return res


def _write_result(conn: sqlite3.Connection, res: dict[str, Any]) -> None:
    now = time.time()
    existing = db.get_file_by_path(conn, res["path"])
    # 内容未变（hash 同 + 状态同）→ 仅刷新 stat，跳过重写页
    if (
        existing
        and existing["content_hash"] == res["content_hash"]
        and existing["status"] == res["status"]
    ):
        db.touch_stat(conn, existing["id"], res["size"], res["mtime"], now)
        return
    fid = db.upsert_file(
        conn,
        path=res["path"], name=res["name"], ext=res["ext"], size=res["size"],
        mtime=res["mtime"], content_hash=res["content_hash"],
        page_count=res["page_count"], status=res["status"], error=res["error"],
        indexed_at=now,
    )
    db.replace_pages(conn, fid, res["pages"])


def _write_filename_only(conn: sqlite3.Connection, path: Path) -> None:
    """.ppt 旧格式：仅登记文件名，不解析内容。"""
    st = path.stat()
    now = time.time()
    fid = db.upsert_file(
        conn,
        path=str(path), name=path.name, ext=path.suffix.lower(), size=st.st_size,
        mtime=st.st_mtime, content_hash=f"size:{st.st_size}", page_count=0,
        status="filename_only", error="", indexed_at=now,
    )
    db.replace_pages(conn, fid, [])


def index_file_list(
    conn: sqlite3.Connection,
    paths: list[Path],
    progress_cb: ProgressCb | None = None,
    workers: int | None = None,
    stop_event: Any = None,
) -> dict[str, int]:
    """索引给定文件列表。workers=1 时内联执行（便于测试/调试）。"""
    summary = {"indexed": 0, "errors": 0, "skipped_ppt": 0}
    pptx = [p for p in paths if p.suffix.lower() != PPT_EXT]
    ppt = [p for p in paths if p.suffix.lower() == PPT_EXT]
    total = len(pptx) + len(ppt)
    done = 0

    def tick(cur: str) -> None:
        nonlocal done
        done += 1
        if progress_cb:
            progress_cb(done, total, cur)

    # .ppt：文件名登记
    for p in ppt:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            _write_filename_only(conn, p)
            summary["skipped_ppt"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("ppt register failed %s: %s", p, e)
            summary["errors"] += 1
        tick(str(p))

    # .pptx：解析（并行或内联）
    if workers == 1:
        for p in pptx:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                res = _index_one(str(p))
                _write_result(conn, res)
                if res["status"] in ("ok", "filename_only", "too_large"):
                    summary["indexed"] += 1
                else:
                    summary["errors"] += 1
            except Exception as e:  # noqa: BLE001 单文件失败不中断
                log.warning("index failed %s: %s", p, e)
                summary["errors"] += 1
            tick(str(p))
            if done % COMMIT_EVERY == 0:
                conn.commit()
    else:
        max_workers = workers or min(os.cpu_count() or 4, 8)
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_index_one, str(p)): p for p in pptx}
            for fut in as_completed(futs):
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    res = fut.result()
                    _write_result(conn, res)
                    if res["status"] in ("ok", "filename_only", "too_large"):
                        summary["indexed"] += 1
                    else:
                        summary["errors"] += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("index failed %s: %s", futs[fut], e)
                    summary["errors"] += 1
                tick(str(futs[fut]))
                if done % COMMIT_EVERY == 0:
                    conn.commit()
    conn.commit()
    return summary


def update_index(
    conn: sqlite3.Connection,
    roots: list[str],
    progress_cb: ProgressCb | None = None,
    workers: int | None = None,
    stop_event: Any = None,
    scan_iter: Iterable[Path] | None = None,
) -> dict[str, int]:
    """全量/增量更新：扫描 roots，与库比对，索引新增/变更，删除已消失文件。"""
    from .scanner import iter_ppt_files

    existing = db.all_indexed(conn)
    seen: set[str] = set()
    to_index: list[Path] = []

    source = scan_iter if scan_iter is not None else iter_ppt_files(roots)
    for p in source:
        sp = str(p)
        seen.add(sp)
        if progress_cb and len(seen) % 200 == 0:
            progress_cb(0, -1, f"已发现 {len(seen)} 个文件")  # total<0 表示扫描阶段
        row = existing.get(sp)
        if row is None:
            to_index.append(p)
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        # (size, mtime) 快筛：任一变化即重索引
        if int(st.st_size) != int(row["size"]) or abs(st.st_mtime - row["mtime"]) > 1e-6:
            to_index.append(p)

    # 删除磁盘上已消失的文件
    deleted = 0
    for path in list(existing.keys()):
        if path not in seen:
            db.delete_file(conn, path)
            deleted += 1
    if deleted:
        conn.commit()

    summary = index_file_list(conn, to_index, progress_cb, workers, stop_event)
    summary["deleted"] = deleted
    summary["scanned"] = len(seen)
    return summary
