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
    [DIROFF]       u32 × dir_count      DIRS 段内每个目录的起始偏移
    [NORMOFF]      u32 × count          NORM 段内每条记录的起始偏移（供二分反查）
    [RECS]         每条 20 字节：dir_id u32 / name_off u32 / size u32 / mtime u32 / flags u32

size 用 u32 存（上限 4 GiB-1），超过就钉在 0xFFFFFFFF——这个字段只用来在结果里
显示大小，不参与任何判断，为几个超大文件把每条记录撑到 24 字节不划算。

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

from .config import data_dir
from .db import sqlite_safe_text
from .text_tokenize import normalize

log = logging.getLogger(__name__)

MAGIC = b"PPTDNAM\x01"
FORMAT_VERSION = 1
HEADER_SIZE = 128
RECORD_SIZE = 20
SIZE_CAP = 0xFFFFFFFF
FLAG_DIR = 1 << 0

# 段顺序固定，Header 里逐段记 (offset, length)
_SECTIONS = ("norm", "name", "dirs", "diroff", "normoff", "recs")
_HEADER_STRUCT = struct.Struct("<8sIIQ" + "QQ" * len(_SECTIONS))


POINTER_NAME = "names.idx"
DATA_PREFIX = "names-"
DATA_SUFFIX = ".idx"


def pointer_path() -> Path:
    """指针文件：内容是当前数据文件的文件名。它自己很小，永远不会被 mmap。"""
    return data_dir() / POINTER_NAME


def current_data_path() -> Path | None:
    """跟着指针找到当前的数据文件；指针不存在或指空返回 None。"""
    try:
        name = pointer_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None                      # 指针只允许写同目录下的文件名
    candidate = data_dir() / name
    return candidate if candidate.is_file() else None


def _sweep_old_data_files(keep: Path) -> None:
    """清掉不再被指向的旧数据文件。

    删不掉是常态而不是异常：还开着的搜索线程仍 mmap 着上一份，Windows 不让删。
    下次重建再扫一遍就好——所以这里失败一律咽掉，绝不能影响建库结果。
    """
    try:
        entries = list(data_dir().iterdir())
    except OSError:
        return
    for p in entries:
        if p == keep or not p.name.startswith(DATA_PREFIX):
            continue
        if not p.name.endswith(DATA_SUFFIX) or not p.is_file():
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
        self._diroff = array("I")
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
            self._dirs += _sanitize(directory).encode("utf-8", "surrogatepass") + b"\n"
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
        self._norm += normalize(
            sqlite_safe_text(base)).encode("utf-8", "surrogatepass") + b"\n"
        self._recs += struct.pack(
            "<IIIII", dir_id, name_off, int(size),
            max(0, min(int(mtime), 0xFFFFFFFF)), int(flags),
        )
        self._count += 1

    def write(self, dest: Path | None = None) -> Path:
        """原子落盘，返回数据文件路径。半个索引比没有索引更糟。

        dest 为空时走「版本化文件 + 指针」：每次重建写一个新名字的数据文件，
        再原子替换那个很小的指针文件。**不能就地覆盖**——Windows 上正在被搜索线程
        mmap 的文件是替换不掉的（WinError 5），一旦用户搜过一次「全部文件」，
        此后每轮建库都会静默地装不上新索引，索引就永远停在那一刻。
        指针文件从不被 mmap，所以替换它总是成功；老数据文件等没人用了再顺手清掉。
        """
        versioned = dest is None
        if versioned:
            dest = data_dir() / f"{DATA_PREFIX}{int(time.time() * 1000):013d}{DATA_SUFFIX}"
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        blocks = {
            "norm": bytes(self._norm),
            "name": bytes(self._name),
            "dirs": bytes(self._dirs),
            "diroff": self._diroff.tobytes(),
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
            _write_pointer(dest.name)
            _sweep_old_data_files(dest)
        return dest


def _write_pointer(data_name: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".ptr-", dir=str(data_dir()))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data_name.encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, pointer_path())
        tmp = ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class NameStoreError(RuntimeError):
    """索引文件缺失、版本不符或结构损坏。调用方据此降级，不要当致命错误。"""


class NameStore:
    """只读打开一个索引文件；线性扫描搜索。

    用 mmap 而不是读进内存：托盘常驻的程序，用户不搜的时候这 130 MB 应该能被
    操作系统换出去，而不是一直压在进程的私有内存里。
    """

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            resolved = current_data_path()
            if resolved is None:
                raise NameStoreError("还没有可用的文件名索引")
            path = resolved
        self.path = Path(path)
        self._fh = None
        self._mm: mmap.mmap | None = None
        self._span: dict[str, tuple[int, int]] = {}
        self._norm_offsets = array("I")
        self._dir_offsets = array("I")
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

    def close(self) -> None:
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
        return struct.unpack_from("<IIIII", self._mm, off)

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

    def search(self, terms: list[str], *, limit: int = 100_000,
               scope: str = "") -> list[int]:
        """返回命中记录的序号。多个词是「与」的关系，与 SQLite 那条路口径一致。

        做法就是 Everything 的做法：拿最长的那个词在归一化名字块里线性扫，
        扫到的每个位置二分回查是第几条记录，再用剩下的词逐条复核。
        用最长的词起手是因为它命中最少——候选集越小，后面复核越省。

        limit 是**召回上限**，不是最终条数：命中先全收（上限内），排序和截断交给
        调用方，否则截断出来的就是磁盘遍历顺序里靠前的那些，跟相关度毫无关系。
        """
        needles = [normalize(sqlite_safe_text(t)).encode("utf-8", "surrogatepass")
                   for t in terms if t and t.strip()]
        needles = [n for n in needles if n]
        if not needles or self.count == 0:
            return []
        needles.sort(key=len, reverse=True)
        primary, rest = needles[0], needles[1:]

        offsets = self._norm_offsets
        scope_norm = os.path.normcase(scope) if scope else ""
        out: list[int] = []
        seen_last = -1
        pos = self._find_in_norm(primary, 0)
        while pos >= 0:
            # 同一条记录里出现多次只算一条；find 是单调前进的，所以同一条的
            # 多次命中必然相邻，比一次 seen_last 就够，不必开集合
            i = bisect.bisect_right(offsets, pos) - 1
            if i >= 0 and i != seen_last:
                seen_last = i
                if self._matches(i, rest, scope_norm):
                    out.append(i)
                    if len(out) >= limit:
                        break
            pos = self._find_in_norm(primary, pos + 1)
        return out

    def _matches(self, i: int, rest: list[bytes], scope_norm: str) -> bool:
        if rest:
            name = self._slice_until_newline("norm", self._norm_offsets[i])
            if not all(n in name for n in rest):
                return False
        if scope_norm:
            directory = self.directory(self._record(i)[0])
            if not os.path.normcase(directory).startswith(scope_norm):
                return False
        return True
