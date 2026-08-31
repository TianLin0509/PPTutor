# -*- coding: utf-8 -*-
"""全盘文件名索引：一个平铺文件 + 线性扫描，照搬 Everything 的存储设计。

## 为什么不继续用 SQLite

「按名字找任意文件」和「搜 PPT 里写过的字」是两个不同的问题，硬塞进同一张表
要付两笔冤枉钱。真机实测（1,752,016 个文件、190,161 个目录）：

    SQLite 做法   1,219 MB   每行 731 字节
    本模块         约 130 MB   9.7 : 1

省下来的钱来自两处，都是 Everything 的做法：

1. **目录只记一次**。175 万个文件只分布在 19 万个目录里，平均 9 个文件共用一个
   目录。SQLite 每行存一遍完整路径（平均 112 字符），于是同目录下那一长串前缀
   被完整重复了 9 遍。这里改成「目录表存一份，文件只记 dir_id + 自己的名字」。
2. **不建倒排索引**。SQLite 那边要 file_names_fts + name_norm + idx_files_name
   三套结构（合计 195 字节/行）才能让「打三个字立刻出结果」不退化成读盘全表扫。
   本模块把归一化名字打包成一块连续内存，用 bytes.find 从头硬扫——实测 38 MB
   的名字块扫一遍 13~25 ms，比建索引再查还省事，于是那 195 字节一分不花。

Everything 还有第三招我们学不了：直接读 NTFS 的 MFT。那要管理员权限，而本程序
是绿色免安装、不提权跑的。但那一招决定的是**扫得多快**（它 2 秒，我们 104 秒），
不是**占多大**——而那趟扫盘为了找 PPT 本来每周就在做，所以并不多付代价。

## 文件格式

单个平铺文件，mmap 映射进来。托盘空闲时操作系统可以整个换出去，常驻内存≈0。

    [Header 128B]  magic/版本/各段偏移与长度/条目数
    [NORM]         归一化后的文件名，'\n' 分隔，按记录顺序     ← 搜索扫这一段
    [NAME]         原始文件名，'\n' 分隔，按记录顺序
    [DIRS]         目录全路径，'\n' 分隔，去重后
    [DIRNORM]      目录全路径的归一化形式，与 DIRS 一一对应   ← 路径匹配扫这一段
    [DIROFF]       u32 × dir_count      DIRS 段内每个目录的起始偏移
    [DIRNORMOFF]   u32 × dir_count      DIRNORM 段内每个目录的起始偏移
    [NORMOFF]      u32 × count          NORM 段内每条记录的起始偏移（供二分反查）
    [RECS]         每条 24 字节：dir_id u32 / name_off u32 / size u64 / mtime u32 / flags u32

size 用 u64 存。v3 曾用 u32 并声称“只显示不判断”，但 `size:` 查询实际直接读取
这个字段，导致所有大于 4 GiB 的文件被截断并被 `size:>4gb` 静默漏掉。每条多 4 B，
200 万条约 8 MB，换来正确的大小过滤与全局大小排序。

**文件夹和文件在同一个记录表里**，靠 flags 的 FLAG_DIR 位区分。Everything 能按
名字搜到文件夹，我们也要能——分成两张表会让搜索得扫两遍、排序还得再合并一次，
为一个 bit 不值得。文件夹记录的 dir_id 指向它的父目录，size 恒为 0。
"""
from __future__ import annotations

import bisect
import logging
import mmap
import os
import struct
import tempfile
import time
from array import array
from pathlib import Path

from . import namequery
from .config import data_dir
from .db import sqlite_safe_text
from .text_tokenize import normalize

log = logging.getLogger(__name__)

MAGIC = b"PPTDNAM\x01"
FORMAT_VERSION = 4
#: 头部预留大小。留足余量：每加一个段就多 16 字节，装不下的话
#: `header[:len(packed)] = packed` 会把 bytearray 撑长，于是头部悄悄盖掉第一个段
#: 的开头、所有偏移全错——不报错，只是搜出来的东西是乱的。下面有断言兜底。
HEADER_SIZE = 256
_RECORD_STRUCT = struct.Struct("<IIQII")
RECORD_SIZE = _RECORD_STRUCT.size
SIZE_CAP = 0xFFFFFFFFFFFFFFFF
FLAG_DIR = 1 << 0

# 段顺序固定，Header 里逐段记 (offset, length)
_SECTIONS = ("norm", "name", "dirs", "dirnorm", "diroff", "dirnormoff",
             "normoff", "recs")
_HEADER_STRUCT = struct.Struct("<8sIIQ" + "QQ" * len(_SECTIONS))
assert _HEADER_STRUCT.size <= HEADER_SIZE, (
    f"头部装不下了：需要 {_HEADER_STRUCT.size} 字节，只预留了 {HEADER_SIZE}")


DATA_SUFFIX = ".idx"
# 两套索引共用一模一样的格式与代码：
#   main    —— 每轮全盘扫描整份重建，是绝大多数结果的来源
#   overlay —— 目录监听发现变化后按目录重建的增量层，体积很小
# Everything 在拿不到 NTFS 变更日志时走的也是目录监听这条路，我们照搬。
MAIN = "names"
OVERLAY = "names-overlay"
KINDS = (MAIN, OVERLAY)


def pointer_path(kind: str = MAIN) -> Path:
    """指针文件：内容是当前数据文件的文件名。它自己很小，永远不会被 mmap。"""
    return data_dir() / f"{kind}{DATA_SUFFIX}"


def _data_prefix(kind: str) -> str:
    return f"{kind}-"


def current_data_path(kind: str = MAIN) -> Path | None:
    """跟着指针找到当前的数据文件；指针不存在或指空返回 None。"""
    try:
        name = pointer_path(kind).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None                      # 指针只允许写同目录下的文件名
    if not name.startswith(_data_prefix(kind)):
        return None
    candidate = data_dir() / name
    return candidate if candidate.is_file() else None


def open_store(kind: str = MAIN):
    """打开某一套索引；没有就返回 None（调用方据此降级，不当异常处理）。"""
    path = current_data_path(kind)
    if path is None:
        return None
    try:
        return NameStore(path)
    except NameStoreError as exc:
        log.info("name index (%s) unavailable: %s", kind, exc)
        return None


def discard(kind: str) -> None:
    """作废某一套索引：先撤指针，再尽力删数据文件。

    全盘重建之后必须把增量层撤掉——它记的是「上一份全量之后的变化」，
    留着只会让已经并入全量的条目重复出现（虽然按路径去重了，但纯属浪费）。
    """
    try:
        pointer_path(kind).unlink()
    except OSError:
        pass
    _sweep_old_data_files(None, kind)


def _sweep_old_data_files(keep: Path | None, kind: str = MAIN) -> None:
    """清掉不再被指向的旧数据文件。

    删不掉是常态而不是异常：还开着的搜索线程仍 mmap 着上一份，Windows 不让删。
    下次重建再扫一遍就好——所以这里失败一律咽掉，绝不能影响建库结果。
    """
    prefix = _data_prefix(kind)
    try:
        entries = list(data_dir().iterdir())
    except OSError:
        return
    for p in entries:
        if p == keep or not p.is_file():
            continue
        # 只认 <前缀><纯数字>.idx。不能只看前缀——"names-" 同时是 "names-overlay-"
        # 和指针文件 "names-overlay.idx" 的前缀，按前缀删会把增量层连指针一起抹掉
        # （新文件当场从搜索里消失，而且不报错）。
        stem = p.name[len(prefix): -len(DATA_SUFFIX)]
        if not p.name.startswith(prefix) or not p.name.endswith(DATA_SUFFIX):
            continue
        if not stem.isdigit():
            continue
        try:
            p.unlink()
        except OSError:
            pass


def _sanitize(name: str) -> str:
    r"""名字里不能有 '\n'——它是段内的记录分隔符。

    Windows 文件名本来就不允许换行，但索引的是别人给的磁盘，遇到走私进来的
    分隔符必须当场换掉，否则一条记录会被劈成两条、后面全错位。
    """
    return name.replace("\n", " ").replace("\r", " ") if ("\n" in name or "\r" in name) else name


class NameStoreBuilder:
    """边扫盘边攒，最后一次性落盘。

    内存峰值就是最终文件的大小（真机约 130 MB），全程只在结尾写一次盘。
    """

    def __init__(self) -> None:
        self._dir_ids: dict[str, int] = {}
        self._dirs = bytearray()
        self._dirnorm = bytearray()
        self._diroff = array("I")
        self._dirnormoff = array("I")
        self._norm = bytearray()
        self._normoff = array("I")
        self._name = bytearray()
        self._recs = bytearray()
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def _dir_id(self, directory: str) -> int:
        dir_id = self._dir_ids.get(directory)
        if dir_id is None:
            dir_id = len(self._dir_ids)
            self._dir_ids[directory] = dir_id
            self._diroff.append(len(self._dirs))
            self._dirnormoff.append(len(self._dirnorm))
            clean = _sanitize(directory)
            self._dirs += clean.encode("utf-8", "surrogatepass") + b"\n"
            # 目录的归一化形式在**建库时**算好存进去。放到查询时算的话，25 万个目录
            # 每次都要现跑一遍 NFKC + OpenCC——真机实测头一条路径查询要为此多花约
            # 2.7 秒。存进来只多占 13% 体积，而且路径匹配可以直接在这一段上做
            # C 速度的子串扫描，连解码都省了。
            self._dirnorm += namequery.fold(clean).replace(
                "\\", "/").encode("utf-8", "surrogatepass") + b"\n"
        return dir_id

    def add_dir(self, path: str, mtime: float = 0.0) -> None:
        """登记一个文件夹本身，让它能被按名字搜到（Everything 也是这么做的）。

        文件夹同时还会作为**别人的父目录**进 DIRS 表——那是两件事：DIRS 表是
        用来拼路径的，这里加的是「文件夹自己作为一条可搜结果」。
        """
        parent, base = os.path.split(path.rstrip("\\/") or path)
        if not base:                    # 盘符根（C:\）没有名字可搜，跳过
            return
        self._append(self._dir_id(parent), base, 0, mtime, FLAG_DIR)

    def add(self, path: str, size: int, mtime: float) -> None:
        directory, base = os.path.split(path)
        self._append(self._dir_id(directory), base,
                     min(int(size), SIZE_CAP) if size and size > 0 else 0,
                     mtime, 0)

    def _append(self, dir_id: int, base: str, size: int,
                mtime: float, flags: int) -> None:
        base = _sanitize(base)
        name_off = len(self._name)
        # 原始名字原样存（surrogatepass 扛得住孤立代理项），这样 entry() 交回去的
        # 路径永远能真的打开那个文件。
        self._name += base.encode("utf-8", "surrogatepass") + b"\n"
        self._normoff.append(len(self._norm))
        # 归一化副本必须与 SQLite 那条路逐字节同口径（db.py 也是先 sqlite_safe_text
        # 再 normalize），否则两边搜同一个词会得出不同结果。顺带挡住 OpenCC——
        # 它拿到孤立代理项会直接抛 UnicodeEncodeError，真机上足以让整轮扫盘半途炸掉。
        # fold = normalize + 去掉变音符号。索引与查询必须调同一个函数，否则
        # 打 resume 找不到 résumé——两边口径不一致就等于搜不到。
        self._norm += namequery.fold(base).encode("utf-8", "surrogatepass") + b"\n"
        self._recs += _RECORD_STRUCT.pack(
            dir_id, name_off, int(size),
            max(0, min(int(mtime), 0xFFFFFFFF)), int(flags),
        )
        self._count += 1

    def write(self, dest: Path | None = None, *, kind: str = MAIN) -> Path:
        """原子落盘，返回数据文件路径。半个索引比没有索引更糟。

        dest 为空时走「版本化文件 + 指针」：每次重建写一个新名字的数据文件，
        再原子替换那个很小的指针文件。**不能就地覆盖**——Windows 上正在被搜索线程
        mmap 的文件是替换不掉的（WinError 5），一旦用户搜过一次「全部文件」，
        此后每轮建库都会静默地装不上新索引，索引就永远停在那一刻。
        指针文件从不被 mmap，所以替换它总是成功；老数据文件等没人用了再顺手清掉。
        """
        versioned = dest is None
        if versioned:
            dest = (data_dir()
                    / f"{_data_prefix(kind)}{int(time.time() * 1000):013d}{DATA_SUFFIX}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        blocks = {
            "norm": bytes(self._norm),
            "name": bytes(self._name),
            "dirs": bytes(self._dirs),
            "dirnorm": bytes(self._dirnorm),
            "diroff": self._diroff.tobytes(),
            "dirnormoff": self._dirnormoff.tobytes(),
            "normoff": self._normoff.tobytes(),
            "recs": bytes(self._recs),
        }
        offset = HEADER_SIZE
        spans: list[tuple[int, int]] = []
        for key in _SECTIONS:
            spans.append((offset, len(blocks[key])))
            offset += len(blocks[key])

        header = bytearray(HEADER_SIZE)
        packed = _HEADER_STRUCT.pack(
            MAGIC, FORMAT_VERSION, self._count, int(time.time()),
            *[v for span in spans for v in span],
        )
        header[: len(packed)] = packed

        fd, tmp = tempfile.mkstemp(prefix=".names-", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(header)
                for key in _SECTIONS:
                    out.write(blocks[key])
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if versioned:
            _write_pointer(dest.name, kind)
            _sweep_old_data_files(dest, kind)
        return dest


#: 替换指针文件的重试次数与间隔。指针从不被 mmap，但读它的那一瞬间
#: （current_data_path 的 read_text）Windows 照样不让替换：Python 的 open()
#: 不带 FILE_SHARE_DELETE，于是 os.replace 会撞 WinError 5。
#: 真机压测：3 个搜索线程 + 换库 8 次，失败 1 次；6 个线程死循环搜 + 换库 40 次，
#: **失败 39 次**。失败的后果是整轮全盘扫描白干、索引停在上一份，而且不报错。
_POINTER_RETRIES = 8
_POINTER_RETRY_SLEEP = 0.01


def _write_pointer(data_name: str, kind: str = MAIN) -> None:
    """把「当前数据文件叫什么」写进指针文件。

    正常路径是「写临时文件 + os.replace」——原子、读者要么看到旧的要么看到新的。
    但 Windows 上只要有读者正开着指针，replace 就失败；读者一多，重试也未必等得到
    空档（实测 6 线程死循环下 40 次能失败 6 次）。所以退让几次之后**改为就地重写**：

      · 就地写会被并发读者看到半截内容，但 current_data_path 对读到的东西是校验过的
        （必须以 `<kind>-` 打头、且那个文件真的存在），半截名字一律当「没有索引」。
        代价是那一次查询降级成空结果，下一次就好了。
      · 相比之下，装不上新索引是**永久**的：用户此后搜到的一直是旧快照。
        一次性的瞬时降级换掉一个永久性的错误，这笔账是划算的。
    """
    fd, tmp = tempfile.mkstemp(prefix=".ptr-", dir=str(data_dir()))
    payload = data_name.encode("utf-8")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        target = pointer_path(kind)
        for attempt in range(_POINTER_RETRIES):
            try:
                os.replace(tmp, target)
                tmp = ""
                return
            except PermissionError:
                if attempt < _POINTER_RETRIES - 1:
                    time.sleep(_POINTER_RETRY_SLEEP)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    # 兜底：就地重写。读者拿的是共享读句柄，不挡写。
    # 用 r+b 而不是 wb：wb 会先把文件截成 0，读者正好撞上就读到空。r+b 不截断，
    # 而两代文件名长度恒等（都是 `<kind>-` + 13 位毫秒时间戳 + `.idx`），所以
    # 半截读到的要么是旧名、要么是新名、要么是个数字混合的不存在的名字——
    # 三种都被 current_data_path 的校验兜住，不会指向别的东西。
    log.info("name index pointer (%s) busy, rewriting in place", kind)
    target = pointer_path(kind)
    try:
        handle = open(target, "r+b")
    except FileNotFoundError:
        handle = open(target, "wb")
    with handle as out:
        out.write(payload)
        out.truncate(len(payload))
        out.flush()
        os.fsync(out.fileno())


class NameStoreError(RuntimeError):
    """索引文件缺失、版本不符或结构损坏。调用方据此降级，不要当致命错误。"""


class NameStore:
    """只读打开一个索引文件；线性扫描搜索。

    用 mmap 而不是读进内存：托盘常驻的程序，用户不搜的时候这 130 MB 应该能被
    操作系统换出去，而不是一直压在进程的私有内存里。
    """

    def __init__(self, path: Path | None = None, *, kind: str = MAIN) -> None:
        if path is None:
            resolved = current_data_path(kind)
            if resolved is None:
                raise NameStoreError("还没有可用的文件名索引")
            path = resolved
        self.path = Path(path)
        self._fh = None
        self._mm: mmap.mmap | None = None
        self._span: dict[str, tuple[int, int]] = {}
        self._norm_offsets = array("I")
        self._dir_offsets = array("I")
        self._dirnorm_offsets = array("I")
        self._name_offs: array | None = None
        self._path_keys_cache: set[str] | None = None
        self.count = 0
        self.built_at = 0
        self._open()

    def _open(self) -> None:
        try:
            self._fh = open(self.path, "rb")
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError) as exc:
            self.close()
            raise NameStoreError(f"打不开文件名索引: {exc}") from exc
        try:
            self._parse_header()
        except Exception:
            self.close()
            raise

    def _parse_header(self) -> None:
        mm = self._mm
        assert mm is not None
        if len(mm) < HEADER_SIZE:
            raise NameStoreError("文件名索引被截断")
        fields = _HEADER_STRUCT.unpack(mm[: _HEADER_STRUCT.size])
        magic, version, count, built_at = fields[:4]
        if magic != MAGIC:
            raise NameStoreError("不是文件名索引文件")
        if version != FORMAT_VERSION:
            raise NameStoreError(f"文件名索引版本不符（{version} != {FORMAT_VERSION}）")
        self.count = int(count)
        self.built_at = int(built_at)
        spans = fields[4:]
        for i, key in enumerate(_SECTIONS):
            off, length = int(spans[i * 2]), int(spans[i * 2 + 1])
            if off < HEADER_SIZE or off + length > len(mm):
                raise NameStoreError(f"文件名索引的 {key} 段越界")
            self._span[key] = (off, length)
        if self._span["recs"][1] != self.count * RECORD_SIZE:
            raise NameStoreError("文件名索引的记录数与段长不符")
        if self._span["normoff"][1] != self.count * 4:
            raise NameStoreError("文件名索引的偏移表与记录数不符")
        # 二分反查用：把 normoff 段读成一个真正的数组（1.75M × 4B = 7 MB）
        off, length = self._span["normoff"]
        self._norm_offsets = array("I")
        self._norm_offsets.frombytes(mm[off: off + length])
        off, length = self._span["diroff"]
        self._dir_offsets = array("I")
        self._dir_offsets.frombytes(mm[off: off + length])
        off, length = self._span["dirnormoff"]
        self._dirnorm_offsets = array("I")
        self._dirnorm_offsets.frombytes(mm[off: off + length])

    def close(self) -> None:
        self._path_keys_cache = None
        if self._mm is not None:
            try:
                self._mm.close()
            except (BufferError, ValueError):
                pass
            self._mm = None
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __enter__(self) -> "NameStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 取字段

    def _record(self, i: int) -> tuple[int, int, int, int, int]:
        off = self._span["recs"][0] + i * RECORD_SIZE
        assert self._mm is not None
        return _RECORD_STRUCT.unpack_from(self._mm, off)

    def _slice_until_newline(self, key: str, start: int) -> bytes:
        off, length = self._span[key]
        assert self._mm is not None
        end = self._mm.find(b"\n", off + start, off + length)
        if end < 0:
            end = off + length
        return self._mm[off + start: end]

    def norm_name(self, i: int) -> str:
        return self._slice_until_newline(
            "norm", self._norm_offsets[i]).decode("utf-8", "surrogatepass")

    def raw_name(self, i: int) -> str:
        return self._slice_until_newline(
            "name", self._record(i)[1]).decode("utf-8", "surrogatepass")

    def directory(self, dir_id: int) -> str:
        return self._slice_until_newline(
            "dirs", self._dir_offsets[dir_id]).decode("utf-8", "surrogatepass")

    def entry(self, i: int) -> tuple[str, str, int, int, bool]:
        """第 i 条记录 → (完整路径, 名字, 字节数, mtime, 是否文件夹)。"""
        dir_id, _name_off, size, mtime, flags = self._record(i)
        name = self.raw_name(i)
        directory = self.directory(dir_id)
        return (os.path.join(directory, name) if directory else name,
                name, int(size), int(mtime), bool(flags & FLAG_DIR))

    def path_keys(self) -> set[str]:
        """Cached case-insensitive paths, intended for the small overlay store."""
        if self._path_keys_cache is None:
            self._path_keys_cache = {
                os.path.normcase(self.entry(i)[0]) for i in range(self.count)
            }
        return self._path_keys_cache

    # ---------------------------------------------------------------- 搜索

    def _find_in_norm(self, needle: bytes, start_rel: int) -> int:
        """在 norm 段里找子串，返回段内相对偏移；找不到 -1。

        直接问底层 mmap 要（它自带 find 且接受绝对起止），全程零拷贝——
        把 38 MB 的段 bytes() 出来再 find 会把省下的内存又还回去。
        """
        off, length = self._span["norm"]
        assert self._mm is not None
        hit = self._mm.find(needle, off + start_rel, off + length)
        return -1 if hit < 0 else hit - off

    def dir_norm(self, dir_id: int) -> str:
        """某个目录的归一化路径。建库时已经算好，这里只是切一段出来。"""
        return self._slice_until_newline(
            "dirnorm", self._dirnorm_offsets[dir_id]).decode("utf-8", "surrogatepass")

    def _dirs_matching(self, needle: str) -> set[int]:
        """归一化路径里含 needle 的目录 id。

        直接在 DIRNORM 段上做 C 速度的子串扫描，一个目录字符串都不解码——
        25 万个目录逐个 decode 再比对是查询延迟的大头。
        """
        off, length = self._span["dirnorm"]
        assert self._mm is not None
        encoded = needle.encode("utf-8", "surrogatepass")
        offsets = self._dirnorm_offsets
        out: set[int] = set()
        pos = self._mm.find(encoded, off, off + length)
        while pos >= 0:
            out.add(bisect.bisect_right(offsets, pos - off) - 1)
            pos = self._mm.find(encoded, pos + 1, off + length)
        return out

    def record_for(self, i: int) -> "_LazyRecord":
        """第 i 条 → 判定用的记录。字段全部惰性求值：查询用不到的字段不解码。"""
        return _LazyRecord(self, i)

    @staticmethod
    def _check_cancel(cancel, counter: int = 0) -> None:
        if cancel is not None and (counter & 0x3FF) == 0 and bool(cancel()):
            raise RuntimeError("interrupted")

    def search(
        self, query, *, limit: int = 100_000, scope: str = "", cancel=None,
    ) -> list[int]:
        """返回命中记录的序号。

        两段式，与 Everything 一样：
        ① **预筛**——从查询里抠出「必然出现在名字里的字面串」，拿最长的那个在归一化
           名字块里线性扫（`bytes.find`，C 速度），扫到的位置二分回查是第几条记录。
        ② **复核**——只对预筛出来的候选跑完整判定（通配符、大小、日期、非、正则）。

        抠不出字面串时（比如只写了 `size:>1gb`）退化成逐条全扫。这条路慢得多，
        所以能抠就抠——`*.pdf` 抠得出 `.pdf`，`ext:docx` 抠得出 `.docx`。

        limit 是**召回上限**，不是最终条数：排序和截断交给调用方，否则截出来的
        就是磁盘遍历顺序里靠前的那些，跟相关度毫无关系。
        """
        out: list[int] = []
        for i in self.iter_search(query, scope=scope, cancel=cancel):
            out.append(i)
            if len(out) >= max(1, int(limit)):
                break
        return sorted(out)

    def iter_search(self, query, *, scope: str = "", cancel=None):
        """Yield every matching ordinal, allowing global top-K sorting upstream."""
        if isinstance(query, (list, tuple)):
            query = namequery.from_terms(query)
        if not query or self.count == 0:
            return
        scope_norm = os.path.normcase(scope) if scope else ""
        branches = query.prefilter()
        if branches is None:
            yield from self._iter_scan_all(query, scope_norm, cancel)
        else:
            yield from self._iter_scan_prefiltered(query, branches, scope_norm, cancel)

    def _accept(self, i: int, query, scope_norm: str) -> bool:
        if scope_norm:
            directory = self.directory(self._record(i)[0])
            if not os.path.normcase(directory).startswith(scope_norm):
                return False
        return query.match(self.record_for(i))

    def _iter_scan_prefiltered(self, query, branches, scope_norm, cancel):
        offsets = self._norm_offsets
        found: set[int] = set()
        checked = 0
        for needles in branches:
            encoded = [n.encode("utf-8", "surrogatepass") for n in needles if n]
            if not encoded:
                yield from self._iter_scan_all(query, scope_norm, cancel)
                return
            # 用最长的那个起手：它命中最少，候选集越小后面复核越省
            primary = max(encoded, key=len)
            seen_last = -1
            pos = self._find_in_norm(primary, 0)
            while pos >= 0:
                # find 单调前进，所以同一条记录的多次命中必然相邻，比一次就够
                i = bisect.bisect_right(offsets, pos) - 1
                if i >= 0 and i != seen_last:
                    seen_last = i
                    checked += 1
                    self._check_cancel(cancel, checked)
                    if i not in found and self._accept(i, query, scope_norm):
                        found.add(i)
                        yield i
                pos = self._find_in_norm(primary, pos + 1)

    def _iter_scan_all(self, query, scope_norm, cancel):
        if not scope_norm and query.needs() <= namequery.Query.METADATA_ONLY:
            yield from self._iter_scan_metadata_only(query, cancel)
            return
        candidates = self._path_candidates(query, cancel=cancel)
        if candidates is None:
            candidates = self._regex_candidates(query, cancel=cancel)
        source = range(self.count) if candidates is None else candidates
        for i in source:
            self._check_cancel(cancel, i)
            if self._accept(i, query, scope_norm):
                yield i

    def _path_candidates(self, query, *, cancel=None) -> list[int] | None:
        """靠目录表把路径查询的候选先压小；压不动就返回 None（老老实实全扫）。

        路径 = 目录 + '/' + 名字，而目录只有 25 万个、被 200 万个文件共享。
        先在目录里找，再按三种情形收候选：
          ① 目录本身就含这个串 → 该目录下所有文件都算候选；
          ② 串里带 '/'（明显在描述位置）→ 只可能跨边界，即「目录以 head 结尾
             且 名字以 tail 开头」，按这个条件收；
          ③ 串里不带 '/' → 还可能整个落在文件名里，用名字块的快扫补上。
        实测 `src/pptx_finder` 4.7 秒 → 见下方压测。
        """
        literals = query.path_literals()
        if not literals:
            return None
        needle = max(literals, key=len)
        hit_dirs = self._dirs_matching(needle)

        head, sep, tail = needle.rpartition("/")
        boundary: dict[int, str] = {}
        if sep and tail:
            # 「目录以 head 结尾」＝ DIRNORM 段里含 "/head\n"（每条都以换行收尾），
            # 于是这一步同样是一次 C 速度的扫描，不必逐个目录取出来比
            for d in self._dirs_matching("/" + head + "\n"):
                if d not in hit_dirs:
                    boundary[d] = tail

        out: list[int] = []
        off, length = self._span["recs"]
        assert self._mm is not None
        raw = memoryview(self._mm)[off: off + length]
        try:
            for i, (dir_id, _n, _s, _m, _f) in enumerate(
                    _RECORD_STRUCT.iter_unpack(raw)):
                self._check_cancel(cancel, i)
                if dir_id in hit_dirs:
                    out.append(i)
                elif dir_id in boundary and self.norm_name(i).startswith(boundary[dir_id]):
                    out.append(i)
        finally:
            raw.release()
        if not sep:
            # 串整个落在文件名里的情形，用名字块快扫补齐（'/' 不可能出现在文件名里，
            # 所以带分隔符时这一步没有意义）
            seen = set(out)
            for i in self._name_hits(needle):
                if i not in seen:
                    out.append(i)
            out.sort()
        return out

    def _name_offsets(self) -> array:
        """每条记录在 NAME 段里的起始偏移。按录入顺序单调递增，可直接二分。

        只在正则查询时才建（200 万条约 0.3 秒），建好挂在 store 上复用。
        """
        if self._name_offs is None:
            off, length = self._span["recs"]
            assert self._mm is not None
            raw = memoryview(self._mm)[off: off + length]
            try:
                self._name_offs = array(
                    "I", (r[1] for r in _RECORD_STRUCT.iter_unpack(raw)))
            finally:
                raw.release()
        return self._name_offs

    def _regex_candidates(self, query, *, cancel=None) -> list[int] | None:
        """正则查询：在 NAME 段上整块跑一遍，把命中位置回查成记录序号。"""
        pattern = query.regex_candidates()
        if pattern is None:
            return None
        off, length = self._span["name"]
        assert self._mm is not None
        blob = self._mm[off: off + length]
        offsets = self._name_offsets()
        out: list[int] = []
        seen_last = -1
        try:
            for n, m in enumerate(pattern.finditer(
                    blob, timeout=namequery.REGEX_BLOCK_TIMEOUT_SEC)):
                self._check_cancel(cancel, n)
                i = bisect.bisect_right(offsets, m.start()) - 1
                if i >= 0 and i != seen_last:
                    seen_last = i
                    out.append(i)
        except TimeoutError as exc:
            raise namequery.QueryError("正则扫描超时，请简化表达式") from exc
        return sorted(set(out))

    def _name_hits(self, needle: str) -> list[int]:
        encoded = needle.encode("utf-8", "surrogatepass")
        offsets = self._norm_offsets
        out: list[int] = []
        seen_last = -1
        pos = self._find_in_norm(encoded, 0)
        while pos >= 0:
            i = bisect.bisect_right(offsets, pos) - 1
            if i >= 0 and i != seen_last:
                seen_last = i
                out.append(i)
            pos = self._find_in_norm(encoded, pos + 1)
        return out

    def fuzzy_candidates(
        self, anchors, *, limit: int = 20_000, cancel=None,
    ) -> list[int]:
        """Union name n-gram hits; full fuzzy verification happens upstream."""
        found: set[int] = set()
        for anchor in anchors or ():
            for i in self._name_hits(str(anchor)):
                self._check_cancel(cancel, len(found))
                found.add(i)
                if len(found) >= max(1, int(limit)):
                    return sorted(found)
        return sorted(found)

    def entries_for_directories(self, directories, *, cancel=None) -> dict[str, tuple]:
        """Return direct-child metadata for exact directories in one record pass.

        The live overlay uses this to store only entries that differ from the
        last full snapshot.  Without a baseline, one noisy 34k-entry cache
        directory is copied wholesale and permanently consumes the 50k overlay.
        """
        wanted_ids: set[int] = set()
        for directory in directories or ():
            clean = os.path.abspath(str(directory))
            needle = namequery.fold(clean).replace("\\", "/")
            for dir_id in self._dirs_matching(needle):
                if self.dir_norm(dir_id) == needle:
                    wanted_ids.add(dir_id)
        if not wanted_ids:
            return {}
        out: dict[str, tuple] = {}
        off, length = self._span["recs"]
        assert self._mm is not None
        raw = memoryview(self._mm)[off: off + length]
        try:
            for i, (dir_id, _name, _size, _mtime, _flags) in enumerate(
                    _RECORD_STRUCT.iter_unpack(raw)):
                self._check_cancel(cancel, i)
                if dir_id not in wanted_ids:
                    continue
                path, name, size, mtime, is_dir = self.entry(i)
                out[os.path.normcase(path)] = (path, name, size, mtime, is_dir)
        finally:
            raw.release()
        return out

    def _iter_scan_metadata_only(self, query, cancel):
        """只看大小/时间/是否目录的查询：直接遍历定长记录，一个字符串都不解码。

        `size:` `dm:` `folder:` 这类查询抠不出任何名字片段，只能全扫；但它们
        根本不需要名字。真机 200 万条实测 6.4 秒 → 0.3 秒。
        """
        off, length = self._span["recs"]
        assert self._mm is not None
        raw = memoryview(self._mm)[off: off + length]
        root = query.root
        try:
            for i, (_dir_id, _name_off, size, mtime, flags) in enumerate(
                    _RECORD_STRUCT.iter_unpack(raw)):
                self._check_cancel(cancel, i)
                if root.match(_MetaRecord(size, mtime, bool(flags & FLAG_DIR))):
                    yield i
        finally:
            raw.release()


class _MetaRecord:
    """只带元数据的记录：给「不碰名字」的查询用，省掉全部字符串解码。"""

    __slots__ = ("size", "mtime", "is_dir")

    def __init__(self, size: int, mtime: int, is_dir: bool) -> None:
        self.size = size
        self.mtime = mtime
        self.is_dir = is_dir


class _LazyRecord:
    """按需解码的记录。全扫时大部分字段根本用不到，提前算就是白算。"""

    __slots__ = ("_store", "_i", "_rec", "_name", "_name_norm", "_path", "_path_norm")

    def __init__(self, store: "NameStore", i: int) -> None:
        self._store = store
        self._i = i
        self._rec = None
        self._name = None
        self._name_norm = None
        self._path = None
        self._path_norm = None

    @property
    def _record(self):
        if self._rec is None:
            self._rec = self._store._record(self._i)
        return self._rec

    @property
    def size(self) -> int:
        return self._record[2]

    @property
    def mtime(self) -> int:
        return self._record[3]

    @property
    def is_dir(self) -> bool:
        return bool(self._record[4] & FLAG_DIR)

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = self._store.raw_name(self._i)
        return self._name

    @property
    def name_norm(self) -> str:
        if self._name_norm is None:
            self._name_norm = self._store.norm_name(self._i)
        return self._name_norm

    @property
    def path(self) -> str:
        if self._path is None:
            directory = self._store.directory(self._record[0])
            self._path = os.path.join(directory, self.name) if directory else self.name
        return self._path

    @property
    def path_norm(self) -> str:
        """整条路径的归一化形式，用「目录归一化（已缓存）+ 名字归一化」拼出来。

        直接 normalize(path) 会对每条记录跑一遍 NFKC + OpenCC——200 万条 27 秒。
        目录部分被大量共享，算一次存着即可。
        """
        if self._path_norm is None:
            d = self._store.dir_norm(self._record[0])
            self._path_norm = f"{d}/{self.name_norm}" if d else self.name_norm
        return self._path_norm
