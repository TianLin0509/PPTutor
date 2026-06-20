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

    def __init__(self, db_path: str, parent=None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()

    def submit(self, path: str) -> None:
        """主线程调用：仅入队，立即返回（不阻塞 UI）。"""
        if not self._stop.is_set():
            self._q.put(path)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(_STOP)

    def run(self) -> None:
        conn = db.connect(self._db_path)  # 自有连接，绝不与主线程共用
        try:
            while not self._stop.is_set():
                item = self._q.get()
                if item is _STOP or self._stop.is_set():
                    break
                try:
                    if indexer.index_single(conn, item):
                        self.indexed.emit(item)
                except Exception:  # noqa: BLE001 单文件失败不杀线程
                    log.warning("live index failed %s", item, exc_info=True)
        finally:
            conn.close()
