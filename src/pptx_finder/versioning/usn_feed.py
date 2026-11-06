"""把 NTFS 流水账翻译成 watcher 事件：整卷监听的低开销实现。

对外的形状刻意和 watchdog 的 Observer 一样——往同一个 ``_Handler`` 里喂
created / modified / moved / deleted。防抖、重试、扩展名过滤、版本留底这些逻辑
一行都没动，换掉的只是「事件从哪儿来」。

事件来源换掉之后省下的钱（真机：程序常驻 15 小时累计用掉 5.8 小时 CPU）：

- 不再每个文件事件都走一遍 Python。流水账按块读，临时目录、依赖目录里的变化在
  「父目录已被排除」这一步就丢掉，连路径都不用反查。
- 记录里没有文件名（非特权读取会抹掉），只有文件编号和父目录编号。父目录翻译成路径
  后按编号缓存，同一目录下一万个文件只查一次。

删除和改名怎么办：文件删掉之后编号就打不开了，路径无从反查。所以改成对账——
删除事件发生的目录，拿索引里已知的那几行逐个看还在不在，不在的就是被删/被挪走的。
改名靠「新路径的大小+时间等于某条刚消失的旧记录」配对，保住版本历史的连续性。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from .. import usn_journal as usn
from ..usn_journal import ATTRS, FID, PARENT, REASON

log = logging.getLogger(__name__)

POLL_SEC = 3.0
#: 单轮最多连读几块（每块 256 KB，约 4000 条）。读不完留到下一轮，后台线程不能霸占 GIL。
MAX_CHUNKS_PER_POLL = 64
SAVE_STATE_SEC = 30.0
DIR_CACHE_MAX = 20_000
FILE_CACHE_MAX = 20_000
#: 一个新挪进来的目录树最多展开多少项。超过就交给全量校准，别在后台线程里遍历半个盘。
SUBTREE_WALK_LIMIT = 50_000
_RENAME_OLD = usn.USN_REASON_RENAME_OLD_NAME
_RENAME_NEW = usn.USN_REASON_RENAME_NEW_NAME
_DELETE = usn.USN_REASON_FILE_DELETE
_CREATE = usn.USN_REASON_FILE_CREATE
_IS_DIR = usn.FILE_ATTRIBUTE_DIRECTORY

#: known_rows(目录, 是否含子目录) → [(路径, 字节数, 修改时间)]：索引里已知的内容文件
KnownRows = Callable[[str, bool], Iterable[tuple[str, int, float]]]


def _default_state_path() -> str:
    from ..config import data_dir

    return str(data_dir() / "usn-state.json")


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class _Volume:
    __slots__ = (
        "journal", "root", "journal_id", "cursor", "dirs", "files", "pending_moves",
        "records", "losses", "caught_up_at",
    )

    def __init__(self, journal: usn.VolumeJournal, root: str) -> None:
        self.journal = journal
        self.root = root
        self.journal_id = 0
        self.cursor = 0
        # 父目录编号 → 路径；None 表示这个目录被排除（或已不存在），子文件一律不看
        self.dirs: dict[int, str | None] = {}
        # 内容文件编号 → 上次看到的路径；删除和改名时用来知道它原来叫什么
        self.files: dict[int, str] = {}
        # 只看到改名前半截（旧名）的文件：编号 → 旧父目录编号，等下一块的后半截
        self.pending_moves: dict[int, int] = {}
        self.records = 0
        self.losses = 0
        self.caught_up_at = 0.0


class UsnFeed:
    """一个后台线程，轮询若干个卷的流水账并把变化喂给 handler。"""

    def __init__(
        self,
        handler,
        roots: list[str],
        *,
        state_path: str | None = None,
        poll_sec: float = POLL_SEC,
        known_rows: KnownRows | None = None,
        on_gap: Callable[[str, str], None] | None = None,
        on_unavailable: Callable[[str, str], None] | None = None,
    ) -> None:
        self._handler = handler
        self._roots = list(roots)
        self._state_path = state_path or _default_state_path()
        self._poll_sec = float(poll_sec)
        self._known_rows = known_rows
        self._on_gap = on_gap
        self._on_unavailable = on_unavailable
        self._volumes: list[_Volume] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_saved_at = 0.0
        self._state_dirty = False
        self.unavailable: dict[str, str] = {}

    # ---------------------------------------------------------------- 生命周期
    def open(self) -> list[str]:
        """打开各卷，返回成功接管的卷根。打不开的卷由调用方退回目录监听。"""
        state = load_state(self._state_path)
        opened: list[str] = []
        for root in self._roots:
            try:
                journal = usn.VolumeJournal(root)
                info = journal.query()
            except OSError as exc:
                self.unavailable[root] = str(exc)
                log.info("USN journal unavailable on %s: %s", root, exc)
                continue
            vol = _Volume(journal, root)
            saved = state.get(root) if isinstance(state.get(root), dict) else None
            vol.journal_id = info.journal_id
            vol.cursor = info.next_usn
            if saved is None:
                pass                      # 第一次见这个卷：从现在开始，历史交给全量扫描
            elif (
                int(saved.get("id", 0)) == info.journal_id
                and info.first_usn <= int(saved.get("usn", -1)) <= info.next_usn
            ):
                vol.cursor = int(saved["usn"])   # 接着上次读的位置：离线期间的变化一条不漏
            else:
                self._note_gap(vol, "offline changes overwrote the saved position")
            self._volumes.append(vol)
            opened.append(root)
        return opened

    def start(self) -> None:
        if self._thread is not None or not self._volumes:
            return
        self._thread = threading.Thread(target=self._run, name="UsnFeed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
        self._persist(force=True)
        for vol in self._volumes:
            vol.journal.close()

    def wake(self) -> None:
        """用户正要搜索：马上读一轮，不等下一个轮询周期。"""
        self._wake.set()

    def status(self) -> list[dict]:
        now = time.time()
        rows = []
        for vol in self._volumes:
            rows.append({
                "root": vol.root,
                "backend": "usn",
                "records": vol.records,
                "gaps": vol.losses,
                "last_caught_up_sec": (now - vol.caught_up_at) if vol.caught_up_at else None,
            })
        for root, reason in self.unavailable.items():
            rows.append({"root": root, "backend": "dirwatch", "reason": reason})
        return rows

    # ---------------------------------------------------------------- 主循环
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            for vol in list(self._volumes):
                if self._stop.is_set():
                    break
                try:
                    self._poll(vol)
                except usn.UsnContinuityLost as exc:
                    self._recover_gap(vol, str(exc))
                except OSError as exc:
                    self._drop_volume(vol, str(exc))
                except Exception:  # noqa: BLE001 一轮处理失败不能杀死整个监听线程
                    log.warning("usn feed round failed on %s", vol.root, exc_info=True)
            self._persist()
            self._wake.wait(self._poll_sec)

    def _poll(self, vol: _Volume) -> None:
        journal = vol.journal
        # 没有变化时只有这一次系统调用：这就是空闲开销几乎为零的原因
        info = journal.query()
        if info.journal_id != vol.journal_id:
            raise usn.UsnContinuityLost(f"journal recreated on {vol.root}")
        if info.next_usn == vol.cursor:
            vol.caught_up_at = time.time()
            return
        for _ in range(MAX_CHUNKS_PER_POLL):
            next_usn, records = journal.read(vol.cursor, vol.journal_id)
            if next_usn == vol.cursor:
                vol.caught_up_at = time.time()
                return
            self._process(vol, records)
            vol.cursor = next_usn
            vol.records += len(records)
            self._state_dirty = True
            if self._stop.is_set():
                return
            if not records:
                # 整块记录都被原因筛掉了：位置前进但没内容，继续读
                continue
        # 读满额度还没追平：下一轮马上接着读，别等满一个周期
        self._wake.set()

    # ---------------------------------------------------------------- 断档与退化
    def _note_gap(self, vol: _Volume, reason: str) -> None:
        vol.losses += 1
        log.info("usn feed gap on %s: %s", vol.root, reason)
        if self._on_gap is not None:
            try:
                self._on_gap(vol.root, reason)
            except Exception:  # noqa: BLE001
                log.debug("usn gap callback failed", exc_info=True)

    def _recover_gap(self, vol: _Volume, reason: str) -> None:
        info = vol.journal.query()
        vol.journal_id = info.journal_id
        vol.cursor = info.next_usn
        vol.dirs.clear()
        vol.pending_moves.clear()
        self._state_dirty = True
        self._note_gap(vol, reason)

    def _drop_volume(self, vol: _Volume, reason: str) -> None:
        log.warning("usn feed stopped on %s: %s", vol.root, reason)
        self._volumes.remove(vol)
        self.unavailable[vol.root] = reason
        vol.journal.close()
        if self._on_unavailable is not None:
            try:
                self._on_unavailable(vol.root, reason)
            except Exception:  # noqa: BLE001
                log.debug("usn unavailable callback failed", exc_info=True)

    def _persist(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not self._state_dirty and not force:
            return
        if not force and now - self._state_saved_at < SAVE_STATE_SEC:
            return
        state = load_state(self._state_path) if force else {}
        for vol in self._volumes:
            state[vol.root] = {"id": vol.journal_id, "usn": vol.cursor}
        save_state(self._state_path, state)
        self._state_saved_at = now
        self._state_dirty = False

    # ---------------------------------------------------------------- 翻译
    def _dir_path(self, vol: _Volume, dir_id: int) -> str | None:
        cache = vol.dirs
        try:
            return cache[dir_id]
        except KeyError:
            pass
        path = vol.journal.path_for(dir_id)
        if path is not None and self._handler.skip_directory(path):
            path = None
        if len(cache) >= DIR_CACHE_MAX:
            cache.clear()
        cache[dir_id] = path
        return path

    def _process(self, vol: _Volume, records: list[usn.Record]) -> None:
        """一块记录 → handler 事件。同一文件的多条记录先按编号合并，只处理一次。"""
        files: dict[int, list[int]] = {}
        dir_events: list[usn.Record] = []
        dir_path = self._dir_path
        for rec in records:
            if rec[ATTRS] & _IS_DIR:
                if rec[REASON] & (_CREATE | _DELETE | _RENAME_OLD | _RENAME_NEW):
                    dir_events.append(rec)
                continue
            parent = rec[PARENT]
            reason = rec[REASON]
            # 改名前半截的「旧目录」可能已被排除，但文件可能正往可见处挪，不能在这里丢
            if dir_path(vol, parent) is None and not reason & (_RENAME_OLD | _RENAME_NEW):
                continue
            entry = files.get(rec[FID])
            if entry is None:
                files[rec[FID]] = [reason, parent, parent if reason & _RENAME_OLD else 0]
            else:
                entry[0] |= reason
                entry[1] = parent
                if reason & _RENAME_OLD and not entry[2]:
                    entry[2] = parent
                if reason & _CREATE and not reason & _DELETE:
                    # 同一编号被删后复用的情形不会出现（编号带序列号），这里只合并原因
                    pass
        for fid, old_parent in list(vol.pending_moves.items()):
            entry = files.get(fid)
            if entry is not None:
                entry[2] = old_parent
                entry[0] |= _RENAME_OLD
                del vol.pending_moves[fid]
        if dir_events:
            self._process_dir_events(vol, dir_events)
        if files:
            self._process_files(vol, files)

    def _process_files(self, vol: _Volume, files: dict[int, list[int]]) -> None:
        handler = self._handler
        known_cache = vol.files
        check_dirs: set[str] = set()
        arrivals: list[tuple[str, int, str]] = []   # (新路径, 编号, 旧目录)
        other_dirs: set[str] = set()
        for fid, (reason, parent, old_parent) in files.items():
            parent_path = self._dir_path(vol, parent)
            if reason & _DELETE:
                if parent_path:
                    check_dirs.add(parent_path)
                    other_dirs.add(parent_path)
                known = known_cache.pop(fid, None)
                if known is not None:
                    check_dirs.add(os.path.dirname(known))
                continue
            if reason & _RENAME_OLD and not reason & _RENAME_NEW:
                # 只读到改名前半截，后半截在下一块；也可能是挪到了排除目录
                vol.pending_moves[fid] = old_parent
                if len(vol.pending_moves) > 1000:
                    vol.pending_moves.clear()
                continue
            path = vol.journal.path_for(fid)
            if path is None or handler.skip_path(path):
                # 被挪到了看不见的地方（或已经又被删）：旧位置要对账
                old_dir = self._dir_path(vol, old_parent) if old_parent else None
                if old_dir:
                    check_dirs.add(old_dir)
                    other_dirs.add(old_dir)
                if parent_path:
                    other_dirs.add(parent_path)
                continue
            if handler.is_content_path(path):
                previous = known_cache.get(fid)
                if len(known_cache) >= FILE_CACHE_MAX:
                    known_cache.clear()
                known_cache[fid] = path
                if reason & (_RENAME_OLD | _RENAME_NEW):
                    old_dir = self._dir_path(vol, old_parent) if old_parent else None
                    if previous and os.path.normcase(previous) != os.path.normcase(path):
                        handler.on_moved(FileMovedEvent(previous, path))
                    elif old_dir:
                        check_dirs.add(old_dir)
                        arrivals.append((path, fid, old_dir))
                    else:
                        handler.on_created(FileCreatedEvent(path))
                elif reason & _CREATE:
                    handler.on_created(FileCreatedEvent(path))
                else:
                    handler.on_modified(FileModifiedEvent(path))
            else:
                other_dirs.add(os.path.dirname(path))
                if reason & _RENAME_OLD:
                    old_dir = self._dir_path(vol, old_parent) if old_parent else None
                    if old_dir:
                        other_dirs.add(old_dir)
        for directory in other_dirs:
            handler.other_dir_changed(directory)
        if check_dirs or arrivals:
            self._reconcile_dirs(check_dirs, arrivals)

    def _reconcile_dirs(self, dirs: set[str], arrivals: list[tuple[str, int, str]]) -> None:
        """对账：这些目录里，索引已知但已经不在了的文件 = 被删除或被挪走。"""
        handler = self._handler
        missing: dict[tuple[int, int], list[str]] = {}
        gone: list[str] = []
        if self._known_rows is not None:
            for directory in dirs:
                try:
                    rows = list(self._known_rows(directory, False))
                except Exception:  # noqa: BLE001 对账失败不影响其余事件
                    log.debug("known rows lookup failed: %s", directory, exc_info=True)
                    continue
                for path, size, mtime in rows:
                    if not os.path.exists(path):
                        gone.append(path)
                        missing.setdefault((int(size), int(mtime)), []).append(path)
        matched: set[str] = set()
        for path, _fid, _old in arrivals:
            source = None
            try:
                st = os.stat(path)
            except OSError:
                st = None
            if st is not None:
                candidates = missing.get((int(st.st_size), int(st.st_mtime)))
                if candidates:
                    source = candidates.pop()
            if source is not None and os.path.normcase(source) != os.path.normcase(path):
                matched.add(source)
                handler.on_moved(FileMovedEvent(source, path))
            else:
                handler.on_created(FileCreatedEvent(path))
        for path in gone:
            if path not in matched:
                handler.on_deleted(FileDeletedEvent(path))

    # ---------------------------------------------------------------- 目录事件
    def _process_dir_events(self, vol: _Volume, events: list[usn.Record]) -> None:
        handler = self._handler
        cache = vol.dirs
        renamed_old: dict[int, str | None] = {}
        for rec in events:
            fid = rec[FID]
            reason = rec[REASON]
            parent_path = self._dir_path(vol, rec[PARENT])
            if parent_path:
                handler.other_dir_changed(parent_path)
            if reason & _RENAME_OLD:
                renamed_old[fid] = cache.get(fid)
                continue
            if reason & _DELETE:
                old = cache.pop(fid, None)
                if old:
                    self._dir_removed(old)
                continue
            if reason & _RENAME_NEW:
                old = renamed_old.pop(fid, cache.get(fid))
                # 目录改名：它和所有子目录缓存的路径都失效了，整张表清掉最简单也最稳
                cache.clear()
                vol.files.clear()
                new = self._dir_path(vol, fid)
                if old and new and os.path.normcase(old) != os.path.normcase(new):
                    self._dir_moved(old, new)
                elif old and not new:
                    self._dir_removed(old)
                elif new:
                    self._expand_subtree(new)
        for old in renamed_old.values():
            # 只见前半截：下一块才有后半截；缓存里的旧路径此时已经不准，先失效
            if old:
                cache.clear()
                break

    def _dir_removed(self, directory: str) -> None:
        if self._known_rows is None:
            return
        try:
            rows = list(self._known_rows(directory, True))
        except Exception:  # noqa: BLE001
            log.debug("known rows lookup failed: %s", directory, exc_info=True)
            return
        for path, _size, _mtime in rows:
            if not os.path.exists(path):
                self._handler.on_deleted(FileDeletedEvent(path))

    def _dir_moved(self, old: str, new: str) -> None:
        handler = self._handler
        if self._known_rows is not None:
            try:
                rows = list(self._known_rows(old, True))
            except Exception:  # noqa: BLE001
                log.debug("known rows lookup failed: %s", old, exc_info=True)
                rows = []
            prefix = len(old.rstrip("\\/")) + 1
            for path, _size, _mtime in rows:
                dest = os.path.join(new, path[prefix:])
                if os.path.exists(dest) and not os.path.exists(path):
                    handler.on_moved(FileMovedEvent(path, dest))
        self._expand_subtree(new)

    def _expand_subtree(self, directory: str) -> None:
        """整棵目录挪进来时流水账只记一条，里面的文件要自己展开。"""
        handler = self._handler
        seen = 0
        stack = [directory]
        while stack and not self._stop.is_set():
            current = stack.pop()
            handler.other_dir_changed(current)
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > SUBTREE_WALK_LIMIT:
                            self._note_subtree_overflow(directory)
                            return
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not handler.skip_directory(entry.path):
                                    stack.append(entry.path)
                            elif handler.is_content_path(entry.path) and not handler.skip_path(entry.path):
                                handler.on_created(FileCreatedEvent(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue

    def _note_subtree_overflow(self, directory: str) -> None:
        if self._on_gap is not None:
            try:
                self._on_gap(directory, "moved directory too large to expand")
            except Exception:  # noqa: BLE001
                log.debug("usn gap callback failed", exc_info=True)
