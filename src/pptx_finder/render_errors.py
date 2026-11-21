"""Stable preview failure categories shared by the GUI and renderer process."""
from __future__ import annotations

CATEGORIES = {"missing_file", "powerpoint_unavailable", "busy_timeout", "other"}


def normalize_category(category: str) -> str:
    return category if category in CATEGORIES else "other"


def classify_error(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "missing_file"
    if isinstance(error, TimeoutError):
        return "busy_timeout"
    if isinstance(error, ImportError):
        return "powerpoint_unavailable"
    code = getattr(error, "hresult", 0) & 0xFFFFFFFF
    if code == 0x80020009:
        info = getattr(error, "excepinfo", None)
        if info and len(info) >= 6 and isinstance(info[5], int):
            code = info[5] & 0xFFFFFFFF
    if code in (0x80010001, 0x8001010A, 0x8001011F):
        return "busy_timeout"
    if code in (0x80040154, 0x800401F3, 0x80080005):
        return "powerpoint_unavailable"
    return "other"
