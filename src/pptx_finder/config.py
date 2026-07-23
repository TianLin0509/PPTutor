"""路径、扫描排除规则、常量。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "pptx-finder"
DEFAULT_THEME = "atelier"
DEFAULT_AUTOSTART = True
DEFAULT_VERSION_KEEP_PER_DOC = 100
# 高阶功能默认关闭：基础模式只承担全盘 PPT 搜索与 PPT 统计。
DEFAULT_VERSION_MANAGEMENT_ENABLED = False
DEFAULT_DOCUMENT_SEARCH_ENABLED = False
DEFAULT_SMART_GROUPING_ENABLED = False


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
    "$recycle.bin", "system volume information",
    "local settings",  # AppData 正式纳入覆盖；系统 Temp 由 scanner 按完整路径排除
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    # AI/开发自动化产物：PPT Doctor 自身压测与 agent artifacts 不应进入索引/版本库。
    ".selftest", ".arena", ".ai-team",
    "$winreagent", "recovery", "msocache", "intel", "perflogs",
}

# 支持的扩展名
PPTX_EXT = ".pptx"
PPT_EXT = ".ppt"
DOCX_EXT = ".docx"
XLSX_EXT = ".xlsx"
TXT_EXT = ".txt"
PDF_EXT = ".pdf"
# 能解析「内容」的类型（pptx 优先，其余后台补建）。.ppt 旧二进制仅文件名登记、不在此列。
# PPT Doctor 只面向 PowerPoint / Word / PDF（2026-06-29 砍掉 xlsx/txt：少扫少解析、更快更稳）。
# XLSX_EXT/TXT_EXT 常量保留（document_parser 仍有解析器、夹具/测试引用），但不进扫描/索引集合。
CONTENT_EXTS = (PPTX_EXT, DOCX_EXT, PDF_EXT)
# 扫描枚举的全部类型 = 可解析内容的 + 仅文件名的 .ppt
SUPPORTED_EXTS = CONTENT_EXTS + (PPT_EXT,)
# 「PPT 分析」口径：胶片报告 / 仪表盘 / 库健康只统计 PowerPoint（pptx+ppt），
# 不混入多文档搜索引入的 docx/xlsx/txt/pdf。底部状态栏索引进度仍按全类型。
PPT_EXTS = (PPTX_EXT, PPT_EXT)

# 超过此大小跳过解析（仍可文件名命中）
# 超过此大小的文件只登记文件名、不解析内容（防巨文件拖慢/卡死建库；仍可按文件名搜）。
# 2026-06-29 从 200MB 收紧到 60MB——文本搜索没必要硬啃上百 MB 的富媒体大稿。
MAX_PARSE_SIZE = 60 * 1024 * 1024   # 60MB（通用）
MAX_PDF_PARSE_SIZE = 30 * 1024 * 1024  # 30MB（PDF 更严：pypdf 对大/坏 PDF 易慢易卡）

# 全局唤起热键（默认值；用户可在设置里改，覆盖值存 ui.json 的 "hotkey" 键）
GLOBAL_HOTKEY = "Alt+F"


# ---------- UI 偏好（ui.json：主题 / 热键 等，读-改-写保留其它键） ----------
def _ui_settings_path() -> Path:
    return data_dir() / "ui.json"


def load_ui_settings() -> dict:
    """读 ui.json，损坏/缺失返回 {}。"""
    try:
        p = _ui_settings_path()
        if p.exists():
            data = json.loads(p.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001 配置损坏不能拖垮启动
        pass
    return {}


def update_ui_settings(**changes) -> None:
    """合并写 ui.json：保留未涉及的键（改主题不清掉热键，反之亦然）。"""
    data = load_ui_settings()
    data.update(changes)
    try:
        _ui_settings_path().write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def get_theme(default: str = DEFAULT_THEME) -> str:
    v = load_ui_settings().get("theme")
    return v if isinstance(v, str) and v else default


def set_theme(name: str) -> None:
    update_ui_settings(theme=name)


def get_autostart(default: bool = DEFAULT_AUTOSTART) -> bool:
    v = load_ui_settings().get("autostart")
    return v if isinstance(v, bool) else default


def set_autostart(enabled: bool) -> None:
    update_ui_settings(autostart=bool(enabled))


def _get_bool_setting(key: str, default: bool) -> bool:
    value = load_ui_settings().get(key)
    return value if isinstance(value, bool) else bool(default)


def get_version_management_enabled(
    default: bool = DEFAULT_VERSION_MANAGEMENT_ENABLED,
) -> bool:
    return _get_bool_setting("version_management_enabled", default)


def set_version_management_enabled(enabled: bool) -> None:
    update_ui_settings(version_management_enabled=bool(enabled))


def get_document_search_enabled(
    default: bool = DEFAULT_DOCUMENT_SEARCH_ENABLED,
) -> bool:
    return _get_bool_setting("document_search_enabled", default)


def set_document_search_enabled(enabled: bool) -> None:
    update_ui_settings(document_search_enabled=bool(enabled))


def get_smart_grouping_enabled(
    default: bool = DEFAULT_SMART_GROUPING_ENABLED,
) -> bool:
    return _get_bool_setting("smart_grouping_enabled", default)


def set_smart_grouping_enabled(enabled: bool) -> None:
    update_ui_settings(smart_grouping_enabled=bool(enabled))


def enabled_index_exts(document_search_enabled: bool | None = None) -> tuple[str, ...]:
    """当前产品层允许进入索引/搜索的扩展名。

    PPT 始终开启；Word/PDF 是主动选择的高阶能力。调用方可传入内存态，避免
    watcher 热路径反复读取 ui.json。
    """
    docs_on = (
        get_document_search_enabled()
        if document_search_enabled is None
        else bool(document_search_enabled)
    )
    return PPT_EXTS + ((DOCX_EXT, PDF_EXT) if docs_on else ())


def index_feature_signature(
    document_search_enabled: bool | None = None,
    smart_grouping_enabled: bool | None = None,
) -> str:
    docs_on = (
        get_document_search_enabled()
        if document_search_enabled is None else bool(document_search_enabled)
    )
    groups_on = (
        get_smart_grouping_enabled()
        if smart_grouping_enabled is None else bool(smart_grouping_enabled)
    )
    return f"documents={int(docs_on)};smart_grouping={int(groups_on)}"


def get_completed_index_feature_signature(default: str = "") -> str:
    value = load_ui_settings().get("completed_index_feature_signature")
    return value if isinstance(value, str) else default


def set_completed_index_feature_signature(signature: str) -> None:
    update_ui_settings(completed_index_feature_signature=str(signature or ""))


def ensure_completed_index_feature_signature(signature: str) -> str:
    """Upgrade baseline: old releases already have a usable PPT index.

    Persist the current basic scope once so a later opt-in can be distinguished
    from an upgrade that merely lacks the new bookkeeping key.
    """
    current = get_completed_index_feature_signature()
    if current:
        return current
    set_completed_index_feature_signature(signature)
    return str(signature)


def get_version_keep_per_doc(default: int = DEFAULT_VERSION_KEEP_PER_DOC) -> int:
    value = load_ui_settings().get("version_keep_per_doc")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return max(0, int(default))


def set_version_keep_per_doc(limit: int) -> None:
    update_ui_settings(version_keep_per_doc=max(0, int(limit)))


def get_hotkey() -> str:
    """当前全局唤起热键：用户覆盖值优先，否则默认 GLOBAL_HOTKEY。"""
    v = load_ui_settings().get("hotkey")
    return v if isinstance(v, str) and v.strip() else GLOBAL_HOTKEY


def set_hotkey(spec: str) -> None:
    update_ui_settings(hotkey=spec)

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
