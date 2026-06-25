"""索引构建：流式扫描 + 两阶段渐进 + 并行解析（walk 与解析流水线化）。

设计（v0.3 优化）：
- 增量快筛仍用 (size, mtime)，避免每次扫描都读完整文件；
  解析阶段顺手计算 sha256 内容指纹，用于识别完全相同副本。
- 两阶段渐进：阶段 1 流式登记文件名（status=pending，秒级可按名搜）；
  阶段 2 并行解析内容、升级为 ok。
- 流水线：边扫描边登记、边投递解析，walk 的磁盘 IO 与解析的 CPU 重叠。
- 边建边可搜：扫描期定期提交，已登记的文件名立即可被搜索命中。
- 可中断：stop_event 协作式停止；进程池退出时取消未完成任务。
"""
from __future__ import annotations

import logging
import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from pathlib import Path
from typing import Any

from . import db
from .config import MAX_PARSE_SIZE, PPT_EXT, ext_path
from .parser import parse_pptx
from .text_tokenize import tokenize

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]
COMMIT_EVERY = 50
SCAN_COMMIT_EVERY = 200  # 扫描期每登记这么多就提交一次，让文件名尽快可搜


def _stat_hash(size: int, mtime: float) -> str:
    """(size, mtime) 派生的轻量变更标识——不读文件内容。"""
    return f"{mtime}:{size}"


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _index_one(path: str) -> dict[str, Any]:
    """worker：解压 + 提取文本 + 逐页分词。返回可 pickle 的紧凑结果。

    变更检测交给上层 (size, mtime) 快筛；走到这里说明需要解析，
    顺手计算完整文件 sha256，供搜索结果折叠完全相同副本。
    """
    st = os.stat(ext_path(path))
    res: dict[str, Any] = {
        "path": path,
        "name": os.path.basename(path),
        "ext": os.path.splitext(path)[1].lower(),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "content_hash": _file_sha256(ext_path(path)),
        "status": "ok",
        "error": "",
        "page_count": 0,
        "pages": [],
    }
    if st.st_size > MAX_PARSE_SIZE:
        res["status"] = "too_large"
        return res
    deck = parse_pptx(path)
    res["status"] = deck.status
    res["error"] = deck.error
    res["page_count"] = deck.page_count
    if deck.status == "ok":
        res["pages"] = [
            (pg.page_no, pg.raw_text, tokenize(pg.raw_text)) for pg in deck.pages
        ]
    return res


def _register_pending(conn: sqlite3.Connection, path: Path, st: os.stat_result) -> None:
    """阶段 1：仅登记文件名（status=pending，不解析内容），文件名立即可搜。

    增量重解析时覆盖旧记录并清空旧页（旧内容先消失，解析完再补新内容）。
    """
    fid = db.upsert_file(
        conn,
        path=str(path), name=path.name, ext=path.suffix.lower(), size=st.st_size,
        mtime=st.st_mtime, content_hash=_stat_hash(st.st_size, st.st_mtime),
        page_count=0, status="pending", error="", indexed_at=time.time(),
    )
    db.replace_pages(conn, fid, [])


def _write_result(conn: sqlite3.Connection, res: dict[str, Any]) -> None:
    """阶段 2：写入解析结果（覆盖阶段 1 的 pending 占位 / 旧内容）。"""
    fid = db.upsert_file(
        conn,
        path=res["path"], name=res["name"], ext=res["ext"], size=res["size"],
        mtime=res["mtime"], content_hash=res["content_hash"],
        page_count=res["page_count"], status=res["status"], error=res["error"],
        indexed_at=time.time(),
    )
    db.replace_pages(conn, fid, res["pages"])


def _write_filename_only(conn: sqlite3.Connection, path: Path) -> None:
    """.ppt 旧格式：仅登记文件名，不解析内容。"""
    st = path.stat()
    fid = db.upsert_file(
        conn,
        path=str(path), name=path.name, ext=path.suffix.lower(), size=st.st_size,
        mtime=st.st_mtime, content_hash=f"size:{st.st_size}", page_count=0,
        status="filename_only", error="", indexed_at=time.time(),
    )
    db.replace_pages(conn, fid, [])


def update_index(
    conn: sqlite3.Connection,
    roots: list[str],
    progress_cb: ProgressCb | None = None,
    workers: int | None = None,
    stop_event: Any = None,
    scan_iter: Iterable[Path] | None = None,
) -> dict[str, int]:
    """增量更新：流式扫描 → 即时登记文件名 → 并行解析补全内容。

    progress_cb(done, total, cur)：total<0 = 扫描阶段（文件名渐进可搜），
    total>=0 = 内容解析阶段（done/total）。
    """
    from .scanner import iter_ppt_files

    existing = db.all_indexed(conn)
    seen: set[str] = set()
    summary = {"indexed": 0, "errors": 0, "skipped_ppt": 0, "deleted": 0}
    source = scan_iter if scan_iter is not None else iter_ppt_files(roots)

    inline = workers == 1
    max_workers = workers or min(os.cpu_count() or 4, 8)
    if not inline:
        tokenize("预热")  # 主线程先触发 OpenCC 繁简词典加载，避免多线程首次并发竞态
    ex = None if inline else ThreadPoolExecutor(max_workers=max_workers)
    futs: dict[Any, Path] = {}
    total = 0  # 需解析的 .pptx 数（随扫描增长）
    done = 0
    scan_done = False  # 扫描是否结束（total 是否已是最终值）→ 决定进度报忙碌态还是真实百分比

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def write_done(fut) -> None:
        nonlocal done
        p = futs.pop(fut)
        try:
            _write_result(conn, fut.result())
            summary["indexed"] += 1
        except Exception as e:  # noqa: BLE001 单文件失败不中断批量
            log.warning("index failed %s: %s", p, e)
            summary["errors"] += 1
        done += 1
        if progress_cb:
            if scan_done:
                progress_cb(done, total, str(p))   # 扫描已完，total 为最终值 → 真实百分比
            else:
                # 扫描进行中 total 随发现增长，done/total 恒≈99% 误导用户；
                # 改报忙碌态(total=-1) + 真实计数，待扫描结束再走确定性百分比。
                progress_cb(done, -1, f"已发现 {len(seen)} 个 · 已索引 {done} 个")
        if done % COMMIT_EVERY == 0:
            conn.commit()

    try:
        # ---- 阶段 1：流式扫描 + 即时登记文件名（并行路径同时投递解析）----
        for p in source:
            if stopped():
                break
            sp = str(p)
            seen.add(sp)
            if len(seen) % SCAN_COMMIT_EVERY == 0:
                conn.commit()  # 已登记的文件名落盘 → 立即可搜
                if progress_cb:
                    progress_cb(0, -1, f"已发现 {len(seen)} 个文件")
            row = existing.get(sp)
            try:
                st = p.stat()
            except OSError:
                continue
            # (size, mtime) 快筛；status=pending 视为「上次没解析完」需重做
            unchanged = (
                row is not None
                and int(st.st_size) == int(row["size"])
                and abs(st.st_mtime - row["mtime"]) <= 1e-6
                and row["status"] != "pending"
            )
            if unchanged:
                continue
            if p.suffix.lower() == PPT_EXT:
                try:
                    _write_filename_only(conn, p)
                    summary["skipped_ppt"] += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("ppt register failed %s: %s", p, e)
                    summary["errors"] += 1
                continue
            # .pptx
            total += 1
            if inline:
                try:
                    _write_result(conn, _index_one(sp))
                    summary["indexed"] += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("index failed %s: %s", p, e)
                    summary["errors"] += 1
                done += 1
                if progress_cb:
                    progress_cb(done, total, sp)
                if done % COMMIT_EVERY == 0:
                    conn.commit()
            else:
                _register_pending(conn, p, st)  # 先登记文件名（秒级可搜）
                futs[ex.submit(_index_one, sp)] = p
                for f in [f for f in list(futs) if f.done()]:  # 非阻塞收割
                    write_done(f)
                if len(futs) >= max_workers * 4:  # 背压：防积压内存膨胀
                    fin, _ = wait(list(futs), return_when=FIRST_COMPLETED)
                    for f in fin:
                        write_done(f)
        conn.commit()
        scan_done = True  # 扫描结束：total 已是最终值，收尾解析的进度走真实百分比

        # ---- 删除磁盘上已消失的文件 ----
        for path in list(existing.keys()):
            if stopped():
                break
            if path not in seen:
                db.delete_file(conn, path)
                summary["deleted"] += 1
        if summary["deleted"]:
            conn.commit()

        # ---- 阶段 2 收尾：收割剩余解析 ----
        if not inline and futs:
            for fut in as_completed(list(futs)):
                if stopped():
                    break
                write_done(fut)
        conn.commit()
    finally:
        if ex is not None:
            ex.shutdown(wait=not stopped(), cancel_futures=stopped())

    if progress_cb:
        progress_cb(total, total, "完成")  # 进度走满
    summary["scanned"] = len(seen)
    return summary


def index_single(conn: sqlite3.Connection, path: str) -> bool:
    """实时增量：索引单个文件（watcher 捕获到新建/改存时调用）。

    只 upsert 这一个文件、绝不删除其他记录（区别于 update_index 的全量删除逻辑）。
    供「谁变管谁」的实时索引用——新建/改完一个 PPT 立刻可搜，无需重扫全盘。
    """
    p = Path(path)
    try:
        if not p.exists():
            return False
        ext = p.suffix.lower()
        if ext == PPT_EXT:
            _write_filename_only(conn, p)
        elif ext == ".pptx":
            _write_result(conn, _index_one(str(p)))
        else:
            return False
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("index_single failed %s: %s", path, e)
        return False
