"""可选组件（文字识别 / 公式识别）的统一入口：状态、版本、占用、下载安装、卸载。

v2.0.1 起基础版是默认安装包：识别组件不预装，用户在「设置 → 组件」或第一次用到
功能时按需下载。下载走与软件更新同一套多源逻辑（GitHub 在前、备用服务器兜底、
认出公司代理的拦截网页、逐层 sha256 校验），这里只把两种组件包成一样的接口，
界面（组件下载窗口、设置页）不必关心它们各自的目录布局。
"""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ComponentInfo:
    key: str
    name: str
    purpose: str
    #: 清单取不到时显示的大概下载量（MB，按 1024² 计，与进度条口径一致）
    approx_mb: int


OCR = ComponentInfo("ocr", "文字识别组件", "图片转可编辑文字、截图识字", 82)
FORMULA = ComponentInfo("formula", "公式识别组件", "截图识别公式（LaTeX / Word 公式）", 436)
ALL = (OCR, FORMULA)
BY_KEY = {c.key: c for c in ALL}


def info(key: str) -> ComponentInfo:
    try:
        return BY_KEY[key]
    except KeyError:
        raise ValueError(f"未知组件：{key}") from None


def _ocr():
    from . import imgtext_ocr
    return imgtext_ocr


def _formula():
    from .screenshots import formula_component
    return formula_component


def is_installed(key: str) -> bool:
    info(key)
    try:
        return bool(_ocr().is_installed() if key == "ocr" else _formula().is_installed())
    except Exception:  # noqa: BLE001 探测失败按未安装处理，界面给下载入口
        return False


def bundled(key: str) -> bool:
    """组件是否随安装包内置（文字识别版 / 完整版）。内置的不需要联网下载。"""
    info(key)
    try:
        return (_ocr().bundled_component_dir() if key == "ocr" else _formula().bundled_dir()) is not None
    except Exception:  # noqa: BLE001
        return False


def installed_version(key: str) -> str:
    info(key)
    try:
        if key == "ocr":
            return _ocr().installed_version()
        return _formula().installed_version()
    except Exception:  # noqa: BLE001
        return ""


def _dir_size(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def installed_dir(key: str) -> Path:
    """本机解压后的组件目录（卸载删的就是它）。"""
    info(key)
    return _ocr().component_dir() if key == "ocr" else _formula().component_dir()


def disk_bytes(key: str) -> int:
    """组件在本机数据目录里占的空间（不含安装包内置的那份压缩包）。"""
    info(key)
    if key == "ocr":
        ocr = _ocr()
        return _dir_size(ocr.component_dir()) + _dir_size(ocr.component_dir().with_name("ocr-bundled"))
    return _dir_size(_formula().component_dir())


def fetch_manifest(key: str) -> dict:
    """各源都问一遍清单，返回带 `_bases`（可用源）与 `_checks`（各源结果）的清单。"""
    info(key)
    return _ocr().fetch_component_manifest(timeout=15) if key == "ocr" else _formula().fetch_manifest()


def source_names(key: str) -> list[str]:
    from .netfetch import source_name
    info(key)
    sources = _ocr().component_sources() if key == "ocr" else _formula().sources()
    return [source_name(label) for label, _base in sources]


def install(key: str, manifest: dict | None, *, status=None, cancel=None) -> str:
    """下载（或从安装包内置副本解压）并原子安装，返回版本号。

    status(dict)：与软件更新相同的进度快照（source / received / total / speed / eta），
    或 {"attempts": [...]}（各源结果）。cancel：threading.Event，置位即取消，抛 InterruptedError。
    """
    info(key)
    if key == "ocr":
        # 识别会话常驻一个子进程（空闲 45 秒才退出）；它占着旧组件目录时 Windows 不让换目录
        from .ocr_session import session
        session.close()
        if manifest is None and bundled("ocr"):
            ocr = _ocr()
            local = ocr.bundled_component_dir()
            assert local is not None
            import json
            payload = json.loads((local / ocr.VERSION_FILE).read_text("utf-8"))
            return ocr.install(payload, archive_path=local / "component.zip",
                               target_dir=ocr.active_component_dir(),
                               progress=_local_progress(status),
                               cancel=(cancel.is_set if cancel is not None else None))
        return _ocr().install(manifest, status=status,
                              cancel=(cancel.is_set if cancel is not None else None))
    from .screenshots.encoding import CaptureCancelled
    try:
        local = manifest is None and bundled("formula")
        return _formula().install(manifest=manifest, cancelled=cancel, status=status,
                                  progress=_local_progress(status) if local else None)
    except CaptureCancelled as exc:
        raise InterruptedError("下载已取消") from exc


def _local_progress(status):
    """内置副本是本地拷贝：没有源与速度，只报已处理/总量。"""
    if status is None:
        return None

    def progress(done, total):
        status({"source": "安装包内置", "received": done, "total": total})
    return progress


class ComponentBusy(RuntimeError):
    """组件文件正被识别进程占用，现在删不掉。"""


def uninstall(key: str) -> int:
    """删除本机解压出来的组件，返回释放的字节数。

    先把整个目录改名到旁边再删：Windows 上目录里有程序在跑时改名直接失败，
    这样要么整个挪走、要么原样不动，不会删一半留下坏组件。
    """
    info(key)
    if key == "ocr":
        from .ocr_session import session
        session.close()
        roots = [_ocr().component_dir(), _ocr().component_dir().with_name("ocr-bundled")]
    else:
        roots = [_formula().component_dir()]
    freed = 0
    for root in roots:
        if not root.exists():
            continue
        size = _dir_size(root)
        trash = root.with_name(f".{root.name}-removing-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(root, trash)
        except OSError as exc:
            raise ComponentBusy("组件正在使用（识别还没结束），稍后再卸载") from exc
        shutil.rmtree(trash, ignore_errors=True)
        freed += size
    return freed
