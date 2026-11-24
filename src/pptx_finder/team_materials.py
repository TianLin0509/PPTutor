"""「团队素材包」可选组件：整包直读，不再逐页导入；安装 / 升级是原子的，失败旧库照常可用。

发布形态与文字识别 / 公式组件相同（component.json + component.zip，两层 sha256），来源一律
走 imgtext_ocr.component_sources("materials", ...)：默认只用公司内源，外网源藏在设置开关后面。
新版压缩包里是三件套 team-materials.pptx + index.json + thumbs.zip；老版只有 pptx，同样能直读
（索引现读备注 XML，缩略图缺省）。

本机目录（数据目录/materials-team）：
    state.json            {"current": {...}, "previous": {...} | null}，os.replace 原子切换
    packs/<版本>-<随机>/   每个已激活版本一份，激活后只读
    stage/                下载暂存；启动时清掉残留
    imported/<sha16>/     用户导入的素材包（同事发来的 .pptx / 离线 component.zip）

升级 5 步（m3 M2-03）：
    1 下载到 stage 并解压（imgtext_ocr.install 先验整包 sha256，再逐文件验 sha256）；
    2 校验 pptx 的 sha256、index 条数与页码、缩略图齐全；
    3 写 state.json.tmp → os.replace（原子切换），旧 current 记进 previous；
    4 每次进程启动先校验 current，坏了自动回 previous；
    5 新 current 在「下一次进程启动」校验通过后，才删 previous 和旧版逐页导入的素材
      （MaterialLibrary.remove_pack("team")，只删 .packs/team.json 登记的条目，个人收藏不碰）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from .config import data_dir
from . import materials as _m

log = logging.getLogger(__name__)

PACK = "team"
PACK_FILE = _m.PACK_PPTX
PACK_FILES = (_m.PACK_PPTX, _m.INDEX_FILE, _m.THUMBS_FILE)
#: GitHub 上首个素材包的独立标签（只在「允许外网更新源」打开时才会用到）
RELEASE_BASE = "https://github.com/TianLin0509/PPTutor/releases/download/materials-v1.0.0"
STATE_FILE = "state.json"

_lock = threading.RLock()
_started = False


def root() -> Path:
    return data_dir() / "materials-team"


def component_dir() -> Path:
    """组件在本机的目录（卸载删的就是它）。"""
    return root()


def legacy_dir() -> Path:
    """v2.1.x 之前「下载后逐页导入」留下的素材包原件。"""
    return data_dir() / "materials-pack"


def _library():
    from .materials import MaterialLibrary
    return MaterialLibrary()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 状态文件
def _state_path() -> Path:
    return root() / STATE_FILE


def read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("materials state unreadable", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def _pack_path(entry: dict | None) -> Path | None:
    if not entry or not isinstance(entry.get("dir"), str):
        return None
    name = entry["dir"]
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return None
    return root() / "packs" / name


def verify_entry(entry: dict | None) -> bool:
    """启动时的快速校验：pptx 在、sha256 对、索引条数对。"""
    folder = _pack_path(entry)
    if folder is None:
        return False
    try:
        if sha256_file(folder / PACK_FILE) != entry.get("pptx_sha256"):
            return False
        # 激活时登记的每个成员（pptx / index.json / thumbs.zip）都要在、hash 都要对
        files = entry.get("files")
        if not isinstance(files, dict) or PACK_FILE not in files:
            return False
        for name, digest in files.items():
            if name not in PACK_FILES or sha256_file(folder / name) != digest:
                return False
        reader = _m.PackReader(folder, "team")
        ok = len(reader.items) == int(entry.get("items", -1))
        reader.close()
        return ok
    except (OSError, ValueError, KeyError):
        return False


def verify_pack_dir(folder: Path, pptx_sha256: str = "") -> int:
    """安装前的完整校验，返回条数；不合格抛 ValueError（不改任何已有状态）。"""
    pptx = folder / PACK_FILE
    if not pptx.is_file():
        raise ValueError("素材包里没有 team-materials.pptx。")
    if pptx_sha256 and sha256_file(pptx) != pptx_sha256:
        raise ValueError("素材包校验失败（pptx sha256 不符）。")
    _m.validate_pptx(pptx)
    truth = _m.read_pack_index(pptx)
    index_path = folder / _m.INDEX_FILE
    if index_path.is_file():
        reader = _m.PackReader(folder, "team")
        try:
            got = [(x.id, x.page) for x in reader.items]
            if got != [(x["id"], x["page"]) for x in truth]:
                raise ValueError("素材索引与素材包不一致。")
            thumbs = folder / _m.THUMBS_FILE
            wanted = {x.thumb for x in reader.items if x.thumb}
            if wanted:
                if not thumbs.is_file():
                    raise ValueError("素材包缺少缩略图。")
                with zipfile.ZipFile(thumbs) as z:
                    missing = wanted - set(z.namelist())
                    if missing or z.testzip() is not None:
                        raise ValueError(f"缩略图不完整（缺 {len(missing)} 张）。")
        finally:
            reader.close()
    return len(truth)


# ---------------------------------------------------------------- 启动
def startup_check(*, force: bool = False) -> str:
    """每个进程第一次用到团队素材时调用一次；返回发生了什么（便于测试与日志）。"""
    global _started
    with _lock:
        if _started and not force:
            return "skipped"
        _started = True
        for leftover in (root() / "stage").glob("*") if (root() / "stage").is_dir() else ():
            shutil.rmtree(leftover, ignore_errors=True)
        state = read_state()
        cur, prev = state.get("current"), state.get("previous")
        outcome = "none"
        if cur and not verify_entry(cur):
            log.warning("materials current %s failed verification", cur.get("version"))
            if prev and verify_entry(prev):
                prev = dict(prev, confirmed=True)
                _write_state({"current": prev, "previous": None})
                outcome = "rolled_back"
            else:
                _write_state({})
                outcome = "broken"
        elif cur and not cur.get("confirmed"):
            # 新版本在这次启动校验通过：现在才清 previous 与旧版逐页导入的素材
            _write_state({"current": dict(cur, confirmed=True), "previous": None})
            _cleanup_legacy()
            outcome = "confirmed"
        elif cur:
            outcome = "ok"
        _cleanup_unreferenced()
        return outcome


def _cleanup_legacy():
    try:
        library = _library()
        if library.pack_record(PACK):
            removed = library.remove_pack(PACK)
            log.info("legacy team materials removed: %d", removed)
    except (OSError, ValueError):
        log.warning("legacy team materials cleanup failed; retry next start", exc_info=True)
        return
    shutil.rmtree(legacy_dir(), ignore_errors=True)


def _cleanup_unreferenced():
    state = read_state()
    keep = {p.name for p in (_pack_path(state.get("current")), _pack_path(state.get("previous"))) if p}
    packs = root() / "packs"
    if packs.is_dir():
        for folder in packs.iterdir():
            if folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)   # 被占用就下次启动再删


# ---------------------------------------------------------------- 安装
def sources() -> list[tuple[str, str]]:
    from . import imgtext_ocr
    return imgtext_ocr.component_sources("materials", RELEASE_BASE)


def fetch_manifest(timeout: float = 15) -> dict:
    from . import imgtext_ocr
    return imgtext_ocr.fetch_manifest_from_sources(sources(), timeout, "团队素材包")


def _new_stage() -> Path:
    stage = root() / "stage" / uuid.uuid4().hex[:12]
    stage.parent.mkdir(parents=True, exist_ok=True)
    return stage


def activate(stage: Path, version: str, *, pptx_sha256: str = "", archive_sha256: str = "",
             verified: bool = True) -> str:
    """把校验好的暂存目录换成 current（原子），旧 current 记进 previous。"""
    count = verify_pack_dir(stage, pptx_sha256)
    with _lock:
        packs = root() / "packs"
        packs.mkdir(parents=True, exist_ok=True)
        name = f"{version or 'local'}-{uuid.uuid4().hex[:8]}"
        os.replace(stage, packs / name)
        state = read_state()
        files = {n: sha256_file(packs / name / n) for n in PACK_FILES if (packs / name / n).is_file()}
        entry = {"version": version, "dir": name, "items": count, "files": files,
                 "pptx_sha256": pptx_sha256 or sha256_file(packs / name / PACK_FILE),
                 "archive_sha256": archive_sha256, "installed_at": time.time(), "confirmed": False,
                 "verified": verified}
        previous = state.get("current")
        if previous and not verify_entry(previous):
            previous = state.get("previous")
        _write_state({"current": entry, "previous": previous})
        _cleanup_unreferenced()
    return version


def install(manifest: dict | None = None, *, status=None, cancel=None) -> str:
    """下载 → 两层校验 → 原子激活，返回版本号。任一步失败旧库照常可用。"""
    from . import imgtext_ocr

    is_cancelled = cancel.is_set if cancel is not None else None
    manifest = manifest or fetch_manifest()
    files = manifest.get("files") or {}
    if PACK_FILE not in files:
        raise ValueError("不是团队素材包组件")
    stage = _new_stage()
    try:
        version = imgtext_ocr.install(manifest, status=status, cancel=is_cancelled,
                                      target_dir=stage, what="团队素材包")
        if is_cancelled is not None and is_cancelled():
            raise InterruptedError("已取消")
        return activate(stage, version, pptx_sha256=str(files[PACK_FILE].get("hash") or ""),
                        archive_sha256=str((manifest.get("archive") or {}).get("hash") or ""))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def install_from_file(path: Path) -> dict:
    """导入同事发来的素材包。返回 {"kind": "team"|"imported", "version", "items"}。

    - 离线 component.zip：旁边有 component.json 时按清单做两层校验；没有就校验 zip 自身
      CRC 并只接受三件套文件名。装成团队完整库（原子切换，同上）。
    - .pptx（PPT Doctor Material v1）：放进 imported/，整包直读，不开 PowerPoint。
    """
    from . import imgtext_ocr
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        manifest_path = path.with_name("component.json")
        if manifest_path.is_file():
            # 有清单就只走清单：JSON / 大小 / 整包 hash / 成员 hash 任何一项不符都停，指针不动
            try:
                manifest = json.loads(manifest_path.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("component.json 不是合法清单，已停止导入。") from exc
            archive = manifest.get("archive") if isinstance(manifest, dict) else None
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if (not isinstance(archive, dict) or not isinstance(files, dict) or PACK_FILE not in files
                    or manifest.get("kind", "materials") != "materials"):
                raise ValueError("component.json 不是团队素材包清单，已停止导入。")
            if int(archive.get("size") or -1) != path.stat().st_size:
                raise ValueError("压缩包大小与 component.json 不符，已停止导入。")
            if str(archive.get("hash") or "") != sha256_file(path):
                raise ValueError("压缩包校验失败（与 component.json 的 sha256 不符），已停止导入。")
            stage = _new_stage()
            try:
                version = imgtext_ocr.install(manifest, archive_path=path, target_dir=stage,
                                              what="团队素材包")
                activate(stage, version, pptx_sha256=str(files[PACK_FILE].get("hash") or ""),
                         archive_sha256=archive["hash"])
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            return {"kind": "team", "version": version, "items": read_state()["current"]["items"]}
        stage = _new_stage()
        try:
            stage.mkdir()
            with zipfile.ZipFile(path) as z:
                if z.testzip() is not None:
                    raise ValueError("压缩包损坏。")
                names = [i.filename for i in z.infolist() if not i.is_dir()]
                if PACK_FILE not in names or any(n not in PACK_FILES for n in names):
                    raise ValueError("不是团队素材包（压缩包里应只有 team-materials.pptx / index.json / thumbs.zip）。")
                for n in names:
                    (stage / n).write_bytes(z.read(n))
            version = ""
            if (stage / _m.INDEX_FILE).is_file():
                version = str(json.loads((stage / _m.INDEX_FILE).read_text("utf-8")).get("version") or "")
            # 没有清单的离线包：版本号是包自己说的，不可信。记为未核验，对外按 0.0.0 报，
            # 正式发布的任何版本都比它新，不会被一个自称 9.9.9 的包挡住更新。
            activate(stage, version or "local", archive_sha256=sha256_file(path), verified=False)
            return {"kind": "team", "version": version or "local", "items": read_state()["current"]["items"]}
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    if suffix == ".pptx":
        _m.validate_pptx(path)
        items = _m.read_pack_index(path)
        digest = sha256_file(path)[:16]
        target = root() / "imported" / digest
        if not (target / PACK_FILE).is_file():
            tmp = root() / "imported" / f".{digest}-{uuid.uuid4().hex[:6]}"
            tmp.mkdir(parents=True)
            shutil.copyfile(path, tmp / PACK_FILE)
            (tmp / "source.json").write_text(json.dumps({"name": path.name, "imported_at": time.time()},
                                                        ensure_ascii=False), "utf-8")
            os.replace(tmp, target)
        return {"kind": "imported", "version": "", "items": len(items)}
    raise ValueError("请选择素材包 .pptx 或离线组件 .zip。")


# ---------------------------------------------------------------- 读取
def current_reader():
    """已安装的团队完整库；没有返回 None。"""
    startup_check()
    state = read_state()
    for entry in (state.get("current"), state.get("previous")):
        folder = _pack_path(entry)
        if folder is not None and (folder / PACK_FILE).is_file():
            try:
                reader = _m.PackReader(folder, "team")
                reader.version = reader.version or str(entry.get("version") or "")
                return reader
            except (OSError, ValueError):
                log.warning("cannot read team pack %s", folder, exc_info=True)
    return None


class _LegacyReader(_m.PackReader):
    """旧版逐页导入的用户：原件还在 materials-pack/，缩略图借用逐页导入时生成的 preview.png。"""

    def __init__(self, folder: Path, version: str):
        super().__init__(folder, "team")
        self.version = version
        self._library = _library()

    def thumbnail(self, item):
        try:
            path = self._library.folder(_m.pack_item_id(PACK, item.id)) / "preview.png"
            return path.read_bytes()
        except (OSError, ValueError):
            return None


def legacy_reader():
    folder = legacy_dir()
    if not (folder / PACK_FILE).is_file():
        return None
    try:
        version = str(_library().pack_record(PACK).get("version") or "")
        return _LegacyReader(folder, version)
    except (OSError, ValueError):
        log.warning("legacy team pack unreadable", exc_info=True)
        return None


def builtin_reader():
    folder = _m.builtin_core_dir()
    core = folder / "team-materials-core.pptx"
    if not core.is_file():
        return None
    try:
        return _BuiltinReader(folder)
    except (OSError, ValueError):
        log.warning("builtin materials unreadable", exc_info=True)
        return None


class _BuiltinReader(_m.PackReader):
    """主包内置精选：文件名是 team-materials-core.pptx（打包白名单逐文件放行）。"""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.source = "builtin"
        self.pptx = self.folder / "team-materials-core.pptx"
        data = json.loads((self.folder / _m.INDEX_FILE).read_text("utf-8"))
        self.version = str(data.get("version") or "")
        self.items = [_m.PackItem(str(x["id"]), str(x["name"]), str(x.get("category", "")), int(x["page"]),
                                  "builtin", self.pptx, str(x.get("thumb", "")), int(x.get("w", 0) or 0),
                                  int(x.get("h", 0) or 0), str(x.get("kind", ""))) for x in data["items"]]
        self._thumbs_path = self.folder / _m.THUMBS_FILE
        self._zip = None
        self._zip_lock = threading.Lock()


def team_reader():
    """界面用的团队素材来源：完整库 > 旧版逐页导入的原件 > 主包内置精选。"""
    return current_reader() or legacy_reader() or builtin_reader()


def imported_readers() -> list:
    folder = root() / "imported"
    out = []
    if folder.is_dir():
        for sub in sorted(folder.iterdir()):
            if sub.name.startswith(".") or not (sub / PACK_FILE).is_file():
                continue
            try:
                out.append(_m.PackReader(sub, "imported"))
            except (OSError, ValueError):
                log.warning("imported pack unreadable: %s", sub, exc_info=True)
    return out


def remove_imported(reader) -> None:
    reader.close()
    folder = Path(reader.folder)
    if folder.parent == root() / "imported":
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- 组件中心接口
def installed_version() -> str:
    try:
        cur = read_state().get("current")
        if cur and _pack_path(cur) and (_pack_path(cur) / PACK_FILE).is_file():
            return str(cur.get("version") or "") if cur.get("verified", True) else "0.0.0"
        return str(_library().pack_record(PACK).get("version") or "")
    except (OSError, ValueError):
        return ""


def is_installed() -> bool:
    return bool(installed_version())


def uninstall() -> int:
    """删掉团队完整库（含旧版逐页导入的素材与原件），返回释放的字节数。内置精选不受影响。"""
    freed = disk_bytes()
    with _lock:
        library = _library()
        try:
            library.remove_pack(PACK)
        except (OSError, ValueError):
            log.warning("legacy remove failed", exc_info=True)
        shutil.rmtree(legacy_dir(), ignore_errors=True)
        imported = root() / "imported"
        keep = None
        if imported.is_dir():   # 导入的素材包是用户自己的，不随卸载删
            keep = root().with_name(f".materials-imported-{uuid.uuid4().hex[:6]}")
            os.replace(imported, keep)
        shutil.rmtree(root(), ignore_errors=True)
        if keep is not None:
            root().mkdir(parents=True, exist_ok=True)
            os.replace(keep, imported)
    return freed


def disk_bytes() -> int:
    try:
        legacy = _library().pack_bytes(PACK)
    except (OSError, ValueError):
        legacy = 0
    return legacy + _dir_size(root()) + _dir_size(legacy_dir())


def _dir_size(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
