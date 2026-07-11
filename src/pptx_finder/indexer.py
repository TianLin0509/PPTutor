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
MAX_UNCHANGED_PARSE_FAILURES = 3
ERROR_RETRY_DELAYS_S = (24 * 60 * 60, 7 * 24 * 60 * 60)

# Windows 云盘占位文件：文件名/元数据可见，但内容需召回后才能读。
_CLOUD_PLACEHOLDER_ATTRS = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


def _stat_hash(size: int, mtime: float) -> str:
    """(size, mtime) 派生的轻量变更标识——不读文件内容。"""
    return f"{mtime}:{size}"


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _is_cloud_placeholder(path: str | Path, st: os.stat_result | None = None) -> bool:
    """Return whether a Windows file is an unhydrated cloud placeholder."""
    try:
        st = st or os.stat(ext_path(str(path)))
    except OSError:
        return False
    return bool(int(getattr(st, "st_file_attributes", 0) or 0) & _CLOUD_PLACEHOLDER_ATTRS)


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

    增量重解析采用 stale-while-revalidate：旧页继续可搜，解析成功后再原子替换。
    """
    _mark_skipped(
        conn, path, "pending", "", size=st.st_size, mtime=st.st_mtime,
    )


def _write_result(conn: sqlite3.Connection, res: dict[str, Any]) -> None:
    """阶段 2：成功才替换旧内容；失败保留最后一次可搜索结果。"""
    if res["status"] != "ok":
        _mark_skipped(
            conn,
            Path(res["path"]),
            str(res["status"]),
            str(res.get("error") or ""),
            size=int(res.get("size") or 0),
            mtime=float(res.get("mtime") or 0.0),
            retryable=res["status"] == "error",
        )
        return
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


def _mark_skipped(
    conn: sqlite3.Connection,
    path: Path,
    status: str,
    error: str,
    *,
    size: int | None = None,
    mtime: float | None = None,
    retryable: bool = False,
) -> None:
    """Persist a non-success state without destroying last-known-good pages.

    Parser errors get a bounded retry schedule. After three failures with the
    same file stat, only a real size/mtime change can reopen the circuit.
    """
    if size is None or mtime is None:
        try:
            st = path.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
    now = time.time()
    previous = db.get_file_by_path(conn, str(path))
    previous_failures = int(previous["parse_failures"] or 0) if previous else 0
    previous_retry_after = float(previous["retry_after"] or 0) if previous else 0.0
    if retryable:
        failures = previous_failures + 1
        if failures <= len(ERROR_RETRY_DELAYS_S):
            retry_after = now + ERROR_RETRY_DELAYS_S[failures - 1]
        else:
            retry_after = 0.0
    elif status == "pending":
        failures = previous_failures
        retry_after = previous_retry_after
    else:
        failures = 0
        retry_after = 0.0
    content_hash = (
        str(previous["content_hash"] or "")
        if previous
        else _stat_hash(int(size), float(mtime))
    )
    page_count = int(previous["page_count"] or 0) if previous else 0
    db.upsert_file(
        conn,
        path=str(path), name=path.name, ext=path.suffix.lower(), size=size,
        mtime=mtime, content_hash=content_hash, page_count=page_count,
        status=status, error=error, indexed_at=now,
        parse_failures=failures, retry_after=retry_after,
    )


def _mark_index_failure(conn: sqlite3.Connection, path: Path, exc: Exception) -> str:
    """Resolve a worker failure to a stable non-pending state."""
    if not path.exists():
        db.delete_file(conn, str(path))
        return "missing"
    status = "cloud_placeholder" if _is_cloud_placeholder(path) else "error"
    message = (
        "云文件尚未下载，内容可用后将自动重试"
        if status == "cloud_placeholder"
        else f"{type(exc).__name__}: {exc}"
    )
    _mark_skipped(conn, path, status, message, retryable=status == "error")
    return status


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
    summary = {
        "indexed": 0,
        "errors": 0,
        "skipped_ppt": 0,
        "skipped_cloud": 0,
        "deleted": 0,
    }
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
            _mark_index_failure(conn, p, e)
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
            if _is_cloud_placeholder(p, st):
                unchanged_placeholder = (
                    row is not None
                    and int(st.st_size) == int(row["size"])
                    and abs(st.st_mtime - row["mtime"]) <= 1e-6
                    and row["status"] == "cloud_placeholder"
                )
                if not unchanged_placeholder:
                    _mark_skipped(
                        conn,
                        p,
                        "cloud_placeholder",
                        "云文件尚未下载，内容可用后将自动重试",
                    )
                summary["skipped_cloud"] += 1
                continue
            # (size, mtime) 快筛。永久解析错误用熔断式退避：同一份字节最多
            # 自动尝试三次；文件 stat 真变化时始终立即重试。
            same_stat = (
                row is not None
                and int(st.st_size) == int(row["size"])
                and abs(st.st_mtime - row["mtime"]) <= 1e-6
            )
            unchanged = same_stat
            if same_stat and row["status"] in ("pending", "cloud_placeholder"):
                unchanged = False
            elif same_stat and row["status"] == "error":
                failures = int(row["parse_failures"] or 0)
                retry_after = float(row["retry_after"] or 0)
                unchanged = not (
                    failures < MAX_UNCHANGED_PARSE_FAILURES
                    and time.time() >= retry_after
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
                    _mark_index_failure(conn, p, e)
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
                    _mark_index_failure(conn, p, e)
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
    if scan_iter is None and not stopped():
        db.set_meta(conn, db.META_LAST_COMPLETED_SCAN_AT, str(time.time()))
        conn.commit()
    summary["scanned"] = len(seen)
    return summary


def index_single(conn: sqlite3.Connection, path: str) -> bool:
    """实时增量：索引单个文件（watcher 捕获到新建/改存时调用）。

    只变更这一个路径：存在则 upsert，已消失则删掉该路径的陈旧索引；绝不影响
    其他记录。供实时 watcher 使用——新建、改存、移动、删除都无需全盘重扫。
    """
    p = Path(path)
    try:
        if not p.exists():
            if db.get_file_by_path(conn, str(p)) is None:
                return False
            db.delete_file(conn, str(p))
            conn.commit()
            return True
        st = p.stat()
        if _is_cloud_placeholder(p, st):
            _mark_skipped(
                conn,
                p,
                "cloud_placeholder",
                "云文件尚未下载，内容可用后将自动重试",
            )
            conn.commit()
            return True
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
        try:
            _mark_index_failure(conn, p, e)
            conn.commit()
        except Exception:  # noqa: BLE001
            log.debug("failed to persist live index error %s", path, exc_info=True)
        return False
