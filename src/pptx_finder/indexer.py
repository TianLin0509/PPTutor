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
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from typing import Any

from . import db
from .config import (
    CONTENT_EXTS,
    DOCX_EXT,
    MAX_PARSE_SIZE,
    MAX_PDF_PARSE_SIZE,
    PDF_EXT,
    PPT_EXT,
    PPTX_EXT,
    ext_path,
)
from .document_parser import parse_document
from .text_tokenize import tokenize

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]
COMMIT_EVERY = 50
SCAN_COMMIT_EVERY = 200  # 扫描期每登记这么多就提交一次，让文件名尽快可搜
PARSE_TIMEOUT_S = 60.0   # 单文件解析超时：超过判定卡住 → 跳过不阻塞整批（子进程隔离的关键保护）
DEFERRED_CONTENT_EXTS = (DOCX_EXT, PDF_EXT)  # 砍掉 xlsx/txt；PPT 优先建完后补建这些


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
    ext = os.path.splitext(path)[1].lower()
    res: dict[str, Any] = {
        "path": path,
        "name": os.path.basename(path),
        "ext": ext,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "content_hash": f"size:{st.st_size}",
        "status": "ok",
        "error": "",
        "page_count": 0,
        "pages": [],
    }
    # 先判尺寸再算 hash：超限直接跳过、连 sha256 都不读（省 IO，防大文件拖慢/卡死）。
    # PDF 更严（pypdf 对大/坏 PDF 易慢易卡）。too_large 仍登记文件名、可按名搜。
    cap = MAX_PDF_PARSE_SIZE if ext == PDF_EXT else MAX_PARSE_SIZE
    if st.st_size > cap:
        res["status"] = "too_large"
        return res
    res["content_hash"] = _file_sha256(ext_path(path))
    deck = parse_document(path)
    res["status"] = deck.status
    res["error"] = deck.error
    res["page_count"] = deck.page_count
    if deck.status == "ok":
        res["pages"] = [
            (pg.page_no, raw, tokenize(raw))
            for pg in deck.pages
            if (raw := db.sqlite_safe_text(pg.raw_text)).strip()
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


def _mark_skipped(conn: sqlite3.Connection, path: Path, status: str, error: str) -> None:
    """把卡住/超时的文件标记为已跳过（用真实 size/mtime，下次扫描视为未变→不再反复重试）。"""
    try:
        st = path.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    fid = db.upsert_file(
        conn,
        path=str(path), name=path.name, ext=path.suffix.lower(), size=size,
        mtime=mtime, content_hash=f"size:{size}", page_count=0,
        status=status, error=error, indexed_at=time.time(),
    )
    db.replace_pages(conn, fid, [])


def _ping() -> bool:
    return True


def _make_executor(max_workers: int):
    """优先 ProcessPoolExecutor：多核真并行（提速）+ GIL/崩溃隔离（单个坏/慢文件冻不住主程序）。
    打包/受限环境子进程起不来则回退 ThreadPoolExecutor（功能不变、退化为单核+GIL）。"""
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        ex = ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx)
        ex.submit(_ping).result(timeout=60)  # 预热并确认子进程真能 spawn（frozen 下可能失败）
        log.info("索引解析用 ProcessPool（%d 进程，多核并行 + 隔离）", max_workers)
        return ex
    except Exception as e:  # noqa: BLE001 子进程起不来不致命，回退线程
        log.warning("ProcessPool 不可用，回退 ThreadPool：%s", e)
        return ThreadPoolExecutor(max_workers=max_workers)


def _shutdown_executor(ex) -> None:
    """关执行器：不等待被超时放弃的卡死任务（否则在此重新卡住），并强制终止 ProcessPool
    残留 worker 进程（防卡死任务占核空转）。ThreadPool 无法杀线程，随主进程退出回收。"""
    try:
        ex.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass
    procs = getattr(ex, "_processes", None)  # ProcessPoolExecutor 私有：{pid: Process}
    if procs:
        for pr in list(procs.values()):
            try:
                if pr.is_alive():
                    pr.terminate()
            except Exception:  # noqa: BLE001
                pass


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
    ex = None if inline else _make_executor(max_workers)
    summary["executor"] = (
        "inline" if ex is None
        else ("process" if isinstance(ex, ProcessPoolExecutor) else "thread")
    )
    futs: dict[Any, Path] = {}
    started: dict[Any, float] = {}  # future → 投递时刻，用于单文件超时判定
    total = 0  # 需解析的 .pptx 数（随扫描增长）
    done = 0
    scan_done = False  # 扫描是否结束（total 是否已是最终值）→ 决定进度报忙碌态还是真实百分比
    # 非 pptx 文档：先按类型排队，PPT 全部处理完后再按稳定顺序整类补建。
    deferred_by_ext: dict[str, list[Path]] = {ext: [] for ext in DEFERRED_CONTENT_EXTS}
    deferred_other: list[Path] = []

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _emit(p) -> None:
        nonlocal done
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

    def write_done(fut) -> None:
        p = futs.pop(fut)
        started.pop(fut, None)
        try:
            _write_result(conn, fut.result())
            summary["indexed"] += 1
        except Exception as e:  # noqa: BLE001 单文件失败不中断批量
            log.warning("index failed %s: %s", p, e)
            summary["errors"] += 1
        _emit(p)

    def submit(p: Path) -> None:
        f = ex.submit(_index_one, str(p))
        futs[f] = p
        started[f] = time.monotonic()

    def reap_timeouts() -> None:
        """放弃超时未完成的 future：标记 timeout、移出队列（子进程留到 shutdown 终止）。
        这是「单个坏/卡死文件不冻住整批」的核心保护——配合 ProcessPool 的进程隔离。"""
        now = time.monotonic()
        for f in [f for f in list(futs)
                  if not f.done() and now - started.get(f, now) > PARSE_TIMEOUT_S]:
            p = futs.pop(f)
            started.pop(f, None)
            f.cancel()  # 排队中的能取消；运行中的取消无效，但我们不再等它
            try:
                _mark_skipped(conn, p, "timeout", "解析超时已跳过")
            except Exception as e:  # noqa: BLE001
                log.warning("mark timeout failed %s: %s", p, e)
            summary["errors"] += 1
            log.warning("parse timeout %.0fs, skipped: %s", PARSE_TIMEOUT_S, p)
            _emit(p)

    def harvest_ready() -> None:
        """非阻塞：收割已完成 future + 清理超时。"""
        for f in [f for f in list(futs) if f.done()]:
            write_done(f)
        reap_timeouts()

    def backpressure() -> None:
        """积压超过容量则阻塞收割直到降回容量内（1s 轮询 + 超时清理，绝不永久阻塞）。"""
        while len(futs) >= max_workers * 4 and not stopped():
            wait(list(futs), timeout=1.0, return_when=FIRST_COMPLETED)
            harvest_ready()

    def drain() -> None:
        """收尾：收割到队列空（1s 轮询 + 超时清理，绝不卡在坏文件上）。"""
        while futs and not stopped():
            wait(list(futs), timeout=1.0, return_when=FIRST_COMPLETED)
            harvest_ready()

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
            ext = p.suffix.lower()
            if ext == PPT_EXT:
                try:
                    _write_filename_only(conn, p)
                    summary["skipped_ppt"] += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("ppt register failed %s: %s", p, e)
                    summary["errors"] += 1
                continue
            if ext != PPTX_EXT:
                # 非 pptx 文档（docx/xlsx/txt/pdf）：先登记文件名（可搜），
                # 内容解析推迟到 pptx 全部完成后再补建（PPT 优先）。
                if ext in CONTENT_EXTS:
                    if not inline:
                        _register_pending(conn, p, st)
                    if ext in deferred_by_ext:
                        deferred_by_ext[ext].append(p)
                    else:
                        deferred_other.append(p)
                continue
            # .pptx —— 最高优先，立即处理（逻辑不变）
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
                    progress_cb(done, -1, f"已发现 {len(seen)} 个 · 已索引 {done} 个")
                if done % COMMIT_EVERY == 0:
                    conn.commit()
            else:
                _register_pending(conn, p, st)  # 先登记文件名（秒级可搜）
                submit(p)
                harvest_ready()   # 非阻塞收割已完成 + 清理超时
                backpressure()    # 积压则阻塞收割到容量内（带超时保护）
        conn.commit()
        scan_done = True  # 扫描结束：total 已是最终值，收尾解析的进度走真实百分比
        deferred = [
            p
            for ext in DEFERRED_CONTENT_EXTS
            for p in deferred_by_ext.get(ext, [])
        ]
        deferred.extend(deferred_other)
        total += len(deferred)

        # ---- 删除磁盘上已消失的文件 ----
        for path in list(existing.keys()):
            if stopped():
                break
            if path not in seen:
                db.delete_file(conn, path)
                summary["deleted"] += 1
        if summary["deleted"]:
            conn.commit()

        # ---- 阶段 2 收尾：收割剩余 pptx 解析（PPT 优先：先把 pptx 全部完成）----
        if not inline:
            drain()
        conn.commit()

        # ---- 阶段 3：PPT 全部就绪后，再后台补建其它文档类型（docx/xlsx/txt/pdf）----
        for p in deferred:
            if stopped():
                break
            sp = str(p)
            if inline:
                try:
                    _write_result(conn, _index_one(sp))
                    summary["indexed"] += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("index failed %s: %s", p, e)
                    summary["errors"] += 1
                done += 1
                if progress_cb:
                    if scan_done:
                        progress_cb(done, total, sp)
                    else:
                        progress_cb(done, -1, f"已发现 {len(seen)} 个 · 已索引 {done} 个")
                if done % COMMIT_EVERY == 0:
                    conn.commit()
            else:
                submit(p)
                harvest_ready()
                backpressure()
        if not inline:
            drain()
        conn.commit()
    finally:
        if ex is not None:
            _shutdown_executor(ex)

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
        elif ext in CONTENT_EXTS:
            _write_result(conn, _index_one(str(p)))
        else:
            return False
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("index_single failed %s: %s", path, e)
        return False
