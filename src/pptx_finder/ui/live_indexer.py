"""实时单文件索引——后台串行线程。

根治 UI 冻结：watcher 捕获保存后，**绝不**在主线程 parse PPT / 写库（会抢后台
IndexWorker 的 SQLite 写锁，busy_timeout 最长 8s，主线程一卡就「未响应」）。
主线程只 `submit(path)` 入队即返回；本线程用自有连接串行 `index_single`，
完成后经 `indexed` 信号把刷新动作切回主线程。
"""
from __future__ import annotations

import logging
import queue
import threading

from PySide6.QtCore import QThread, Signal

from .. import db, indexer

log = logging.getLogger(__name__)

_STOP = object()  # 毒丸：唤醒阻塞中的 queue.get 并退出


class LiveIndexer(QThread):
    indexed = Signal(str)  # 某文件已并入索引（path），主线程据此刷新状态/结果
    _CONNECT_RETRY_SEC = 1.0

    def __init__(
        self,
        db_path: str,
        parent=None,
        *,
        allowed_exts_provider=None,
        compute_content_hash_provider=None,
    ) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._allowed_exts_provider = allowed_exts_provider
        self._compute_content_hash_provider = compute_content_hash_provider
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._queued: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, path: str) -> None:
        """主线程调用：仅入队，立即返回（不阻塞 UI）。"""
        if self._stop.is_set():
            return
        with self._lock:
            if path in self._queued:
                return
            self._queued.add(path)
        self._q.put(path)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(_STOP)

    def run(self) -> None:
        conn = None
        connect_attempts = 0
        while not self._stop.is_set():
            try:
                conn = db.connect(self._db_path)  # 自有连接，绝不与主线程共用
                break
            except Exception:  # noqa: BLE001 transient lock/path failure is recoverable
                connect_attempts += 1
                if connect_attempts == 1 or connect_attempts % 30 == 0:
                    log.warning(
                        "live index database connect failed; retrying (attempt %d)",
                        connect_attempts,
                        exc_info=True,
                    )
                if self._stop.wait(self._CONNECT_RETRY_SEC):
                    return
        if conn is None:
            return
        try:
            while not self._stop.is_set():
                item = self._q.get()
                if item is _STOP or self._stop.is_set():
                    break
                with self._lock:
                    self._queued.discard(item)
                try:
                    kwargs = {}
                    if self._allowed_exts_provider is not None:
                        kwargs["supported_exts"] = tuple(self._allowed_exts_provider())
                    if self._compute_content_hash_provider is not None:
                        kwargs["compute_content_hash"] = bool(
                            self._compute_content_hash_provider()
                        )
                    if indexer.index_single(conn, item, **kwargs):
                        self.indexed.emit(item)
                except Exception:  # noqa: BLE001 单文件失败不杀线程
                    log.warning("live index failed %s", item, exc_info=True)
        finally:
            conn.close()
