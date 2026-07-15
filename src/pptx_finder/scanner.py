"""磁盘扫描：枚举 .pptx/.ppt，剪枝排除目录。"""
from __future__ import annotations

import ctypes
import os
import string
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .config import EXCLUDE_DIR_NAMES, SUPPORTED_EXTS, data_dir

DRIVE_FIXED = 3
SCAN_POLICY_VERSION = "3"  # v3: user folders named Temp are covered; only real OS temp stays excluded


def _norm_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    path_norm = _norm_path(path)
    root_norm = _norm_path(root)
    try:
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except ValueError:
        return False


def _is_system_temp_subtree(
    path: str | os.PathLike[str],
    scan_root: str | os.PathLike[str],
) -> bool:
    """Match AppData/Local/Temp only inside this scan root.

    Using the relative path matters for tests and explicit user roots that may
    themselves live under the OS temp directory. A business folder merely named
    ``Temp`` is not a system cache and must remain searchable.
    """
    try:
        rel = os.path.relpath(os.fspath(path), os.fspath(scan_root))
    except (OSError, TypeError, ValueError):
        return False
    parts = [p.casefold() for p in rel.replace("/", "\\").split("\\") if p]
    needle = ("appdata", "local", "temp")
    return any(tuple(parts[i:i + 3]) == needle for i in range(max(0, len(parts) - 2)))


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
    supported_exts: tuple[str, ...] | set[str] | None = None,
    scan_progress_cb: Callable[[int, str], None] | None = None,
    scan_error_cb: Callable[[OSError], None] | None = None,
) -> Iterator[Path]:
    """遍历 roots，产出受支持的演示文稿路径，剪枝排除目录与临时锁文件。"""
    ex = {e.lower() for e in (excludes if excludes is not None else EXCLUDE_DIR_NAMES)}
    allowed_exts = {
        e.lower() for e in (SUPPORTED_EXTS if supported_exts is None else supported_exts)
    }
    hard_excluded_roots = [
        _norm_path(p)
        for p in (exclude_roots if exclude_roots is not None else [str(data_dir())])
    ]
    directories_seen = 0
    last_progress_at = 0.0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(
            root,
            topdown=True,
            onerror=scan_error_cb,
        ):
            directories_seen += 1
            now = time.monotonic()
            if scan_progress_cb is not None and (
                directories_seen == 1 or now - last_progress_at >= 0.5
            ):
                scan_progress_cb(directories_seen, dirpath)
                last_progress_at = now
            if any(_under(dirpath, skip_root) for skip_root in hard_excluded_roots):
                dirnames[:] = []
                continue
            if _is_system_temp_subtree(dirpath, root):
                dirnames[:] = []
                continue
            # 原地剪枝：跳过排除目录与 $ 开头的系统目录
            dirnames[:] = [
                d for d in dirnames
                if (
                    d.lower() not in ex
                    and not d.startswith("$")
                    and not _is_system_temp_subtree(os.path.join(dirpath, d), root)
                    and not any(
                        _under(os.path.join(dirpath, d), skip_root)
                        for skip_root in hard_excluded_roots
                    )
                )
            ]
            for fn in filenames:
                if fn.startswith("~$"):  # Office 临时锁文件
                    continue
                if os.path.splitext(fn)[1].lower() in allowed_exts:
                    yield Path(dirpath) / fn
        if scan_progress_cb is not None:
            scan_progress_cb(directories_seen, root)
