from __future__ import annotations

import os


def ensure_pptx_suffix(path: str) -> str:
    if not path:
        return path
    _root, ext = os.path.splitext(path)
    return path if ext else f"{path}.pptx"
