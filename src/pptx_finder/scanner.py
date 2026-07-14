"""磁盘扫描：枚举 .pptx/.ppt，剪枝排除目录。"""
from __future__ import annotations

import ctypes
import os
import string
from collections.abc import Iterator
from pathlib import Path

from .config import EXCLUDE_DIR_NAMES, SUPPORTED_EXTS, data_dir

DRIVE_FIXED = 3
SCAN_POLICY_VERSION = "2"  # v2: AppData is covered; PPT Doctor's own store stays excluded


def _norm_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    path_norm = _norm_path(path)
    root_norm = _norm_path(root)
    try:
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except ValueError:
        return False


def fixed_drives() -> list[str]:
    """返回所有本地固定磁盘根（如 ['C:\\\\', 'D:\\\\']）。"""
    drives: list[str] = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if not (bitmask & (1 << i)):
                continue
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
                drives.append(root)
    except Exception:  # noqa: BLE001 非 Windows 或调用失败时回退
        pass
    return drives or [str(Path.home())]


def iter_ppt_files(
    roots: list[str],
    excludes: set[str] | None = None,
    exclude_roots: list[str] | None = None,
) -> Iterator[Path]:
    """遍历 roots，产出受支持的演示文稿路径，剪枝排除目录与临时锁文件。"""
    ex = {e.lower() for e in (excludes if excludes is not None else EXCLUDE_DIR_NAMES)}
    hard_excluded_roots = [
        _norm_path(p)
        for p in (exclude_roots if exclude_roots is not None else [str(data_dir())])
    ]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            if any(_under(dirpath, skip_root) for skip_root in hard_excluded_roots):
                dirnames[:] = []
                continue
            # 原地剪枝：跳过排除目录与 $ 开头的系统目录
            dirnames[:] = [
                d for d in dirnames
                if (
                    d.lower() not in ex
                    and not d.startswith("$")
                    and not any(
                        _under(os.path.join(dirpath, d), skip_root)
                        for skip_root in hard_excluded_roots
                    )
                )
            ]
            for fn in filenames:
                if fn.startswith("~$"):  # Office 临时锁文件
                    continue
                if os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS:
                    yield Path(dirpath) / fn
