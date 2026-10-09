"""Read-only physical accounting; only short-lived _tmp is outside the limit."""
import os
from pathlib import Path

from . import vault


def db_bytes(root: Path) -> int:
    total = 0
    for name in ('versions.db', 'versions.db-wal', 'versions.db-shm'):
        try:
            total += (root/name).stat().st_size
        except FileNotFoundError:
            pass
    return total


def usage() -> dict:
    root = vault._vault_dir_no_create()
    total = temporary = 0
    stack = [(root, False)]
    try:
        root.stat()
    except FileNotFoundError:
        return {'saved_bytes': 0, 'temporary_bytes': 0, 'total_bytes': 0, 'directory': str(root)}
    while stack:
        directory, is_temporary = stack.pop()
        # Permission and I/O failures must surface, not turn an unknown size into zero.
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), is_temporary or (directory == root and entry.name == '_tmp')))
                elif entry.is_file(follow_symlinks=False):
                    size = os.stat(entry.path).st_size if entry.name.startswith('versions.db') else entry.stat().st_size
                    total += size
                    if is_temporary:
                        temporary += size
    return {'saved_bytes': total-temporary, 'temporary_bytes': temporary, 'total_bytes': total, 'directory': str(root)}
