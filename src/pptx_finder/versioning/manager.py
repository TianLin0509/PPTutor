"""版本管理编排：纳管目录 / 监听保存 / 离线补记 / 会话折叠 / 配额 / 恢复 / 找回 / 跨版本搜。

零操作负担：用户只管把目录加入管理，其余全自动。
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


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:  # 不同盘符
        return False


class VersionManager:
    def __init__(self, conn=None):
        self._conn = conn or store.connect(vault.db_path())
        store.init_db(self._conn)
        self._lock = threading.RLock()  # 可重入：restore/catch_up 内部还会调 snapshot
        self._watcher = None

    # ---------- 受管目录 ----------
    def list_roots(self) -> list[str]:
        with self._lock:
            return store.list_roots(self._conn)

    def add_root(self, path: str) -> None:
        path = os.path.abspath(path)
        with self._lock:
            store.add_root(self._conn, path, _now())
        self.catch_up_root(path)  # 纳入即对现有 .pptx 建首版（内部各自加锁）

    def remove_root(self, path: str) -> None:
        with self._lock:
            store.remove_root(self._conn, os.path.abspath(path))

    def is_managed(self, path: str) -> bool:
        path = os.path.abspath(path)
        return any(_under(path, r) for r in self.list_roots())

    # ---------- 快照（保存触发 / 手动 / 补记 共用） ----------
    def snapshot_now(self, path: str) -> str | None:
        if os.path.splitext(path)[1].lower() != PPTX_EXT:
            return None
        if not self.is_managed(path):
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

    # ---------- 离线补记 ----------
    def catch_up_root(self, root: str) -> None:
        for p in iter_ppt_files([root]):
            if p.suffix.lower() == PPTX_EXT:
                self.snapshot_now(str(p))  # 内容没变 vault 内部会跳过

    def catch_up_all(self) -> None:
        for r in self.list_roots():
            self.catch_up_root(r)

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
        """应用启动调用：离线补记 + 标记已删 + 起实时监听。"""
        self.catch_up_all()
        self.scan_deleted()
        self._start_watcher()

    def _start_watcher(self) -> None:
        self._stop_watcher()
        roots = self.list_roots()
        if not roots:
            return
        from .watcher import VaultWatcher
        self._watcher = VaultWatcher(roots, self.snapshot_now)
        self._watcher.start()

    def restart_watcher(self) -> None:
        self._start_watcher()

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def stop(self) -> None:
        self._stop_watcher()
