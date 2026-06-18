"""版本管理数据类。store 返回 sqlite Row，这里提供类型化包装供 UI/manager 使用。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ManagedDoc:
    doc_id: str
    path: str
    status: str = "active"  # active | deleted（原文件已删，vault 保留）
    latest_version_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class Version:
    version_id: str
    doc_id: str
    ts: float
    session_id: str = ""
    page_count: int = 0
    size: int = 0
    changed: str = ""
    thumb_path: str = ""
    content_hash: str = ""
