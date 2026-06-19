"""版本管理编排：全盘监听保存事件 → 谁变管谁 → 首次修改即建 v1。

零操作负担：装了就全自动，不需选目录、不扫描存量（死 PPT 永不进库、绝不卡死）。
只有两种 PPTX 进入管理：① 运行后新建的 ② 运行后被改存的老文件
（改后这一版作为第一版，之前的历史不追踪——本来也没数据）。

线程安全：watcher 子线程与 UI 主线程都经本类访问同一 SQLite 连接，
所有 conn 操作用 RLock 串行（SQLite 单连接非线程安全）。
"""
from __future__ import annotations

import datetime
import os
import threading

from ..config import PPTX_EXT
from ..scanner import iter_ppt_files
from ..text_tokenize import build_fts_match
from . import store, vault

SESSION_GAP_SEC = 30 * 60  # 30 分钟内的连续保存算同一编辑会话（时间线折叠用）
KEEP_PER_DOC = 50          # 每文档保留版本上限（超出按时间清理最旧的）


def _now() -> float:
    return datetime.datetime.now().timestamp()


def _sid(ts: float) -> str:
    return "s" + datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M")


class VersionManager:
    def __init__(self, conn=None):
        self._conn = conn or store.connect(vault.db_path())
        store.init_db(self._conn)
        self._lock = threading.RLock()  # 可重入：restore 内部还会调 snapshot
        self._watcher = None

    # ---------- 快照（保存触发 / 手动补录 共用） ----------
    def snapshot_now(self, path: str) -> str | None:
        """对 path 当前内容拍快照。第一次见到该文件 → 建 v1；内容没变则跳过（返回 None）。

        无「受管目录」概念：任何 .pptx 的保存都记录——这正是「谁变管谁」。
        """
        if os.path.splitext(path)[1].lower() != PPTX_EXT:
            return None
        with self._lock:
            sid = self._session_id(path)
            vid = vault.snapshot(self._conn, path, sid)
            if vid:
                self._enforce_quota(vault.doc_id_for(path))
            return vid

    def _session_id(self, path: str) -> str:
        latest = store.latest_version(self._conn, vault.doc_id_for(path))
        now = _now()
        if latest is not None and (now - latest["ts"]) < SESSION_GAP_SEC:
            return latest["session_id"] or _sid(latest["ts"])
        return _sid(now)

    def catch_up_root(self, root: str) -> int:
        """把某目录下现存 .pptx 各建一版——手动补录 / 测试用。

        生产流程靠 watcher 实时触发，绝不自动调用此方法（避免遍历卡死）。
        """
        n = 0
        for p in iter_ppt_files([root]):
            if p.suffix.lower() == PPTX_EXT and self.snapshot_now(str(p)):
                n += 1
        return n

    # ---------- 查询（供 UI，全部加锁） ----------
    def list_docs(self):
        with self._lock:
            return store.list_docs(self._conn)

    def get_doc(self, doc_id: str):
        with self._lock:
            return store.get_doc(self._conn, doc_id)

    def get_version(self, version_id: str):
        with self._lock:
            return store.get_version(self._conn, version_id)

    def list_versions(self, path: str):
        with self._lock:
            return store.list_versions(self._conn, vault.doc_id_for(path))

    def list_versions_by_doc(self, doc_id: str):
        with self._lock:
            return store.list_versions(self._conn, doc_id)

    # ---------- 恢复 / 导出 ----------
    def restore_to(self, path: str, version_id: str, dest: str | None = None) -> bool:
        """恢复某版本。覆盖原文件前先把当前状态快照一版（绝不丢）。"""
        with self._lock:
            did = vault.doc_id_for(path)
            target = dest or path
            if target == path and os.path.exists(path):
                self.snapshot_now(path)
            return vault.rebuild_to(did, version_id, target)

    def export(self, path: str, version_id: str, dest: str) -> bool:
        # 纯文件操作，不碰 conn
        return vault.rebuild_to(vault.doc_id_for(path), version_id, dest)

    # ---------- 跨版本内容搜索 ----------
    def search_history(self, query: str):
        """返回历史版本命中：[(doc_id, version_id, page_no)]。"""
        with self._lock:
            return store.search_versions(self._conn, build_fts_match(query))

    # ---------- 误删 / 改坏找回 ----------
    def scan_deleted(self) -> int:
        """检查受管文档原文件是否还在，消失的标 deleted（但保留 vault）。"""
        with self._lock:
            n = 0
            for doc in store.list_docs(self._conn):
                if doc["status"] == "active" and not os.path.exists(doc["path"]):
                    store.set_status(self._conn, doc["doc_id"], "deleted")
                    n += 1
            return n

    def recover(self, doc_id: str, dest: str | None = None) -> bool:
        """从版本库重建出最新版本到 dest（默认原路径）。"""
        with self._lock:
            doc = store.get_doc(self._conn, doc_id)
            latest = store.latest_version(self._conn, doc_id)
            if not doc or not latest:
                return False
            ok = vault.rebuild_to(doc_id, latest["version_id"], dest or doc["path"])
            if ok and (dest is None or dest == doc["path"]):
                store.set_status(self._conn, doc_id, "active")
            return ok

    # ---------- 配额（调用方已持锁） ----------
    def _enforce_quota(self, doc_id: str) -> None:
        vers = store.list_versions(self._conn, doc_id)  # 按 ts 降序
        if len(vers) <= KEEP_PER_DOC:
            return
        for v in vers[KEEP_PER_DOC:]:
            store.delete_version(self._conn, v["version_id"])
            try:
                vault.version_file(doc_id, v["version_id"]).unlink(missing_ok=True)
            except OSError:
                pass
        self._conn.commit()

    # ---------- 监听生命周期 ----------
    def start(self) -> None:
        """应用启动：标记已删 + 起全盘实时监听。不扫描任何存量，绝不卡。"""
        self.scan_deleted()
        self._start_watcher()

    def _start_watcher(self) -> None:
        self._stop_watcher()
        from .watcher import VaultWatcher, default_watch_paths
        self._watcher = VaultWatcher(default_watch_paths(), self.snapshot_now)
        self._watcher.start()

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def stop(self) -> None:
        self._stop_watcher()
