"""Local native-PowerPoint material library and ordinary PPTX sharing packs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
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


def _read_metadata(slide) -> Material:
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
                return Material(uuid.uuid4().hex, name[:120], category[:80], time.time())
    raise ValueError("不是 PPT Doctor 素材包：每页需有素材信息。普通 PPT 请选中对象后复制收藏。")


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
