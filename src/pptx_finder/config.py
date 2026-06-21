"""路径、扫描排除规则、常量。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "pptx-finder"


def resource_path(*parts: str) -> Path:
    """资源文件路径，兼容 PyInstaller 打包(_MEIPASS)与源码运行。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base).joinpath(*parts)
    # 源码：项目根（config.py 在 src/pptx_finder/ 下，上溯三级）
    return Path(__file__).resolve().parents[2].joinpath(*parts)


def data_dir() -> Path:
    """应用数据目录。可用 PPTX_FINDER_DATA_DIR 覆盖（测试隔离用）。"""
    base = os.environ.get("PPTX_FINDER_DATA_DIR")
    if not base:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home())
        base = os.path.join(local, APP_NAME)
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "index.db"


def cache_dir() -> Path:
    p = data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_first_run() -> bool:
    """首次运行（尚未看过欢迎引导）。"""
    return not (data_dir() / "welcomed.flag").exists()


def mark_welcomed() -> None:
    """记录已看过欢迎引导，之后启动不再弹。"""
    try:
        (data_dir() / "welcomed.flag").write_text("1", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def is_version_intro_done() -> bool:
    """是否已做过「版本管理首次告知」（首次后台留版时弹一次聚光灯，之后永久静默）。"""
    return (data_dir() / "version_intro.flag").exists()


def mark_version_intro_done() -> None:
    try:
        (data_dir() / "version_intro.flag").write_text("1", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# 扫描时排除的目录名（小写，按路径片段匹配）——减少无效 IO 与噪音
EXCLUDE_DIR_NAMES: set[str] = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "appdata",
    "local settings", "temp", "tmp", "locallow",  # 临时目录（pytest 等临时 PPT 不该索引）
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "$winreagent", "recovery", "msocache", "intel", "perflogs",
}

# 支持的扩展名
PPTX_EXT = ".pptx"
PPT_EXT = ".ppt"
SUPPORTED_EXTS = (PPTX_EXT, PPT_EXT)

# 超过此大小跳过解析（仍可文件名命中）
MAX_PARSE_SIZE = 200 * 1024 * 1024  # 200MB

# 全局唤起热键
GLOBAL_HOTKEY = "Ctrl+Alt+P"

# 增量自动更新：清单 + 内容寻址块的根地址。E2E/灰度可用 PPTX_FINDER_UPDATE_URL 覆盖（如指 localhost）
_DEFAULT_UPDATE_URL = "https://me.lt-stockpartner.tech/pptutor"


def update_base_url() -> str:
    return os.environ.get("PPTX_FINDER_UPDATE_URL") or _DEFAULT_UPDATE_URL


def ext_path(path: str) -> str:
    r"""Windows 上对超长路径(>260)加 \\?\ 前缀，避免 [Errno 22] 打不开。"""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if len(p) < 250 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):  # UNC 网络路径
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p
