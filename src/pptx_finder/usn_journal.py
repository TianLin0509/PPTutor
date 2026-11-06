"""NTFS 变更日志（USN Journal）的只读访问：不需要管理员权限。

为什么用它：系统内核把卷上每个文件的新建、改写、改名、删除都记进一本流水账，
重启也不丢。比起「目录监听 + 全盘重扫」有三点根本优势：

1. **不丢事件**。ReadDirectoryChangesW 的缓冲区一满，事件就静默丢失；流水账是
   持久的，读慢了也只是晚一点读到。
2. **离线也算数**。程序没开的时候发生的变化，下次启动从上次读到的位置接着读
   （前提是这段流水还没被覆盖，日志是定长循环的）。
3. **没有变化时不花钱**。一次查询只是一次 DeviceIoControl；不用为每个事件在
   Python 里走一遍，也不用整盘遍历。

权限上有个容易误判的地方：传统的 FSCTL_READ_USN_JOURNAL 要管理员，而且要打开
卷设备句柄；但 Windows 10 起有 FSCTL_READ_UNPRIVILEGED_USN_JOURNAL，普通用户对
卷根目录开一个零权限句柄就能读（2026-11-06 本机非管理员实测）。代价是记录里的
文件名被抹掉（FileNameLength=0），所以要靠文件编号（FRN）用 OpenFileById 反查
路径，这正是本模块的另一半工作。
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
from ctypes import wintypes
from dataclasses import dataclass

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_UNPRIVILEGED_USN_JOURNAL = 0x000903AB

USN_REASON_DATA_OVERWRITE = 0x00000001
USN_REASON_DATA_EXTEND = 0x00000002
USN_REASON_DATA_TRUNCATION = 0x00000004
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000

#: 关心的变化类型。不含元数据变化（属性、权限、时间戳、对象 ID 等），
#: 杀毒软件和资源管理器的缩略图服务一天能产生几十万条，对索引毫无意义。
REASON_MASK = (
    USN_REASON_DATA_OVERWRITE
    | USN_REASON_DATA_EXTEND
    | USN_REASON_DATA_TRUNCATION
    | USN_REASON_FILE_CREATE
    | USN_REASON_FILE_DELETE
    | USN_REASON_RENAME_OLD_NAME
    | USN_REASON_RENAME_NEW_NAME
)

FILE_ATTRIBUTE_DIRECTORY = 0x00000010

ERROR_INVALID_FUNCTION = 1
ERROR_HANDLE_EOF = 38
ERROR_INVALID_PARAMETER = 87
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_ENTRY_DELETED = 1181

_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_SHARE_ALL = 0x7
_FILE_READ_ATTRIBUTES = 0x80
_VOLUME_NAME_DOS = 0x0
_FILE_NAME_NORMALIZED = 0x0
_FILE_ID_TYPE = 0
_EXTENDED_FILE_ID_TYPE = 2

_V2_HEAD = struct.Struct("<QQqqIIIIHH")
_V3_HEAD = struct.Struct("<16s16sqqIIIIHH")
_RECORD_PREFIX = struct.Struct("<IHH")
# READ_USN_JOURNAL_DATA_V1：结构体按 8 字节对齐，尾部补 4 字节，长度不对会报 87。
_READ_DATA = struct.Struct("<qIIQQQHH4x")
_JOURNAL_DATA = struct.Struct("<QqqqqQQ")

# 记录字段下标。大批量解析不建对象：实测每条记录建 dataclass 要多花约 1 微秒，
# 繁忙的盘一分钟有几万条，这部分开销比读取本身还大。
FID, PARENT, USN, TIMESTAMP, REASON, ATTRS = range(6)
Record = tuple[int, int, int, int, int, int]


class UsnUnavailable(OSError):
    """这个卷读不了流水账（非 NTFS、日志没开、系统太老……），调用方要退回目录监听。"""


class UsnContinuityLost(OSError):
    """读取位置已被覆盖或日志被重建，中间的变化已不可知；调用方要补一次校准。"""


@dataclass(frozen=True)
class JournalInfo:
    journal_id: int
    first_usn: int
    next_usn: int
    max_size: int


def parse_records(buffer: bytes, length: int) -> list[Record]:
    """把一次读取的结果解成记录。前 8 字节是下一次读取的 USN，调用方自己取。"""
    records: list[Record] = []
    append = records.append
    pos = 8
    prefix = _RECORD_PREFIX.unpack_from
    v2 = _V2_HEAD.unpack_from
    v3 = _V3_HEAD.unpack_from
    while pos + 8 <= length:
        size, major, _minor = prefix(buffer, pos)
        if size < 8 or pos + size > length:
            break
        if major == 2:
            fid, parent, usn, ts, reason, _src, _sec, attrs, _nlen, _noff = v2(buffer, pos + 8)
            append((fid, parent, usn, ts, reason, attrs))
        elif major == 3:
            fid_raw, parent_raw, usn, ts, reason, _src, _sec, attrs, _nlen, _noff = v3(buffer, pos + 8)
            append((int.from_bytes(fid_raw, "little"), int.from_bytes(parent_raw, "little"),
                    usn, ts, reason, attrs))
        pos += size
    return records


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    k.DeviceIoControl.restype = wintypes.BOOL
    k.OpenFileById.restype = wintypes.HANDLE
    k.OpenFileById.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD,
    ]
    k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    return k


class _FileIdDescriptor(ctypes.Structure):
    # FILE_ID_DESCRIPTOR：dwSize + Type + 联合体（8 字节 FileId / 16 字节扩展 ID）。
    # 联合体按 16 字节占位，dwSize 仍写结构体全长，这是 OpenFileById 接受的布局。
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Type", ctypes.c_int),
        ("Id", ctypes.c_ubyte * 16),
    ]


def strip_final_path(path: str) -> str:
    r"""GetFinalPathNameByHandle 给的是 \\?\C:\x；索引和显示都用普通形式。"""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def volume_root(path: str) -> str:
    """路径所在的卷根（C:\\）；UNC 路径返回空串（网络盘没有可读的流水账）。"""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) == 2 and drive[1] == ":":
        return drive.upper() + "\\"
    return ""


class VolumeJournal:
    """一个卷的流水账读取器。线程不安全，由单个后台线程独占使用。"""

    def __init__(self, root: str, *, buffer_size: int = 1 << 18) -> None:
        if sys.platform != "win32":
            raise UsnUnavailable("USN journal is Windows only")
        self.root = volume_root(root)
        if not self.root:
            raise UsnUnavailable(f"no local volume for {root}")
        self._k = _kernel32()
        self._invalid = wintypes.HANDLE(-1).value
        self._buffer = ctypes.create_string_buffer(buffer_size)
        self._input = ctypes.create_string_buffer(_READ_DATA.size)
        self._query_output = ctypes.create_string_buffer(80)
        self._path_buffer = ctypes.create_unicode_buffer(1024)
        self._returned = wintypes.DWORD()
        self._max_major = 2
        self._extended_ids = False
        self._handle = self._k.CreateFileW(
            self.root, 0, _SHARE_ALL, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if self._handle == self._invalid:
            raise UsnUnavailable(
                f"cannot open {self.root}: error {ctypes.get_last_error()}")
        try:
            self._probe_read(self.query())
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None and handle != self._invalid:
            self._k.CloseHandle(handle)
        self._handle = self._invalid

    def __enter__(self) -> "VolumeJournal":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def query(self) -> JournalInfo:
        ok = self._k.DeviceIoControl(
            self._handle, FSCTL_QUERY_USN_JOURNAL, None, 0, self._query_output, 80,
            ctypes.byref(self._returned), None,
        )
        if not ok:
            raise UsnUnavailable(
                f"no journal on {self.root}: error {ctypes.get_last_error()}")
        jid, first, next_usn, _low, _maxusn, max_size, _alloc = _JOURNAL_DATA.unpack_from(
            self._query_output, 0)
        return JournalInfo(jid, first, next_usn, max_size)

    def _probe_read(self, info: JournalInfo) -> None:
        """确认非特权读取真的可用；ReFS 只给 128 位 ID，要用 v3 记录。"""
        error = 0
        for max_major, extended in ((2, False), (3, True)):
            self._max_major = max_major
            self._extended_ids = extended
            error = self._read_raw(info.next_usn, info.journal_id)
            if error in (0, ERROR_HANDLE_EOF):
                return
            if error not in (ERROR_INVALID_PARAMETER, ERROR_INVALID_FUNCTION):
                break
        raise UsnUnavailable(f"journal read denied on {self.root}: error {error}")

    def _read_raw(self, start_usn: int, journal_id: int) -> int:
        _READ_DATA.pack_into(
            self._input, 0, start_usn, REASON_MASK, 0, 0, 0, journal_id,
            2, self._max_major,
        )
        ok = self._k.DeviceIoControl(
            self._handle, FSCTL_READ_UNPRIVILEGED_USN_JOURNAL,
            self._input, _READ_DATA.size,
            self._buffer, len(self._buffer),
            ctypes.byref(self._returned), None,
        )
        return 0 if ok else (ctypes.get_last_error() or -1)

    def read(self, start_usn: int, journal_id: int) -> tuple[int, list[Record]]:
        """从 start_usn 读一块，返回 (下一次读取位置, 记录)。没有新记录时位置不变。"""
        error = self._read_raw(start_usn, journal_id)
        if error == ERROR_HANDLE_EOF:
            return start_usn, []
        # 87：起始位置不是一条记录的开头。存下来的读取位置不可能走到这里，除非日志被
        # 重建过（编号体系变了），和「位置已被覆盖」同样按断档处理。
        if error in (ERROR_JOURNAL_ENTRY_DELETED, ERROR_JOURNAL_DELETE_IN_PROGRESS,
                     ERROR_JOURNAL_NOT_ACTIVE, ERROR_INVALID_PARAMETER):
            raise UsnContinuityLost(f"journal continuity lost on {self.root}: error {error}")
        if error:
            raise UsnUnavailable(f"journal read failed on {self.root}: error {error}")
        length = int(self._returned.value)
        if length < 8:
            return start_usn, []
        # .raw 会把整块缓冲复制一份；只切用到的部分，空闲时一个字节都不多拷
        data = ctypes.string_at(self._buffer, length)
        next_usn = struct.unpack_from("<q", data, 0)[0]
        return next_usn, parse_records(data, length)

    def path_for(self, file_id: int) -> str | None:
        """文件编号 → 当前路径。文件已被删除、无权访问时返回 None，这是常态不是错误。"""
        desc = _FileIdDescriptor()
        desc.dwSize = ctypes.sizeof(_FileIdDescriptor)
        if self._extended_ids:
            desc.Type = _EXTENDED_FILE_ID_TYPE
            ctypes.memmove(desc.Id, file_id.to_bytes(16, "little"), 16)
        else:
            desc.Type = _FILE_ID_TYPE
            ctypes.memmove(desc.Id, file_id.to_bytes(8, "little"), 8)
        handle = self._k.OpenFileById(
            self._handle, ctypes.byref(desc), _FILE_READ_ATTRIBUTES, _SHARE_ALL, None,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        if handle == self._invalid:
            return None
        try:
            buf = self._path_buffer
            size = self._k.GetFinalPathNameByHandleW(
                handle, buf, len(buf), _VOLUME_NAME_DOS | _FILE_NAME_NORMALIZED)
            if size == 0:
                return None
            if size >= len(buf):
                # 超长路径：放大一次重取，放大后的缓冲留着复用
                self._path_buffer = buf = ctypes.create_unicode_buffer(size + 1)
                size = self._k.GetFinalPathNameByHandleW(
                    handle, buf, len(buf), _VOLUME_NAME_DOS | _FILE_NAME_NORMALIZED)
                if size == 0 or size >= len(buf):
                    return None
            return strip_final_path(buf.value)
        finally:
            self._k.CloseHandle(handle)
