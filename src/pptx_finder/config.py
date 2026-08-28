"""路径、扫描排除规则、常量。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

APP_NAME = "pptx-finder"
DEFAULT_THEME = "atelier"
DEFAULT_AUTOSTART = True
DEFAULT_VERSION_KEEP_PER_DOC = 100
# 高阶功能默认关闭：基础模式只承担全盘 PPT 搜索与 PPT 统计。
DEFAULT_VERSION_MANAGEMENT_ENABLED = False
DEFAULT_DOCUMENT_SEARCH_ENABLED = False
DEFAULT_SMART_GROUPING_ENABLED = False
# Everything 式文件名搜索：只登记文件名（status=filename_only），不解析内容。
# 默认开、且不再有开关（2026-08-28）。此前默认关的理由是体积，而且真实代价比当年
# 注释里写的「约 500MB」贵得多：2026-08-28 复测，按现行剪枝规则要登记 1,750,775 个
# 文件（内容类型只有 2,155 个，813:1），单行 731 字节 → 索引库 **+1.19 GB**。
# 即便如此也要默认开——藏在设置里的开关，用户找不到、也不知道要先等一轮全盘重扫，
# 于是「装了 Everything 能力却搜不到任意文件」，这正是被反馈的现象。
# 现在的口径：能力常在，选择权交给搜索框右侧的范围选择器（PPT / Word / PDF /
# 全部文件）。想要轻量的人选 PPT，什么都不会变慢；想找任意文件的人一键就有。
# 磁盘换来的是「用户不必知道有这么个开关」，这笔账划算。
DEFAULT_INDEX_ALL_FILES = True


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
# ui.json 不是"界面偏好"那么轻：version_vault_dir / index_roots /
# version_management_enabled 都住在这里。丢一次的后果是「版本库指向漂回默认位置、
# 程序开一个全新空库继续留底」——用户在别处的历史还在，却再也接不上。
# 因此这里必须同时挡住两件事：
#   1) 写到一半崩溃/断电 → 旧实现是 write_text（先截断再写），残文件解析失败后
#      load 静默返回 {}，全部设置一次性清零。改成 tmp + fsync + os.replace 原子替换，
#      并留一份 .bak：主文件读不出来时回退，且**出声**（原来连日志都没有）。
#   2) 并发读-改-写丢更新 → 全仓 20+ 处调用分布在 GUI 线程、FeatureRuntime 线程和
#      BackgroundTask 上（如 app.py 关版本管理、settings_dialog 改开关）。实测双线程
#      各写各的键，30 轮里 8 轮有一个键被整份覆盖掉。进程内用一把锁串起临界区。
# 读路径不加锁：os.replace 是原子的，读者只会看到旧的或新的完整内容，不会看到半截。
_UI_LOCK = threading.RLock()


def _ui_settings_path() -> Path:
    return data_dir() / "ui.json"


def _ui_settings_backup_path() -> Path:
    return data_dir() / "ui.json.bak"


def _read_ui_settings_file(path: Path) -> dict | None:
    """读一个候选配置文件；不存在/损坏/不是 dict 都返回 None。"""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 配置损坏不能拖垮启动
        return None
    return data if isinstance(data, dict) else None


def load_ui_settings() -> dict:
    """读 ui.json；主文件损坏时回退 .bak；都读不出来才返回 {}。"""
    data = _read_ui_settings_file(_ui_settings_path())
    if data is not None:
        return data
    backup = _read_ui_settings_file(_ui_settings_backup_path())
    if backup is not None:
        # 静默回退等于把「设置全没了」伪装成「你没设过」。至少留一条日志和一次自愈写回。
        _log.warning("ui.json 不可读，已回退上一份可用备份 ui.json.bak")
        try:
            _atomic_write_json(_ui_settings_path(), backup)
        except OSError:
            pass
        return backup
    return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """tmp + fsync + os.replace：要么是完整的旧内容，要么是完整的新内容。"""
    payload = json.dumps(data, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_ui_settings(**changes) -> None:
    """合并写 ui.json：保留未涉及的键（改主题不清掉热键，反之亦然）。"""
    with _UI_LOCK:
        data = load_ui_settings()
        data.update(changes)
        try:
            _atomic_write_json(_ui_settings_path(), data)
        except (OSError, TypeError, ValueError):  # noqa: BLE001 落盘失败不能拖垮界面
            _log.warning("ui.json 写入失败，本次设置未持久化", exc_info=True)
            return
        try:
            _atomic_write_json(_ui_settings_backup_path(), data)
        except (OSError, TypeError, ValueError):  # noqa: BLE001 备份是兜底，失败不影响主文件
            _log.debug("ui.json.bak 备份写入失败", exc_info=True)


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


DEFAULT_VERSION_VAULT_DIR = ""  # 空 = 默认 data_dir()/vault


def get_version_vault_dir(default: str = DEFAULT_VERSION_VAULT_DIR) -> str:
    """用户自选的版本库存储目录；空串表示默认位置。"""
    v = load_ui_settings().get("version_vault_dir")
    return str(v).strip() if isinstance(v, str) and v.strip() else default


def set_version_vault_dir(path: str) -> None:
    update_ui_settings(version_vault_dir=str(path or "").strip())


def validate_version_vault_dir(path: str) -> str | None:
    """校验版本库候选目录，返回错误文案；None 表示可用。"""
    if not path or not path.strip():
        return "目录不能为空"
    p = Path(path.strip())
    low = str(p).lower().replace("/", "\\")
    for banned in ("\\windows", "\\program files", "\\programdata"):
        if low.startswith(banned) or (len(low) > 3 and banned in low):
            return f"不允许放在系统目录（{banned.strip(chr(92))}）下"
    if low in ("c:\\", "d:\\", "e:\\"):
        return "不允许直接放在盘符根目录，请选择其子文件夹"
    candidate = Path(os.path.abspath(str(p)))
    current_raw = get_version_vault_dir()
    current = Path(
        os.path.abspath(current_raw or str(data_dir() / "vault"))
    )
    candidate_key = os.path.normcase(str(candidate))
    current_key = os.path.normcase(str(current))
    if candidate_key != current_key:
        try:
            common = os.path.commonpath([candidate_key, current_key])
        except ValueError:
            common = ""  # different drives cannot be nested
        if common in {candidate_key, current_key}:
            return "新旧版本库不能互相嵌套，请选择独立目录"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".pptdoctor-write-probe"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    except OSError as e:
        return f"目录不可写：{e}"
    return None


def _clean_index_roots(roots) -> tuple[str, ...]:
    """清洗索引根列表：去空白、去尾分隔符（盘符根除外）、按 normcase 去重，保持顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in roots or ():
        p = str(raw or "").strip()
        if not p:
            continue
        stripped = p.rstrip("/\\")
        if stripped:  # 尾部分隔符统一剥掉，避免同一目录两种写法重复登记
            p = stripped
        if len(p) == 2 and p[1] == ":":  # "C:\" 被剥成 "C:" 会变成盘当前目录，还原
            p += "\\"
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return tuple(out)


def get_index_roots() -> tuple[str, ...]:
    """用户在设置里维护的自定义索引根（本地目录 / 网络 UNC）；空 = 未配置。"""
    v = load_ui_settings().get("index_roots")
    return _clean_index_roots(v) if isinstance(v, list) else ()


def set_index_roots(roots) -> None:
    update_ui_settings(index_roots=list(_clean_index_roots(roots)))


def validate_index_root(path: str) -> tuple[bool, bool, str]:
    """校验自定义索引根，返回 (可保存, 当前可达, 提示文案) 三态。

    格式非法 → (False, False, 原因)；合法且可达 → (True, True, "")；
    合法但暂不可达 → (True, False, 警告)——网络盘时通时断，拒存是错误的，
    保存后由索引器在其可达时自动纳入。
    注意：可达性探测（isdir 触网络盘）可能耗时数秒，UI 线程请放后台调用。
    """
    p = str(path or "").strip()
    if not p:
        return False, False, "路径不能为空"
    normalized = p.replace("/", "\\")
    is_drive = (
        len(normalized) >= 3
        and normalized[0].isalpha()
        and normalized[1] == ":"
        and normalized[2] == "\\"
    )
    is_unc = False
    if normalized.startswith("\\\\") and not normalized.startswith("\\\\?\\"):
        # 至少 \\server\share 两段；\\?\ 设备路径不接收
        is_unc = len([s for s in normalized[2:].split("\\") if s]) >= 2
    if not (is_drive or is_unc):
        return False, False, "只支持本地目录（如 D:\\资料）或网络路径（如 \\\\server\\share\\dir）"
    try:
        reachable = os.path.isdir(ext_path(p))
    except Exception:  # noqa: BLE001 含 \x00 等异常字符抛 ValueError，按不可达处理而非卡死对话框
        reachable = False
    if reachable:
        return True, True, ""
    return True, False, "当前不可达：保存后将在其可用时自动纳入索引"


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
    index_all_files: bool | None = None,
) -> str:
    docs_on = (
        get_document_search_enabled()
        if document_search_enabled is None else bool(document_search_enabled)
    )
    groups_on = (
        get_smart_grouping_enabled()
        if smart_grouping_enabled is None else bool(smart_grouping_enabled)
    )
    any_on = (
        get_index_all_files()
        if index_all_files is None else bool(index_all_files)
    )
    return (
        f"documents={int(docs_on)};smart_grouping={int(groups_on)}"
        f";any_file={int(any_on)}"
    )


def get_index_all_files() -> bool:
    """Everything 式全盘文件名盘点：常开，没有开关。

    刻意不读 ui.json 里的旧键。开关已经撤掉了，再去认那个值只会留下一个死结——
    从前手滑关过一次的用户升级后永远开不回来，而界面上已经没有地方能让他开回来。
    需要走关闭路径的地方（purge / 对账，以及测试）照旧用 index_all_files=False
    显式传参，那条代码路径本身没有作废。
    """
    return DEFAULT_INDEX_ALL_FILES


def feature_signature_needs_rescan(completed: str, current: str) -> bool:
    """特征签名变化是否必须重扫全盘。

    唯一的例外是 any_file 由 1 → 0（关闭「索引所有文件」）：内容索引的口径一个字
    都没变，只需要后台把盘点行清掉，没有任何理由让用户再等一次全盘扫描。
    从默认开改成默认关时，这条豁免让老用户升级即生效、不付重扫代价。
    其余任何差异（documents / smart_grouping 变化、any_file 开启）都会改变入库
    内容，必须重扫。
    """
    if completed == current:
        return False

    def _parse(sig: str) -> dict[str, str]:
        return dict(
            part.split("=", 1) for part in str(sig or "").split(";") if "=" in part
        )

    before, after = _parse(completed), _parse(current)
    if before.get("any_file") == "1" and after.get("any_file") == "0":
        before.pop("any_file", None)
        after.pop("any_file", None)
        return before != after
    return True


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


DEFAULT_VAULT_MAX_MB = 5120  # 版本库总容量上限（MB）；0 = 不限。超出按从老到新驱逐健康版本


def get_vault_max_mb(default: int = DEFAULT_VAULT_MAX_MB) -> int:
    value = load_ui_settings().get("vault_max_mb")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return max(0, int(default))


def set_vault_max_mb(limit: int) -> None:
    update_ui_settings(vault_max_mb=max(0, int(limit)))


def get_hotkey() -> str:
    """当前全局唤起热键：用户覆盖值优先，否则默认 GLOBAL_HOTKEY。"""
    v = load_ui_settings().get("hotkey")
    return v if isinstance(v, str) and v.strip() else GLOBAL_HOTKEY


def set_hotkey(spec: str) -> None:
    update_ui_settings(hotkey=spec)


# 界面字体：font_family 空串 = 跟随内置字族。
# font_scale 三个预设档位保留（一键即得），另外允许 0.50~2.00 之间任意值自定义。
# 上下限不是拍脑袋：0.5 以下 13px 基准字号会掉到 6px、2.0 以上侧栏与卡片开始互相挤，
# 两端都已经不是「能用」的界面了，所以在入口处夹死而不是让用户自己踩坑。
FONT_SCALES = (0.9, 1.0, 1.15)
FONT_SCALE_MIN = 0.5
FONT_SCALE_MAX = 2.0
FONT_SCALE_STEP = 0.05

# 字族名会被拼进 QSS（ui/theme.py 的 * font-family 规则）：引号/反斜杠/控制字符/
# 花括号/分号可突围注入任意样式，甚至让 Qt 拒收整表——入库前一律剥掉并限长。
# theme._font_family_qss 侧用同一函数再洗一道（双保险）。
_FONT_FAMILY_BAD_RE = re.compile(r'["\\\r\n\t{};]')
FONT_FAMILY_MAX_LEN = 100


def sanitize_font_family(family: str) -> str:
    """清洗界面字族名：剥掉可注入 QSS 的字符、限长；返回空串 = 回退内置字族。"""
    name = _FONT_FAMILY_BAD_RE.sub("", str(family or "").strip()).strip()
    return name[:FONT_FAMILY_MAX_LEN]


def get_font_family(default: str = "") -> str:
    """界面字族覆盖；空串 = 内置字族（跟随 QSS 模板默认）。"""
    v = load_ui_settings().get("font_family")
    return v.strip() if isinstance(v, str) and v.strip() else default


def set_font_family(family: str) -> None:
    update_ui_settings(font_family=sanitize_font_family(family))


def clamp_font_scale(scale: float) -> float:
    """把任意输入夹进 [FONT_SCALE_MIN, FONT_SCALE_MAX] 并吸附到 0.01 网格。

    NaN / inf 也要挡下来：它们会一路流进 QSS 的 font-size 计算，
    生成 `font-size: nanpx` 这种 Qt 拒收的整表，而配置已经落盘 → 每次启动都裸奔。
    """
    try:
        v = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(v):
        return 1.0
    return round(min(FONT_SCALE_MAX, max(FONT_SCALE_MIN, v)), 2)


def get_font_scale(default: float = 1.0) -> float:
    """界面字号倍率：合法值原样返回（含自定义），非法值回默认。"""
    v = load_ui_settings().get("font_scale")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return clamp_font_scale(v)
    return clamp_font_scale(default)


def set_font_scale(scale: float) -> None:
    update_ui_settings(font_scale=clamp_font_scale(scale))

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
