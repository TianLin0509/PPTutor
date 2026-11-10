"""Local native-PowerPoint material library and ordinary PPTX sharing packs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

from . import config, ppt_transfer
from .native_clipboard import normalize_shapes

MARKER = "PPT Doctor Material v1\n"
MAX_PACK_ITEMS = 500


@dataclass(frozen=True)
class Material:
    id: str
    name: str
    category: str = ""
    created: float = 0


def validate_pptx(path: Path):
    """Validate before asking Office to open a shared file; no macros or linked assets."""
    if path.suffix.lower() != ".pptx" or path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("请选择不超过 100 MB 的 PPTX 素材包。")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError("素材包包含重复或过多条目。")
        if sum(i.file_size for i in z.infolist()) > 512 * 1024 * 1024:
            raise ValueError("素材包解压后过大，请拆成较小的包。")
        if "ppt/presentation.xml" not in names:
            raise ValueError("不是有效的 PowerPoint 文件。")
        for name in names:
            if "vbaproject" in name.lower() or name.startswith("ppt/embeddings/"):
                raise ValueError("素材包含宏或嵌入程序，不能作为形状素材导入。")
            if name.endswith(".rels"):
                for rel in ET.fromstring(z.read(name)):
                    if (rel.get("TargetMode") == "External" and
                            not rel.get("Type", "").endswith("/hyperlink")):
                        raise ValueError("素材引用了外部文件，请先在 PPT 中将素材嵌入后再收藏。")
        if z.testzip() is not None:
            raise ValueError("素材包文件损坏。")


def _metadata(slide, item: Material):
    slide.NotesPage.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = (
        MARKER + json.dumps(asdict(item), ensure_ascii=False))


def _read_raw(slide) -> dict:
    """备注首行标记之后的 JSON：{id, name, category}；name / category 已校验。"""
    shapes = slide.NotesPage.Shapes
    for i in range(1, int(shapes.Count) + 1):
        shape = shapes.Item(i)
        if bool(shape.HasTextFrame):
            text = str(shape.TextFrame.TextRange.Text).replace("\r", "\n")
            if text.startswith(MARKER):
                data = json.loads(text[len(MARKER):])
                name = data.get("name")
                category = data.get("category", "")
                if not isinstance(name, str) or not name.strip() or not isinstance(category, str):
                    raise ValueError("素材名称或分类格式不正确。")
                return data
    raise ValueError("不是 PPT Doctor 素材包：每页需有素材信息。普通 PPT 请选中对象后复制收藏。")


def _read_metadata(slide) -> Material:
    data = _read_raw(slide)
    return Material(uuid.uuid4().hex, data["name"][:120], data.get("category", "")[:80], time.time())


#: 组件导入的素材登记在素材库的 .packs/<包名>.json：版本号 + 这个包导入的全部素材标识。
#: 升级 / 卸载只动登记里的素材，用户自己收藏或手动导入的素材一个都不碰。
PACKS_DIR = ".packs"
_PACK_KEY = re.compile(r"[a-z][a-z0-9-]{0,31}")
_ITEM_ID = re.compile(r"[0-9a-f]{32}")


def pack_item_id(pack: str, source_id: str) -> str:
    """素材包里的 id → 素材库里的固定标识：同一个素材重复导入落在同一个目录，不会多出一份。"""
    return hashlib.md5(f"pptdoctor-pack:{pack}:{source_id}".encode("utf-8")).hexdigest()


def _save_deck(pres, path: Path):
    pres.SaveAs(str(path.resolve()), 24)  # ppSaveAsOpenXMLPresentation
    if not path.is_file():
        raise OSError("PowerPoint 没有生成素材文件。")


def _preview(slide, target: Path):
    if int(slide.Shapes.Count) == 0:
        raise ValueError("这一页没有素材对象。")
    slide.Shapes.Range().Export(str(target.resolve()), 2)  # ppShapeFormatPNG
    if not target.is_file():
        raise OSError("素材预览生成失败。")


class MaterialLibrary:
    def __init__(self, root: Path | None = None):
        self.root = root or config.data_dir() / "materials"
        self.root.mkdir(parents=True, exist_ok=True)

    def folder(self, item_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", item_id):
            raise ValueError("素材标识不合法。")
        return self.root / item_id

    def list(self, query: str = "", category: str = "") -> list[Material]:
        result = []
        for path in self.root.glob("*/item.json"):
            if path.parent.name.startswith("."):
                continue
            data = json.loads(path.read_text("utf-8"))
            item = Material(**data)
            if self.folder(item.id) != path.parent:
                raise ValueError(f"素材目录与标识不一致：{path.parent.name}")
            if ((not category or item.category == category) and
                    query.casefold() in f"{item.name} {item.category}".casefold()):
                result.append(item)
        return sorted(result, key=lambda x: x.created, reverse=True)

    @staticmethod
    def _write(folder: Path, item: Material):
        tmp = folder / "item.json.tmp"
        tmp.write_text(json.dumps(asdict(item), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, folder / "item.json")

    def rename(self, item_id: str, name: str, category: str):
        folder = self.folder(item_id)
        data = json.loads((folder / "item.json").read_text("utf-8"))
        item = Material(item_id, name.strip()[:120] or data["name"],
                        category.strip()[:80], data["created"])
        self._write(folder, item)

    def delete(self, item_ids: list[str]):
        # Retain deleted materials in a local trash directory; no irreversible removal.
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        for item_id in item_ids:
            self.folder(item_id).rename(trash / f"{item_id}-{uuid.uuid4().hex}")

    def capture(self, name: str = "", category: str = "") -> Material:
        import win32clipboard
        sequence = normalize_shapes()
        item = Material(uuid.uuid4().hex, name.strip()[:120] or
                        time.strftime("素材 %m-%d %H:%M:%S"), category.strip()[:80], time.time())
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=self.root) as tmp:
            folder = Path(tmp) / item.id
            folder.mkdir()
            with ppt_transfer.temporary_presentations() as session:
                pres = session.new()
                slide = pres.Slides.Add(1, 12)  # ppLayoutBlank
                if win32clipboard.GetClipboardSequenceNumber() != sequence:
                    raise ValueError("剪贴板在收藏期间发生变化，请重新复制后重试。")
                try:
                    shapes = slide.Shapes.Paste()
                except Exception as exc:
                    raise ValueError("剪贴板没有可收藏的素材。请先在 PPT 中选中形状或图片并 Ctrl+C。") from exc
                if not int(shapes.Count):
                    raise ValueError("剪贴板中没有素材对象。")
                # Center the selection as a whole; preserve dimensions and grouping.
                dx = (960 - float(shapes.Width)) / 2 - float(shapes.Left)
                dy = (540 - float(shapes.Height)) / 2 - float(shapes.Top)
                shapes.IncrementLeft(dx)
                shapes.IncrementTop(dy)
                _metadata(slide, item)
                _preview(slide, folder / "preview.png")
                _save_deck(pres, folder / "material.pptx")
            validate_pptx(folder / "material.pptx")
            self._write(folder, item)
            folder.rename(self.folder(item.id))
        return item

    def copy(self, item_id: str):
        # Never open a library file that the user may also have opened manually.
        with tempfile.TemporaryDirectory(prefix="material-copy-") as tmp:
            snapshot = Path(tmp) / "material.pptx"
            shutil.copyfile(self.folder(item_id) / "material.pptx", snapshot)
            ppt_transfer.copy_material(snapshot)

    def export_pack(self, item_ids: list[str], destination: Path):
        if not item_ids or len(item_ids) > MAX_PACK_ITEMS:
            raise ValueError("请选择 1–500 个素材导出。")
        # Save beside destination then atomically replace only after successful close.
        destination = destination.resolve()
        with tempfile.TemporaryDirectory(prefix=".pack-", dir=destination.parent) as tmp:
            result = Path(tmp) / "素材包.pptx"
            with ppt_transfer.temporary_presentations() as session:
                pres = session.new()
                for item_id in item_ids:
                    folder = self.folder(item_id)
                    item = Material(**json.loads((folder / "item.json").read_text("utf-8")))
                    count = pres.Slides.InsertFromFile(str(folder / "material.pptx"),
                                                       int(pres.Slides.Count), 1, 1)
                    if int(count) != 1:
                        raise ValueError(f"素材导出失败：{item.name}")
                    _metadata(pres.Slides.Item(pres.Slides.Count), item)
                _save_deck(pres, result)
            validate_pptx(result)
            os.replace(result, destination)

    def import_pack(self, source: Path) -> list[Material]:
        items = []
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as tmp:
            stage = Path(tmp)
            snapshot = stage / "source.pptx"
            shutil.copyfile(source, snapshot)
            validate_pptx(snapshot)
            with ppt_transfer.temporary_presentations() as session:
                source_pres = session.open(snapshot)
                count = int(source_pres.Slides.Count)
                if not 1 <= count <= MAX_PACK_ITEMS:
                    raise ValueError("素材包需要包含 1–500 页。")
                # Validate every page before publishing any materials.
                items = [_read_metadata(source_pres.Slides.Item(i)) for i in range(1, count + 1)]
                for i, item in enumerate(items, 1):
                    folder = stage / item.id
                    folder.mkdir()
                    pres = session.new()
                    if int(pres.Slides.InsertFromFile(str(snapshot), 0, i, i)) != 1:
                        raise ValueError(f"第 {i} 页导入失败。")
                    slide = pres.Slides.Item(1)
                    _metadata(slide, item)
                    _preview(slide, folder / "preview.png")
                    _save_deck(pres, folder / "material.pptx")
                    self._write(folder, item)
                    session.close(pres)
            published = []
            try:
                for item in items:
                    (stage / item.id).rename(self.folder(item.id))
                    published.append(item)
            except OSError:
                for item in reversed(published):
                    self.folder(item.id).rename(stage / item.id)
                raise
        return items

    # ---------- 组件素材包（团队素材包等）：按 id 去重、整包升级 ----------
    def _pack_record_path(self, pack: str) -> Path:
        if not _PACK_KEY.fullmatch(pack):
            raise ValueError("素材包名不合法。")
        return self.root / PACKS_DIR / f"{pack}.json"

    def pack_record(self, pack: str) -> dict:
        """{"version": "1.0.0", "ids": [...]}；没装过返回 {}。"""
        try:
            data = json.loads(self._pack_record_path(pack).read_text("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("ids", []), list):
            raise ValueError("素材包登记文件损坏。")
        return data

    def _pack_ids(self, record: dict) -> list[str]:
        return [x for x in record.get("ids", []) if isinstance(x, str) and _ITEM_ID.fullmatch(x)]

    def _write_pack_record(self, pack: str, record: dict):
        path = self._pack_record_path(pack)
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _discard(folder: Path):
        # 组件素材随时可重新下载：替换 / 卸载时直接删，不进 .trash 越攒越多
        trash = folder.with_name(f".discard-{uuid.uuid4().hex}")
        folder.rename(trash)
        shutil.rmtree(trash, ignore_errors=True)

    def sync_pack(self, pack: str, source: Path, version: str, *,
                  progress=None, cancel=None) -> dict:
        """把组件素材包导入素材库：同一 id 原地替换，新包里没有的旧素材移除。

        全部页面先在暂存目录拆好、生成预览，一页失败整包不落地；取消只在落地前生效。
        返回 {"total", "added", "updated", "removed"}。
        """
        record = self.pack_record(pack)
        old_ids = self._pack_ids(record)
        for leftover in self.root.glob(".discard-*"):   # 上次被打断留下的残留
            shutil.rmtree(leftover, ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix=".sync-", dir=self.root) as tmp:
            stage = Path(tmp)
            snapshot = stage / "source.pptx"
            shutil.copyfile(source, snapshot)
            validate_pptx(snapshot)
            items: list[Material] = []
            with ppt_transfer.temporary_presentations() as session:
                source_pres = session.open(snapshot)
                count = int(source_pres.Slides.Count)
                if not 1 <= count <= MAX_PACK_ITEMS:
                    raise ValueError("素材包需要包含 1–500 页。")
                raws = [_read_raw(source_pres.Slides.Item(i)) for i in range(1, count + 1)]
                seen = set()
                base = time.time()
                for i, raw in enumerate(raws, 1):
                    source_id = raw.get("id")
                    if not isinstance(source_id, str) or not source_id.strip():
                        raise ValueError(f"第 {i} 页素材缺少 id。")
                    if source_id in seen:
                        raise ValueError(f"第 {i} 页素材 id 重复：{source_id}")
                    seen.add(source_id)
                    # created 依次递减：素材库按时间倒序显示时保持素材包里的顺序
                    items.append(Material(pack_item_id(pack, source_id), raw["name"][:120],
                                          raw.get("category", "")[:80], base - i * 1e-3))
                if progress:
                    progress(0, count)
                for i, item in enumerate(items, 1):
                    if cancel is not None and cancel():
                        raise InterruptedError("已取消导入")
                    folder = stage / item.id
                    folder.mkdir()
                    pres = session.new()
                    if int(pres.Slides.InsertFromFile(str(snapshot), 0, i, i)) != 1:
                        raise ValueError(f"第 {i} 页导入失败。")
                    slide = pres.Slides.Item(1)
                    _metadata(slide, item)
                    _preview(slide, folder / "preview.png")
                    _save_deck(pres, folder / "material.pptx")
                    self._write(folder, item)
                    session.close(pres)
                    if progress:
                        progress(i, count)
            if cancel is not None and cancel():
                raise InterruptedError("已取消导入")
            new_ids = [item.id for item in items]
            # 先登记新旧并集：中途被打断时，卸载 / 下次升级仍能找全这个包的素材
            self._write_pack_record(pack, {"version": record.get("version", ""),
                                           "ids": list(dict.fromkeys(old_ids + new_ids))})
            stats = {"total": len(items), "added": 0, "updated": 0, "removed": 0}
            for item in items:
                target = self.folder(item.id)
                if target.exists():
                    self._discard(target)
                    stats["updated"] += 1
                else:
                    stats["added"] += 1
                (stage / item.id).rename(target)
            keep = set(new_ids)
            for item_id in old_ids:
                if item_id not in keep and self.folder(item_id).exists():
                    self._discard(self.folder(item_id))
                    stats["removed"] += 1
            self._write_pack_record(pack, {"version": str(version), "ids": new_ids})
        return stats

    def remove_pack(self, pack: str) -> int:
        """删掉某个组件素材包导入的全部素材（只按登记删），返回删除个数。"""
        removed = 0
        for item_id in self._pack_ids(self.pack_record(pack)):
            folder = self.folder(item_id)
            if folder.exists():
                self._discard(folder)
                removed += 1
        self._pack_record_path(pack).unlink(missing_ok=True)
        return removed

    def pack_bytes(self, pack: str) -> int:
        total = 0
        for item_id in self._pack_ids(self.pack_record(pack)):
            for path in (self.root / item_id).glob("*"):
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

