"""「团队素材包」可选组件：不进安装包，下载后自动导入素材库。

发布形态与文字识别 / 公式组件相同：GitHub 独立 Release 标签（materials-vX.Y.Z）上放
component.json + component.zip，自建服务器 /pptutor/materials/ 作备用；清单格式、两层
sha256 校验、多源回落全部复用 imgtext_ocr 那一套。压缩包里只有一个素材包 PPTX
（PPT Doctor Material v1 格式，每页一个素材，备注写 {id, name, category}）。

导入按素材 id 去重（materials.MaterialLibrary.sync_pack）：重复安装不会多出素材；
升级时旧版导入的素材被新版整体替换；用户自己收藏的素材一个都不动。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .config import data_dir

PACK = "team"
PACK_FILE = "team-materials.pptx"
#: GitHub 上首个素材包的独立标签；之后的版本由备用服务器与新标签同时提供，客户端挑版本最高的
RELEASE_BASE = "https://github.com/TianLin0509/PPTutor/releases/download/materials-v1.0.0"


def component_dir() -> Path:
    """下载解压后的素材包原件（留一份，便于不联网重新导入）。"""
    return data_dir() / "materials-pack"


def _library():
    from .materials import MaterialLibrary
    return MaterialLibrary()


def installed_version() -> str:
    try:
        return str(_library().pack_record(PACK).get("version") or "")
    except (OSError, ValueError):
        return ""


def is_installed() -> bool:
    return bool(installed_version())


def sources() -> list[tuple[str, str]]:
    from . import imgtext_ocr
    return imgtext_ocr.component_sources("materials", RELEASE_BASE)


def fetch_manifest(timeout: float = 15) -> dict:
    from . import imgtext_ocr
    return imgtext_ocr.fetch_manifest_from_sources(sources(), timeout, "团队素材包")


def install(manifest: dict | None = None, *, status=None, cancel=None) -> str:
    """下载 → 校验解压 → 导入素材库，返回版本号。

    status 收到下载进度快照，导入阶段收到 {"import": [已导入, 总数]}。
    """
    from . import imgtext_ocr

    is_cancelled = cancel.is_set if cancel is not None else None
    manifest = manifest or fetch_manifest()
    if PACK_FILE not in (manifest.get("files") or {}):
        raise ValueError("不是团队素材包组件")
    root = component_dir()
    staging = root.with_name(f".materials-pack-{uuid.uuid4().hex[:8]}")
    try:
        version = imgtext_ocr.install(manifest, status=status, cancel=is_cancelled,
                                      target_dir=staging, what="团队素材包")
        if status is not None:
            status({"import": [0, 0]})
        _library().sync_pack(PACK, staging / PACK_FILE, version, cancel=is_cancelled,
                             progress=(lambda done, total: status({"import": [done, total]}))
                             if status is not None else None)
        # 导入成功才换掉留存的原件
        retired = root.with_name(root.name + ".old")
        shutil.rmtree(retired, ignore_errors=True)
        if root.exists():
            root.replace(retired)
        staging.replace(root)
        shutil.rmtree(retired, ignore_errors=True)
        return version
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall() -> int:
    """从素材库移除团队素材包导入的素材并删掉原件，返回释放的字节数。"""
    library = _library()
    freed = library.pack_bytes(PACK) + _dir_size(component_dir())
    library.remove_pack(PACK)
    shutil.rmtree(component_dir(), ignore_errors=True)
    return freed


def disk_bytes() -> int:
    try:
        return _library().pack_bytes(PACK) + _dir_size(component_dir())
    except (OSError, ValueError):
        return 0


def _dir_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

