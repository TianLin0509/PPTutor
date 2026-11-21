"""「刚变过的文件」热层：Everything 式逐条记账，新文件秒级可搜。

为什么需要第三层（全量 names + 增量 overlay 之外）：
  增量层按「脏目录」整目录重列、再把整份增量层重写一遍。开发机上会话记录、
  浏览器缓存、worktree 一分钟能弄脏几百个目录，于是
    ① 脏目录集合 2 万封顶，满了就不再收——之后新建的文件要等次日全盘扫描；
    ② 增量层涨到十几万条后每轮要 70 多秒，用户刚存的目录排在 churn 后面；
    ③ 全盘扫描进行中（每天一次，15~30 分钟）增量层整轮暂停。
  2026-11-21 真机实测：新建的 txt/xlsx/md 5 分钟后仍搜不到。

Everything 的做法是拿到变更日志里的每一条记录就直接改内存索引，与目录大小无关。
这里照搬这个思路：流水账/目录监听报上来的**每个文件路径**直接记进一个小字典，
1 秒防抖后把这几十、几百条单独写成一份小索引（毫秒级），搜索时与另外两层一起扫。
下一轮增量层对账会把它们并进去，热层随即清空，所以它始终很小。
"""
from __future__ import annotations

import os
from collections import OrderedDict

from . import namestore

#: 热层条目上限。正常情况下每轮增量对账都会把它清空，只有增量层也满了才会涨到这里；
#: 超出时丢最早的（它们已经等了最久，下次全盘扫描会收进来）。
HOT_MAX_ENTRIES = 50_000


class HotNames:
    """主线程使用：note() 必须 O(1)。"""

    def __init__(self, max_entries: int = HOT_MAX_ENTRIES) -> None:
        self._items: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._seq = 0
        self._max = max(1, int(max_entries))
        self.evicted = 0

    def __len__(self) -> int:
        return len(self._items)

    def note(self, path: str) -> None:
        if not path or os.path.basename(path).startswith("~$"):
            return
        key = os.path.normcase(path)
        self._seq += 1
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = (path, self._seq)
        while len(self._items) > self._max:
            self._items.popitem(last=False)
            self.evicted += 1

    def snapshot(self) -> tuple[tuple[str, str, int], ...]:
        """(归一化键, 路径, 记账序号)。交给后台线程，不共享可变状态。"""
        return tuple((k, p, s) for k, (p, s) in self._items.items())

    def drop_folded(self, folded) -> int:
        """增量层已收下的条目从热层撤掉；期间又被改过的（序号变了）保留。"""
        removed = 0
        for key, seq in folded:
            cur = self._items.get(key)
            if cur is not None and cur[1] == seq:
                del self._items[key]
                removed += 1
        return removed

    def clear(self) -> None:
        self._items.clear()

    def restore_from_disk(self) -> int:
        """启动时接上上次没来得及并入增量层的热层条目，否则第一次重写会把它们覆盖掉。"""
        store = namestore.open_store(namestore.HOT)
        if store is None:
            return 0
        try:
            for i in range(store.count):
                self.note(store.entry(i)[0])
            return store.count
        finally:
            store.close()


class StatUnavailable(OSError):
    """文件还在但暂时读不到（共享冲突、权限）：调用方保留条目，下次再试。"""


def stat_entry(path: str):
    """返回 (path, size, mtime, is_dir)；确定已不存在返回 None；暂时读不到抛 StatUnavailable。"""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise StatUnavailable(str(exc)) from exc
    is_dir = os.path.isdir(path)
    return path, 0 if is_dir else int(st.st_size), st.st_mtime, is_dir


def write_hot_layer(snapshot) -> int:
    """后台线程：把热层快照写成一份小索引；没有条目就撤掉热层。返回写入条数。"""
    builder = namestore.NameStoreBuilder()
    try:
        count = 0
        for _key, path, _seq in snapshot:
            try:
                entry = stat_entry(path)
            except StatUnavailable:
                entry = (path, 0, 0.0, False)   # 名字照样可搜，大小/时间等下次补
            if entry is None:
                continue
            p, size, mtime, is_dir = entry
            if is_dir:
                builder.add_dir(p, mtime)
            else:
                builder.add(p, size, mtime)
            count += 1
        if count:
            builder.write(kind=namestore.HOT)
        else:
            namestore.discard(namestore.HOT)
        return count
    finally:
        builder.close()
