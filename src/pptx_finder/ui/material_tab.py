"""素材库一级页（方案 D 三栏）：左分类 / 中搜索 + 分页网格 / 右预览 + 插入。

数据全部整包直读：团队完整库（或主包内置精选）+ 导入的素材包 + 我的收藏，浏览、搜索、预览不需要
PowerPoint；只有「插入到当前 PPT」「复制形状」「收藏一份到我的」才用 PowerPoint，并且只打开素材包的
临时副本（只读、无窗口），只关自己开的，不保存、不关闭用户文稿。

挂载合同（phase2-contract.md §3.1）：
    MaterialTabWidget(parent=None, *, data_dir=None, component_center=None, theme_tokens=None)
    apply_theme(tokens) / refresh_library() / focus_search() / import_material_file(path) /
    collect_from_clipboard()；信号 toastRequested(message, level)、statusTextChanged(text)。
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import logging
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QMimeData, QPoint, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDrag, QFont, QFontMetrics, QGuiApplication, QImage, QKeySequence, QPainter,
    QPainterPath, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import materials as mat
from .. import ppt_transfer, team_materials
from .bg_task import BackgroundTask

log = logging.getLogger(__name__)

GAP = 12
CARD_H, CARD_H_NARROW = 150, 128
CARD_MIN_W = 128
LIST_ROW_H = 60
NARROW_WIDTH = 1100
PERSONAL_SOFT_LIMIT = 200 * 1024 * 1024

#: 方案 D 晨雾玻璃的默认值（主窗没传主题时用；键名按 phase2-contract §3.2）
DEFAULT_TOKENS = {
    "name": "mist_glass", "is_dark": False, "is_glass": True, "floating_cards": True,
    "appbg": "#F1F3F5", "surface": "#FFFFFF", "surface_hover": "rgba(24,36,49,0.05)",
    "surface_border": "#E2E6EA", "text": "#182431", "text_muted": "#6E7781", "text_faint": "#A2A8AF",
    "accent": "#0A59F7", "accent_hover": "#0047D9", "accent_text": "#FFFFFF", "accent_soft": "#E7EFFE",
    "danger": "#E84026", "warn": "#B26A00", "warn_soft": "#FFF4E0", "ok": "#3A9E42", "field": "#EDEFF2",
}
#: 旧主题 token（theme.tok）→ 合同键
_LEGACY_KEYS = {"surface": "panel", "surface_hover": "hover", "surface_border": "bd", "text": "ink1",
                "text_muted": "ink3", "text_faint": "ink4", "accent": "acc", "accent_hover": "accd",
                "accent_text": "acctext", "accent_soft": "sel", "ok": "grn", "field": "field",
                "appbg": "appbg"}

SOURCE_LABEL = {"team": "团队素材包", "builtin": "团队素材（内置精选）", "imported": "导入的素材包",
                "mine": "我的收藏"}
KIND_LABEL = {"shape": "原生形状，可改色、可改文字", "picture": "图片", "mixed": "形状 + 图片，可编辑",
              "": "原生形状，可编辑"}
#: 这几种失败给「改为复制形状」：用户回 PPT 自己 Ctrl+V 就行
_OFFER_COPY = {"no_powerpoint", "no_presentation", "read_only", "no_target_slide", "ppt_unreachable", ""}
#: 插入失败时还可以「用 PowerPoint 打开这个素材」：没开 / 连不上 PowerPoint、原因不明
_OFFER_OPEN = {"no_powerpoint", "no_presentation", "read_only", "ppt_unreachable", ""}


def resolve_tokens(tokens: dict | None) -> dict:
    out = dict(DEFAULT_TOKENS)
    if not tokens:
        return out
    for key, legacy in _LEGACY_KEYS.items():
        if key not in tokens and legacy in tokens and isinstance(tokens[legacy], str):
            out[key] = tokens[legacy]
    if "is_dark" not in tokens and "is_light" in tokens:
        out["is_dark"] = not tokens["is_light"]
    out.update({k: v for k, v in tokens.items() if k in DEFAULT_TOKENS})
    if out["is_dark"] and "warn_soft" not in tokens:
        out["warn_soft"] = "rgba(255,170,40,0.14)"
    return _normalize_colors(out)


_RGBA = __import__("re").compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")
_BOOL_KEYS = {"name", "is_dark", "is_glass", "floating_cards"}


def _to_hex(value: str) -> str | None:
    m = _RGBA.fullmatch(value.strip())
    if m:
        r, g, b = (max(0, min(255, int(float(x)))) for x in m.group(1, 2, 3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        a = max(0, min(255, round(a * 255 if a <= 1 else a)))
        return QColor(r, g, b, a).name(QColor.HexArgb)
    color = QColor(value)
    return color.name(QColor.HexArgb) if color.isValid() else None


def _normalize_colors(tokens: dict) -> dict:
    """自绘用 QColor(str)：它不认 rgba(...)，会画成黑块。统一转 #AARRGGBB，坏值退默认。"""
    out = dict(tokens)
    for key, value in tokens.items():
        if key in _BOOL_KEYS or not isinstance(value, str):
            continue
        fixed = _to_hex(value)
        out[key] = fixed if fixed is not None else _to_hex(DEFAULT_TOKENS.get(key, "#000000"))
    return out


@dataclass
class Entry:
    """界面上的一条素材：团队 / 内置 / 导入（PackItem）或我的收藏（Material）。"""
    key: str
    name: str
    category: str
    source: str
    item: object
    reader: object = None

    @property
    def editable(self) -> bool:
        return self.source == "mine"


# ---------------------------------------------------------------- 缩略图缓存
class ThumbCache:
    # Keep decoded originals at full resolution. Only the current viewport and
    # detail selection may exceed this budget; previously visited pages cannot.
    MAX_BYTES = 64 * 1024 * 1024
    MAX_IMAGES = 256
    MAX_MISSING = 512

    def __init__(self, library: mat.MaterialLibrary):
        self.library = library
        self._images: OrderedDict[str, QImage] = OrderedDict()
        self._missing: OrderedDict[str, None] = OrderedDict()
        self._pinned: set[str] = set()
        self._bytes = 0

    def get(self, entry: Entry) -> QImage | None:
        image = self._images.get(entry.key)
        if image is not None:
            self._images.move_to_end(entry.key)
        return image

    def set_pinned(self, keys: set[str]) -> None:
        self._pinned = keys
        self._trim()

    def _trim(self) -> None:
        for key in list(self._images):
            if self._bytes <= self.MAX_BYTES and len(self._images) <= self.MAX_IMAGES:
                break
            if key not in self._pinned:
                self._bytes -= self._images.pop(key).sizeInBytes()
        while len(self._missing) > self.MAX_MISSING:
            self._missing.popitem(last=False)

    def needs(self, entry: Entry) -> bool:
        return entry.key not in self._images and entry.key not in self._missing

    def decode(self, entries: list[Entry]) -> dict[str, QImage | None]:
        """工作线程里解码（QImage 可跨线程）。"""
        out = {}
        for e in entries:
            data = None
            try:
                if e.source == "mine":
                    data = (self.library.folder(e.item.id) / "preview.png").read_bytes()
                elif e.reader is not None:
                    data = e.reader.thumbnail(e.item)
            except (OSError, ValueError):
                data = None
            image = QImage.fromData(data) if data else QImage()
            out[e.key] = None if image.isNull() else image
        return out

    def store(self, decoded: dict):
        for key, image in decoded.items():
            if image is None:
                self._missing[key] = None
                self._missing.move_to_end(key)
            else:
                old = self._images.pop(key, None)
                if old is not None:
                    self._bytes -= old.sizeInBytes()
                self._images[key] = image
                self._bytes += image.sizeInBytes()
                self._missing.pop(key, None)
        self._trim()

    def clear(self):
        self._images.clear()
        self._missing.clear()
        self._pinned.clear()
        self._bytes = 0


# ---------------------------------------------------------------- 网格
class MaterialGrid(QWidget):
    """按可用宽高算每页张数的分页网格（也能切列表），不出纵向滚动条。"""
    selectedChanged = Signal(int)       # 全局下标，-1 = 无
    activated = Signal(int)             # 双击 = 复制形状
    pagingChanged = Signal(int, int, int)   # 当前页（从 0）、总页数、每页张数

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.setObjectName("mtGrid")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(200, 110)
        self.cache = cache
        self.entries: list[Entry] = []
        self.page = 0
        self.selected = -1
        self.hover = -1
        self.list_mode = False
        self.tok = dict(DEFAULT_TOKENS)

    # ---- 布局 ----
    def layout_info(self) -> tuple[int, int, int, int]:
        """(列数, 行数, 卡片宽, 卡片高)"""
        w, h = max(1, self.width()), max(1, self.height())
        if self.list_mode:
            return 1, max(1, (h + 4) // (LIST_ROW_H + 4)), w, LIST_ROW_H
        card_h = CARD_H if (h + GAP) // (CARD_H + GAP) >= 4 or h >= 4 * CARD_H else CARD_H_NARROW
        cols = max(1, (w + GAP) // (CARD_MIN_W + GAP))
        rows = max(1, (h + GAP) // (card_h + GAP))
        card_w = (w - GAP * (cols - 1)) // cols
        return cols, rows, card_w, card_h

    def per_page(self) -> int:
        cols, rows, _, _ = self.layout_info()
        return cols * rows

    def page_count(self) -> int:
        return max(1, -(-len(self.entries) // self.per_page()))

    def visible_range(self) -> range:
        per = self.per_page()
        start = self.page * per
        return range(start, min(len(self.entries), start + per))

    def set_entries(self, entries: list[Entry], keep_key: str = ""):
        self.entries = entries
        self.page = 0
        self.selected = -1
        if keep_key:
            for i, e in enumerate(entries):
                if e.key == keep_key:
                    self.selected = i
                    self.page = i // self.per_page()
                    break
        self._emit_paging()
        self.update()

    def set_page(self, page: int):
        page = max(0, min(page, self.page_count() - 1))
        if page != self.page:
            self.page = page
            self._emit_paging()
            self.update()

    def select(self, index: int):
        if not self.entries:
            index = -1
        else:
            index = max(0, min(index, len(self.entries) - 1))
        if index != self.selected:
            self.selected = index
            if index >= 0:
                self.set_page(index // self.per_page())
            self.update()
            self.selectedChanged.emit(index)

    def _emit_paging(self):
        self.pagingChanged.emit(self.page, self.page_count(), self.per_page())

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.selected >= 0:
            self.page = self.selected // self.per_page()
        self.page = min(self.page, self.page_count() - 1)
        self._emit_paging()

    def _rect(self, slot: int) -> QRect:
        cols, _, cw, ch = self.layout_info()
        r, c = divmod(slot, cols)
        if self.list_mode:
            return QRect(0, r * (ch + 4), cw, ch)
        return QRect(c * (cw + GAP), r * (ch + GAP), cw, ch)

    def index_at(self, pos: QPoint) -> int:
        for slot, i in enumerate(self.visible_range()):
            if self._rect(slot).contains(pos):
                return i
        return -1

    # ---- 绘制 ----
    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        t = self.tok
        name_font = QFont(self.font())
        name_font.setPixelSize(13)
        small = QFont(self.font())
        small.setPixelSize(12)
        for slot, i in enumerate(self.visible_range()):
            e = self.entries[i]
            rect = self._rect(slot)
            if i == self.selected or i == self.hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(t["accent_soft"] if i == self.selected else t["surface_hover"]))
                p.drawRoundedRect(QRectF(rect), 12, 12)
            if self.list_mode:
                thumb = QRect(rect.x() + 8, rect.y() + 6, 64, rect.height() - 12)
                self._thumb(p, e, thumb)
                p.setPen(QColor(t["text"]))
                p.setFont(name_font)
                fm = QFontMetrics(name_font)
                tx = thumb.right() + 12
                p.drawText(QRect(tx, rect.y() + 10, rect.width() - tx - 8, 20), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(e.name, Qt.ElideRight, rect.width() - tx - 8))
                p.setFont(small)
                p.setPen(QColor(t["text_muted"]))
                p.drawText(QRect(tx, rect.y() + 32, rect.width() - tx - 8, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{mat.short_category(e.category) or '未分类'} · {SOURCE_LABEL[e.source]}")
                continue
            thumb = QRect(rect.x() + 6, rect.y() + 6, rect.width() - 12, rect.height() - 34)
            self._thumb(p, e, thumb)
            if e.source == "mine":
                p.setFont(small)
                badge = QRect(thumb.x() + 6, thumb.y() + 6, 34, 18)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(t["field"]))
                p.drawRoundedRect(QRectF(badge), 5, 5)
                p.setPen(QColor(t["text_muted"]))
                p.drawText(badge, Qt.AlignCenter, "收藏")
            p.setFont(name_font)
            p.setPen(QColor(t["text"]))
            fm = QFontMetrics(name_font)
            p.drawText(QRect(rect.x() + 8, rect.bottom() - 25, rect.width() - 16, 20),
                       Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(e.name, Qt.ElideRight, rect.width() - 16))
        p.end()

    def _thumb(self, p: QPainter, e: Entry, box: QRect):
        t = self.tok
        path = QPainterPath()
        path.addRoundedRect(QRectF(box), 8, 8)
        p.fillPath(path, QColor("#FFFFFF"))
        p.setPen(QPen(QColor(t["surface_border"]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        image = self.cache.get(e)
        inner = box.adjusted(8, 8, -8, -8)
        if image is not None and not image.isNull():
            size = image.size().scaled(inner.size(), Qt.KeepAspectRatio)
            if size.width() > image.width() * 1.0 and size.height() > image.height() * 1.0:
                size = image.size()     # 小图不放大，居中
            target = QRect(0, 0, size.width(), size.height())
            target.moveCenter(inner.center())
            p.drawImage(target, image)
        else:
            p.setPen(QColor("#A2A8AF"))
            f = QFont(self.font())
            f.setPixelSize(12)
            p.setFont(f)
            p.drawText(inner, Qt.AlignCenter | Qt.TextWordWrap, e.name)

    # ---- 交互 ----
    def mousePressEvent(self, e):  # noqa: N802
        i = self.index_at(e.position().toPoint())
        if i >= 0:
            self.select(i)
        self.setFocus()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        i = self.index_at(e.position().toPoint())
        if i >= 0:
            self.activated.emit(i)

    def mouseMoveEvent(self, e):  # noqa: N802
        i = self.index_at(e.position().toPoint())
        if i != self.hover:
            self.hover = i
            e_ = self.entries[i] if i >= 0 else None
            self.setToolTip(f"{e_.name}\n{e_.category or '未分类'} · 双击复制形状" if e_ else "")
            self.update()

    def leaveEvent(self, e):  # noqa: N802
        self.hover = -1
        self.update()
        super().leaveEvent(e)

    def keyPressEvent(self, e):  # noqa: N802
        cols = 1 if self.list_mode else self.layout_info()[0]
        key = e.key()
        moves = {Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -cols, Qt.Key_Down: cols}
        if key in moves and self.entries:
            self.select(max(0, self.selected) + moves[key] if self.selected >= 0 else 0)
        elif key == Qt.Key_PageDown:
            self.set_page(self.page + 1)
        elif key == Qt.Key_PageUp:
            self.set_page(self.page - 1)
        elif key in (Qt.Key_Return, Qt.Key_Enter) and self.selected >= 0:
            self.activated.emit(self.selected)
        else:
            super().keyPressEvent(e)

    def wheelEvent(self, e):  # noqa: N802
        # 滚轮翻页（网格本身不滚动）
        self.set_page(self.page + (1 if e.angleDelta().y() < 0 else -1))


class PreviewView(QWidget):
    """右栏 4:3 大图：棋盘浅底、等比居中；按住拖出 = PNG 图片（PowerPoint 只接受图片拖入）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mtPreview")
        self.image: QImage | None = None
        self.label = ""
        self._press = None
        # 最小高度与状态无关（m1 MF-1）：理想 4:3，空间不够时布局把它压到最小 80
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(80)

    def set_image(self, image: QImage | None, label: str):
        self.image, self.label = image, label
        self.setToolTip("按住拖进 PowerPoint（拖出的是图片）" if image is not None else "")
        self.update()

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        w = max(self.width(), 240)
        return QSize(w, w * 3 // 4)

    def minimumSizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(120, 80)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        cap = max(80, self.width() * 3 // 4)
        if self.maximumHeight() != cap:
            self.setMaximumHeight(cap)
            self.updateGeometry()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 12, 12)
        p.setClipPath(path)
        p.fillRect(rect, QColor("#FFFFFF"))
        cell = 8
        for y in range(0, rect.height(), cell):
            for x in range((y // cell) % 2 * cell, rect.width(), cell * 2):
                p.fillRect(x, y, cell, cell, QColor("#F7F8FA"))
        p.setClipping(False)
        p.setPen(QPen(QColor("#E2E6EA"), 1))
        p.drawPath(path)
        inner = rect.adjusted(16, 16, -16, -16)
        if self.image is not None and not self.image.isNull():
            size = self.image.size()
            if size.width() > inner.width() or size.height() > inner.height():
                size = size.scaled(inner.size(), Qt.KeepAspectRatio)
            target = QRect(0, 0, size.width(), size.height())
            target.moveCenter(inner.center())
            p.drawImage(target, self.image)
        elif self.label:
            p.setPen(QColor("#A2A8AF"))
            p.drawText(inner, Qt.AlignCenter | Qt.TextWordWrap, self.label + "\n（没有预渲染图）")
        p.end()

    def mousePressEvent(self, e):  # noqa: N802
        self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):  # noqa: N802
        if (self._press is None or self.image is None or
                (e.position().toPoint() - self._press).manhattanLength() < QGuiApplication.styleHints().startDragDistance()):
            return
        self._press = None
        folder = Path(tempfile.gettempdir()) / "ppt-doctor-material-drag"
        folder.mkdir(exist_ok=True)
        safe = "".join(c for c in self.label if c not in '\\/:*?"<>|')[:60] or "素材"
        path = folder / f"{safe}.png"
        self.image.save(str(path), "PNG")
        mime = QMimeData()
        mime.setImageData(self.image)
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(QPixmap.fromImage(self.image).scaled(96, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        drag.exec(Qt.CopyAction)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._press = None


# ---------------------------------------------------------------- 主控件
class MaterialTabWidget(QWidget):
    toastRequested = Signal(str, str)
    statusTextChanged = Signal(str)

    def __init__(self, parent=None, *, data_dir=None, component_center=None, theme_tokens=None,
                 library: mat.MaterialLibrary | None = None):
        super().__init__(parent)
        self.setObjectName("materialTab")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        root_dir = Path(data_dir) / "materials" if data_dir else None
        self.library = library or mat.MaterialLibrary(root_dir)
        self.cache = ThumbCache(self.library)
        self._center = component_center if component_center is not None else self._default_center()
        self._tasks: list[BackgroundTask] = []
        self._busy_op = False
        self._all: list[Entry] = []
        self._view: list[Entry] = []
        self._team_reader = None
        self._imported_readers: list = []
        self._src = "all"
        self._cat = ""
        self._collapsed: set[str] = set()
        self._closing = False
        self._accordion = False
        self._user_collapsed = False
        self._loaded = False
        self._load_gen = 0
        self._decode_pending: set[tuple[int, str]] = set()
        self._last_menu = None
        self._personal_bytes = 0
        self._last_error_code = ""
        self._op_entry = None
        self.tok = resolve_tokens(theme_tokens)
        self._build()
        self.apply_theme(theme_tokens or {})
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_filter)
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(60)
        self._relayout_timer.timeout.connect(self._build_categories)
        if self._center is not None:
            try:
                self._center.state_changed.connect(self._on_center_state)
                self._center.installed.connect(self._on_center_installed)
            except Exception:  # noqa: BLE001 下载中心不可用时素材页照常能用
                log.exception("component center signals unavailable")
        self.refresh_library()

    @staticmethod
    def _default_center():
        try:
            from .component_center import component_center
            return component_center()
        except Exception:  # noqa: BLE001
            log.exception("component center unavailable")
            return None

    # ---------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)
        # 页头
        head = QHBoxLayout()
        head.setSpacing(10)
        self.title = QLabel("素材库")
        self.title.setObjectName("mtTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("mtSub")
        self.pill = QLabel("正在读取素材…")
        self.pill.setObjectName("mtPill")
        self.fetch_btn = QPushButton("获取完整库")
        self.fetch_btn.setObjectName("mtGhost")
        self.fetch_btn.clicked.connect(self._on_fetch)
        self.fetch_btn.hide()
        self.capture_btn = QPushButton("☆ 从剪贴板收藏")
        self.capture_btn.setObjectName("mtSecondary")
        self.capture_btn.setToolTip("在 PPT 里选中形状按 Ctrl+C，再点这里")
        self.capture_btn.clicked.connect(self.collect_from_clipboard)
        self.import_btn = QPushButton("导入素材包…")
        self.import_btn.setObjectName("mtGhost")
        self.import_btn.clicked.connect(self._pick_import)
        head.addWidget(self.title)
        head.addWidget(self.subtitle)
        head.addStretch(1)
        head.addWidget(self.pill)
        head.addWidget(self.fetch_btn)
        head.addWidget(self.capture_btn)
        head.addWidget(self.import_btn)
        root.addLayout(head)
        # 三栏
        body = QHBoxLayout()
        body.setSpacing(10)
        self.left = self._island()
        self.mid = self._island()
        self.right = self._island()
        body.addWidget(self.left)
        body.addWidget(self.mid, 1)
        body.addWidget(self.right)
        root.addLayout(body, 1)
        self._build_left()
        self._build_mid()
        self._build_right()
        self._drop_hint = QLabel("松开导入素材包（.pptx / .zip）", self.mid)
        self._drop_hint.setObjectName("mtDrop")
        self._drop_hint.setAlignment(Qt.AlignCenter)
        self._drop_hint.hide()
        self._shortcuts = []
        for keys, slot in ((QKeySequence(QKeySequence.Copy), self._copy_shape),
                           (QKeySequence(QKeySequence.Delete), self._delete_mine),
                           (QKeySequence("F2"), self._edit_mine),
                           (QKeySequence(QKeySequence.Find), self.focus_search)):
            sc = QShortcut(keys, self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

    def _island(self) -> QFrame:
        f = QFrame()
        f.setObjectName("mtIsland")
        f.setAttribute(Qt.WA_StyledBackground, True)
        return f

    def _build_left(self):
        lay = QVBoxLayout(self.left)
        lay.setContentsMargins(8, 8, 8, 0)
        lay.setSpacing(0)
        self.cat_host = QWidget()
        self.cat_layout = QVBoxLayout(self.cat_host)
        self.cat_layout.setContentsMargins(0, 0, 0, 0)
        self.cat_layout.setSpacing(1)
        scroll = QScrollArea()
        scroll.setObjectName("mtCatScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.cat_host)
        lay.addWidget(scroll, 1)
        self.left_foot = QLabel("本地 · 离线可用\n更新来源：公司内源")
        self.left_foot.setObjectName("mtFoot")
        lay.addWidget(self.left_foot)
        self._cat_buttons: dict[str, QPushButton] = {}

    def _build_mid(self):
        lay = QVBoxLayout(self.mid)
        lay.setContentsMargins(16, 12, 16, 8)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("mtSearch")
        self.search.setPlaceholderText("搜名称、分类，空格分词")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(380)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        self.count_label = QLabel("")
        self.count_label.setObjectName("mtMeta")
        bar.addWidget(self.search, 1)
        bar.addWidget(self.count_label)
        bar.addStretch()
        self.src_group = QButtonGroup(self)
        self.src_buttons = {}
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for key, text in (("all", "全部"), ("team", "团队"), ("mine", "我的收藏")):
            b = QPushButton(text)
            b.setObjectName("mtSeg")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self._set_source(k))
            self.src_group.addButton(b)
            self.src_buttons[key] = b
            seg.addWidget(b)
        self.src_buttons["all"].setChecked(True)
        bar.addLayout(seg)
        self.view_buttons = {}
        vseg = QHBoxLayout()
        vseg.setSpacing(0)
        vgroup = QButtonGroup(self)
        for key, text in (("grid", "网格"), ("list", "列表")):
            b = QPushButton(text)
            b.setObjectName("mtSeg")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self._set_view(k))
            vgroup.addButton(b)
            self.view_buttons[key] = b
            vseg.addWidget(b)
        self.view_buttons["grid"].setChecked(True)
        bar.addLayout(vseg)
        lay.addLayout(bar)
        # 就地通知条（获取进度 / 内源连不上 / 个人收藏超量提示）
        self.notice = QFrame()
        self.notice.setObjectName("mtNotice")
        self.notice.setAttribute(Qt.WA_StyledBackground, True)
        nl = QHBoxLayout(self.notice)
        nl.setContentsMargins(12, 8, 8, 8)
        self.notice_text = QLabel("")
        self.notice_text.setWordWrap(True)
        self.notice_text.setObjectName("mtNoticeText")
        self.notice_bar = QProgressBar()
        self.notice_bar.setObjectName("mtBar")
        self.notice_bar.setFixedWidth(120)
        self.notice_bar.setTextVisible(False)
        self.notice_bar.setRange(0, 1000)
        self.notice_import = QPushButton("导入素材包…")
        self.notice_import.setObjectName("mtSecondary")
        self.notice_import.clicked.connect(self._pick_import)
        self.notice_retry = QPushButton("重试")
        self.notice_retry.setObjectName("mtGhost")
        self.notice_retry.clicked.connect(self._on_fetch)
        nl.addWidget(self.notice_text, 1)
        nl.addWidget(self.notice_bar)
        nl.addWidget(self.notice_import)
        nl.addWidget(self.notice_retry)
        self.notice.hide()
        lay.addWidget(self.notice)
        # 网格 / 空态
        self.stack = QStackedWidget()
        self.grid = MaterialGrid(self.cache)
        self.grid.selectedChanged.connect(self._on_selected)
        self.grid.activated.connect(lambda _: self._copy_shape())
        self.grid.pagingChanged.connect(self._on_paging)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_context_menu)
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setAlignment(Qt.AlignCenter)
        el.setSpacing(10)
        self.empty_title = QLabel("正在读取素材…")
        self.empty_title.setObjectName("mtEmptyTitle")
        self.empty_title.setAlignment(Qt.AlignCenter)
        self.empty_tip = QLabel("")
        self.empty_tip.setObjectName("mtEmptyTip")
        self.empty_tip.setAlignment(Qt.AlignCenter)
        self.empty_tip.setWordWrap(True)
        eb = QHBoxLayout()
        eb.setAlignment(Qt.AlignCenter)
        self.empty_primary = QPushButton("")
        self.empty_primary.setObjectName("mtPrimary")
        self.empty_secondary = QPushButton("")
        self.empty_primary.hide()
        self.empty_secondary.hide()
        self.empty_secondary.setObjectName("mtSecondary")
        eb.addWidget(self.empty_primary)
        eb.addWidget(self.empty_secondary)
        el.addWidget(self.empty_title)
        el.addWidget(self.empty_tip)
        el.addLayout(eb)
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.empty)
        self.stack.setCurrentWidget(self.empty)
        lay.addWidget(self.stack, 1)
        # 翻页
        pager = QHBoxLayout()
        pager.setSpacing(10)
        self.prev_btn = QPushButton("‹ 上一页")
        self.prev_btn.setObjectName("mtGhost")
        self.prev_btn.clicked.connect(lambda: self.grid.set_page(self.grid.page - 1))
        self.page_label = QLabel("1 / 1")
        self.page_label.setObjectName("mtMeta")
        self.next_btn = QPushButton("下一页 ›")
        self.next_btn.setObjectName("mtGhost")
        self.next_btn.clicked.connect(lambda: self.grid.set_page(self.grid.page + 1))
        self.pager_hint = QLabel("")
        self.pager_hint.setObjectName("mtMeta")
        self.export_btn = QPushButton("导出收藏为素材包…")
        self.export_btn.setObjectName("mtGhost")
        self.export_btn.clicked.connect(self._export_mine)
        self.export_btn.hide()
        pager.addStretch()
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
        pager.addWidget(self.pager_hint)
        pager.addStretch()
        pager.addWidget(self.export_btn)
        lay.addLayout(pager)

    def _build_right(self):
        lay = QVBoxLayout(self.right)
        lay.setContentsMargins(16, 12, 16, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("预览")
        t.setObjectName("mtIslandTitle")
        self.det_cat = QLabel("")
        self.det_cat.setObjectName("mtMeta")
        head.addWidget(t)
        head.addStretch()
        head.addWidget(self.det_cat)
        lay.addLayout(head)
        self.preview = PreviewView()
        lay.addWidget(self.preview)
        self.det_pill = QLabel("")
        self.det_pill.setObjectName("mtCatPill")
        pill_row = QHBoxLayout()
        pill_row.addWidget(self.det_pill)
        pill_row.addStretch()
        lay.addLayout(pill_row)
        self.det_name = QLabel("选一个素材看大图")
        self.det_name.setObjectName("mtDetName")
        self.det_name.setWordWrap(True)
        lay.addWidget(self.det_name)
        kv = QFormLayout()
        kv.setHorizontalSpacing(10)
        kv.setVerticalSpacing(6)
        self.det_source = QLabel("")
        self.det_kind = QLabel("")
        for w in (self.det_source, self.det_kind):
            w.setObjectName("mtKv")
            w.setWordWrap(True)
        k1, k2 = QLabel("来源"), QLabel("类型")
        k1.setObjectName("mtKey")
        k2.setObjectName("mtKey")
        kv.addRow(k1, self.det_source)
        kv.addRow(k2, self.det_kind)
        self.det_meta = QWidget()
        kv.setContentsMargins(0, 0, 0, 0)
        self.det_meta.setLayout(kv)
        lay.addWidget(self.det_meta)
        lay.addStretch(1)
        # 失败就地提示（不弹窗）
        self.warn = QFrame()
        self.warn.setObjectName("mtWarn")
        self.warn.setAttribute(Qt.WA_StyledBackground, True)
        wl = QVBoxLayout(self.warn)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.setSpacing(8)
        self.warn_text = QLabel("")
        self.warn_text.setObjectName("mtWarnText")
        self.warn_text.setWordWrap(True)
        self.warn_copy = QPushButton("改为复制形状")
        self.warn_copy.setObjectName("mtSecondary")
        self.warn_copy.clicked.connect(self._copy_shape)
        # 兜底：不经 COM，把这一页另存成单页 PPT 用默认程序打开，在里面复制
        self.warn_open = QPushButton("用 PPT 打开素材")
        self.warn_open.setObjectName("mtSecondary")
        self.warn_open.setToolTip("把这一页另存成单页 PPT 并用 PowerPoint 打开，在里面选中形状复制；不需要连接 PowerPoint")
        self.warn_open.clicked.connect(self._open_in_powerpoint)
        warn_row = QHBoxLayout()
        warn_row.setSpacing(8)
        warn_row.addWidget(self.warn_copy)
        warn_row.addWidget(self.warn_open)
        wl.addWidget(self.warn_text)
        wl.addLayout(warn_row)
        self.warn.setVisible(False)
        self._warn_on = False
        lay.addWidget(self.warn)
        self.insert_btn = QPushButton("插入到当前 PPT")
        self.insert_btn.setObjectName("mtPrimary")
        self.insert_btn.setMinimumHeight(36)
        self.insert_btn.clicked.connect(self._insert)
        self.copy_btn = QPushButton("复制形状")
        self.copy_btn.setObjectName("mtSecondary")
        self.copy_btn.setMinimumHeight(36)
        self.copy_btn.clicked.connect(self._copy_shape)
        lay.addWidget(self.insert_btn)
        lay.addWidget(self.copy_btn)
        links = QHBoxLayout()
        links.setSpacing(12)
        self.copy_image_link = self._link("复制为图片", self._copy_image)
        self.save_mine_link = self._link("收藏一份到我的", self._save_to_mine)
        self.edit_link = self._link("改名 / 分类", self._edit_mine)
        self.delete_link = self._link("移出收藏", self._delete_mine)
        self.remove_import_link = self._link("移除这个导入包", self._remove_imported)
        for w in (self.copy_image_link, self.save_mine_link, self.edit_link, self.delete_link,
                  self.remove_import_link):
            links.addWidget(w)
        links.addStretch()
        self.links_row = QWidget()
        links.setContentsMargins(0, 0, 0, 0)
        self.links_row.setLayout(links)
        lay.addWidget(self.links_row)
        self.caption = QLabel("插入会贴到 PowerPoint 当前文稿的当前页，形状可编辑，不会自动保存。"
                              "大图可直接拖进 PPT（拖出的是图片）。")
        self.caption.setObjectName("mtCaption")
        self.caption.setWordWrap(True)
        self.caption.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        lay.addWidget(self.caption)
        self.status = QLabel("")
        self.status.setObjectName("mtCaption")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.status.setMinimumHeight(0)
        self.status.setTextFormat(Qt.PlainText)
        lay.addWidget(self.status)
        self._sync_detail()

    def _link(self, text, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("mtLink")
        b.setCursor(Qt.PointingHandCursor)
        b.setFlat(True)
        b.clicked.connect(slot)
        return b

    # ---------------------------------------------------------- 主题
    def apply_theme(self, tokens: dict) -> None:
        self.tok = resolve_tokens(tokens or None)
        self.grid.tok = self.tok
        self.setStyleSheet(self._qss(self.tok))
        self.grid.update()
        self.preview.update()

    @staticmethod
    def _qss(t: dict) -> str:
        return f"""
QWidget#materialTab {{ background: transparent; color: {t['text']}; }}
QFrame#mtIsland {{ background: {t['surface']}; border-radius: 16px;
    border: 1px solid {t['surface_border'] if not t['floating_cards'] else 'transparent'}; }}
QLabel {{ background: transparent; color: {t['text']}; font-size: 13px; }}
QLabel#mtTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#mtSub, QLabel#mtMeta, QLabel#mtFoot, QLabel#mtCaption, QLabel#mtKey {{
    color: {t['text_muted']}; font-size: 12px; }}
QLabel#mtFoot {{ padding: 10px 6px; border-top: 1px solid {t['surface_border']}; line-height: 160%; }}
QLabel#mtIslandTitle {{ font-size: 14px; font-weight: 700; }}
QLabel#mtDetName {{ font-size: 15px; font-weight: 700; }}
QLabel#mtKv {{ color: {t['text']}; font-size: 12.5px; }}
QLabel#mtPill {{ background: {t['field']}; color: {t['text_muted']}; border-radius: 11px;
    padding: 3px 10px; font-size: 12px; }}
QLabel#mtCatPill {{ background: {t['accent_soft']}; color: {t['accent']}; border-radius: 11px;
    padding: 2px 10px; font-size: 12px; }}
QLabel#mtEmptyTitle {{ font-size: 15px; font-weight: 700; }}
QLabel#mtEmptyTip {{ color: {t['text_muted']}; font-size: 13px; }}
QLabel#mtDrop {{ background: rgba(10,89,247,0.06); border: 2px dashed {t['accent']}; border-radius: 14px;
    color: {t['accent']}; font-weight: 600; font-size: 14px; }}
QFrame#mtNotice {{ background: {t['accent_soft']}; border-radius: 10px; }}
QFrame#mtNotice[warn="true"] {{ background: {t['warn_soft']}; }}
QLabel#mtNoticeText {{ color: {t['text']}; font-size: 12.5px; }}
QFrame#mtWarn {{ background: {t['warn_soft']}; border-radius: 10px; }}
QLabel#mtWarnText {{ color: {t['warn']}; font-size: 12.5px; }}
QLineEdit#mtSearch {{ background: {t['field']}; border: none; border-radius: 10px; padding: 7px 10px;
    color: {t['text']}; font-size: 13px; }}
QLineEdit#mtSearch:focus {{ border: 1.5px solid {t['accent']}; }}
QPushButton {{ font-size: 13px; }}
QPushButton#mtPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    border-radius: 18px; padding: 7px 18px; font-weight: 600; }}
QPushButton#mtPrimary:hover {{ background: {t['accent_hover']}; }}
QPushButton#mtPrimary:disabled {{ background: {t['field']}; color: {t['text_faint']}; }}
QPushButton#mtSecondary {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['surface_border']};
    border-radius: 18px; padding: 6px 16px; }}
QPushButton#mtSecondary:hover {{ background: {t['surface_hover']}; }}
QPushButton#mtSecondary:disabled {{ color: {t['text_faint']}; }}
QPushButton#mtGhost {{ background: transparent; color: {t['text_muted']}; border: none; border-radius: 14px;
    padding: 5px 12px; }}
QPushButton#mtGhost:hover {{ background: {t['surface_hover']}; color: {t['text']}; }}
QPushButton#mtGhost:disabled {{ color: {t['text_faint']}; }}
QPushButton#mtLink {{ background: transparent; border: none; color: {t['accent']}; padding: 0; font-size: 12px;
    text-align: left; }}
QPushButton#mtLink:hover {{ text-decoration: underline; }}
QPushButton#mtSeg {{ background: {t['field']}; color: {t['text_muted']}; border: none; padding: 5px 12px;
    font-size: 12px; }}
QPushButton#mtSeg:checked {{ background: {t['surface']}; color: {t['text']}; font-weight: 600;
    border: 1px solid {t['surface_border']}; border-radius: 8px; }}
QPushButton#mtCat {{ background: transparent; border: none; border-radius: 8px; text-align: left;
    padding: 0 10px; color: {t['text']}; font-size: 13px; }}
QPushButton#mtCat:hover {{ background: {t['surface_hover']}; }}
QPushButton#mtCat:checked {{ background: {t['accent_soft']}; color: {t['accent']}; font-weight: 600; }}
QPushButton#mtGroup {{ background: transparent; border: none; text-align: left; padding: 4px 10px 0 10px;
    color: {t['text_muted']}; font-size: 12px; }}
QScrollArea#mtCatScroll, QScrollArea#mtCatScroll > QWidget > QWidget {{ background: transparent; }}
QProgressBar#mtBar {{ background: {t['field']}; border: none; border-radius: 2px; max-height: 4px; }}
QProgressBar#mtBar::chunk {{ background: {t['accent']}; border-radius: 2px; }}
"""

    # ---------------------------------------------------------- 合同方法
    def refresh_library(self) -> None:
        """后台重读团队包 / 导入包 / 我的收藏；不阻塞界面。"""
        if self._closing:
            return
        if self._center is not None:
            try:
                self._center.check_latest("materials")
            except Exception:  # noqa: BLE001
                log.debug("check_latest unavailable", exc_info=True)
        self._load_gen += 1
        gen = self._load_gen
        self._run(self._load, lambda data, g=gen: self._loaded_done(data, g), "读取素材库", op=False)

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def import_material_file(self, path: str) -> None:
        path = str(path)
        if not path.lower().endswith((".pptx", ".zip")):
            self.toastRequested.emit("请选择素材包 .pptx 或离线组件 .zip", "warn")
            return

        def done(result):
            if result["kind"] == "team":
                msg = f"团队素材已更新到 v{result['version']} · {result['items']} 条"
            else:
                msg = f"已导入素材包 · {result['items']} 个素材（只读，可收藏一份到我的）"
            self._toast(msg, "ok")
            self.refresh_library()
        self._run(lambda: team_materials.install_from_file(Path(path)), done, "导入素材包")

    def collect_from_clipboard(self) -> None:
        def done(item):
            self._toast(f"已收藏：{item.name} → 我的收藏", "ok")
            self.refresh_library()
        self._run(lambda: self.library.capture("", ""), done, "从剪贴板收藏")

    # ---------------------------------------------------------- 读取
    def _load(self) -> dict:
        team = team_materials.team_reader()
        imported = team_materials.imported_readers()
        legacy_ids = set()
        try:
            legacy_ids = set(self.library.pack_record(team_materials.PACK).get("ids", []))
        except (OSError, ValueError):
            pass
        mine = [m for m in self.library.list() if m.id not in legacy_ids]
        personal = 0
        for m in mine:
            for f in (self.library.folder(m.id)).glob("*"):
                try:
                    personal += f.stat().st_size
                except OSError:
                    pass
        return {"team": team, "imported": imported, "mine": mine, "personal_bytes": personal}

    def _loaded_done(self, data: dict, gen: int | None = None):
        if gen is not None and gen != self._load_gen:
            for reader in [data.get("team"), *(data.get("imported") or [])]:
                if reader is not None and reader is not self._team_reader and reader not in self._imported_readers:
                    reader.close()
            return
        for reader in [self._team_reader, *self._imported_readers]:
            if reader is not None and reader is not data["team"] and reader not in data["imported"]:
                reader.close()
        self._team_reader = data["team"]
        self._imported_readers = data["imported"]
        self._personal_bytes = data["personal_bytes"]
        entries: list[Entry] = []
        if self._team_reader is not None:
            for x in self._team_reader.items:
                entries.append(Entry(f"{x.source}:{x.id}", x.name, x.category, x.source, x, self._team_reader))
        for reader in self._imported_readers:
            for x in reader.items:
                entries.append(Entry(f"imported:{reader.folder.name}:{x.id}", x.name, x.category, "imported",
                                     x, reader))
        for m in data["mine"]:
            entries.append(Entry(f"mine:{m.id}", m.name, m.category, "mine", m, None))
        self._all = entries
        self._loaded = True
        self.cache.clear()
        self._decode_pending.clear()
        self._build_categories()
        self._refresh_status()
        self._apply_filter()

    # ---------------------------------------------------------- 左栏分类
    def _build_categories(self):
        while self.cat_layout.count():
            item = self.cat_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._cat_buttons.clear()
        counts: dict[str, int] = {}
        for e in self._all:
            if e.source != "mine" and e.category:
                counts[e.category] = counts.get(e.category, 0) + 1
        mine_n = sum(1 for e in self._all if e.source == "mine")
        narrow = self.width() and self.width() < NARROW_WIDTH
        short = 0 < self.height() < 640
        h = 26 if short else (28 if narrow else 30)
        fixed_h = 28 if short else 32

        def cat_button(key, text, n, height=32):
            b = QPushButton()
            b.setObjectName("mtCat")
            b.setCheckable(True)
            b.setFixedHeight(height)
            row = QHBoxLayout(b)
            row.setContentsMargins(10, 0, 10, 0)
            name = QLabel(text)
            name.setAttribute(Qt.WA_TransparentForMouseEvents)
            num = QLabel(str(n))
            num.setObjectName("mtMeta")
            num.setAttribute(Qt.WA_TransparentForMouseEvents)
            row.addWidget(name, 1)
            row.addWidget(num)
            b.clicked.connect(lambda _=False, k=key: self._pick_category(k))
            self._cat_buttons[key] = b
            self.cat_layout.addWidget(b)
            return b

        cat_button("@all", "全部", len(self._all), fixed_h)
        cat_button("@mine", "☆ 我的收藏", mine_n, fixed_h)
        self._auto_collapse(counts, h)
        for group in mat.GROUPS:
            cats = sorted((c for c in counts if mat.category_group(c) == group), key=lambda c: -counts[c])
            if not cats:
                continue
            total = sum(counts[c] for c in cats)
            arrow = "›" if group in self._collapsed else "⌄"
            g = QPushButton(f"{arrow}  {group}    {total}")
            g.setObjectName("mtGroup")
            g.setFixedHeight(26 if short else 32)
            g.clicked.connect(lambda _=False, k=group: self._toggle_group(k))
            self.cat_layout.addWidget(g)
            if group in self._collapsed:
                continue
            for c in cats:
                cat_button(c, mat.short_category(c), counts[c], h)
        self.cat_layout.addStretch(1)
        self._sync_left()

    def _toggle_group(self, group: str):
        if group in self._collapsed and self._accordion:
            # 窄窗放不下全部展开：展开一组时收起其他组（手风琴），左栏始终不出滚动条
            self._collapsed = set(mat.GROUPS) - {group}
        else:
            self._collapsed ^= {group}
        self._user_collapsed = True
        self._build_categories()

    def _auto_collapse(self, counts: dict, row_h: int):
        groups = {g: sum(1 for c in counts if mat.category_group(c) == g) for g in mat.GROUPS}
        present = [g for g in mat.GROUPS if groups[g]]
        available = self.left.height() - self.left_foot.sizeHint().height() - 16
        fixed = 29 if row_h <= 26 else 33
        need_all = 2 * fixed + len(present) * (27 if row_h <= 26 else 33) + sum(groups.values()) * (row_h + 1)
        self._accordion = available > 0 and need_all > available
        if not self._accordion:
            if not self._user_collapsed:
                self._collapsed = set()
            return
        active = mat.category_group(self._cat) if self._cat else (present[0] if present else "")
        expanded = [g for g in present if g not in self._collapsed]
        if len(expanded) != 1 or (self._cat and active not in expanded):
            self._collapsed = set(present) - {active}

    def _pick_category(self, key: str):
        if key == "@mine":
            self._cat = ""
            self._set_source("mine")
            return
        if key == "@all":
            self._cat = ""
            self._set_source("all")
            return
        self._cat = key
        if self._src == "mine":
            self._src = "all"
            self.src_buttons["all"].setChecked(True)
        if key not in self._cat_buttons:      # 手风琴收起的组：展开它所在的组
            self._collapsed.discard(mat.category_group(key))
            self._build_categories()
        self._apply_filter()

    def _sync_left(self):
        current = "@mine" if self._src == "mine" else (self._cat or "@all")
        for key, b in self._cat_buttons.items():
            b.setChecked(key == current)

    def _set_source(self, src: str):
        self._src = src
        self.src_buttons[src].setChecked(True)
        if src == "mine":
            self._cat = ""
        self._apply_filter()

    def _set_view(self, mode: str):
        self.view_buttons[mode].setChecked(True)
        self.grid.list_mode = mode == "list"
        self.grid.set_entries(self._view, self._selected_key())
        self._decode_visible()

    # ---------------------------------------------------------- 过滤与分页
    def _apply_filter(self):
        tokens = mat.search_tokens(self.search.text())
        keep = self._selected_key()
        view = []
        for e in self._all:
            if self._src == "mine" and e.source != "mine":
                continue
            if self._src == "team" and e.source == "mine":
                continue
            if self._cat and e.category != self._cat:
                continue
            if not mat.matches(tokens, e.name, e.category):
                continue
            view.append(e)
        self._view = view
        self.grid.set_entries(view, keep)
        self.count_label.setText(f"{len(view)} 个素材")
        self.export_btn.setVisible(self._src == "mine" and bool(view))
        self._sync_left()
        self._sync_empty()
        if self.grid.selected < 0 and view:
            self.grid.select(0)
        elif not view:
            self._on_selected(-1)
        self._decode_visible()

    def _sync_empty(self):
        if not self._loaded:
            self._show_empty("正在读取素材…", "", None, None)
            return
        if self._view:
            self.stack.setCurrentWidget(self.grid)
            return
        if self._src == "mine" and not any(e.source == "mine" for e in self._all):
            self._show_empty("我的收藏还是空的",
                             "在 PPT 里选中形状按 Ctrl+C，再点「从剪贴板收藏」。也可以把同事发来的素材包拖进来。",
                             ("从剪贴板收藏", self.collect_from_clipboard), ("导入素材包…", self._pick_import))
        elif not self._all:
            self._show_empty("还没有团队素材",
                             "完整团队库可从公司内源一键获取；也可以导入同事发来的素材包（.zip / .pptx）。",
                             ("获取完整库", self._on_fetch), ("导入素材包…", self._pick_import))
        else:
            self._show_empty("没有符合条件的素材", f"换个词，或点「全部」看所有 {len(self._all)} 个素材。",
                             ("清空搜索，看全部", self._clear_filters), None)

    def _show_empty(self, title, tip, primary, secondary):
        self.empty_title.setText(title)
        self.empty_tip.setText(tip)
        for button, spec in ((self.empty_primary, primary), (self.empty_secondary, secondary)):
            try:
                button.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            button.setVisible(spec is not None)
            if spec is not None:
                button.setText(spec[0])
                button.clicked.connect(spec[1])
        self.stack.setCurrentWidget(self.empty)

    def _clear_filters(self):
        self.search.clear()
        self._cat = ""
        self._set_source("all")

    def _on_paging(self, page: int, pages: int, per: int):
        self.page_label.setText(f"{page + 1} / {pages}")
        self.prev_btn.setEnabled(page > 0)
        self.next_btn.setEnabled(page + 1 < pages)
        self.pager_hint.setText("双击 = 复制形状")
        self._decode_visible()

    def _decode_visible(self):
        if self._closing:
            return
        gen = self._load_gen
        visible = [self._view[i] for i in self.grid.visible_range()]
        if 0 <= self.grid.selected < len(self._view):
            visible.append(self._view[self.grid.selected])
        self.cache.set_pinned({entry.key for entry in visible})
        # Paging, selection and resize can request the same viewport before a
        # worker returns. Each key gets only one decode per library generation.
        todo = list({entry.key: entry for entry in visible
                     if self.cache.needs(entry)
                     and (gen, entry.key) not in self._decode_pending}.values())
        if not todo:
            return
        pending = {(gen, entry.key) for entry in todo}
        self._decode_pending.update(pending)
        task = BackgroundTask(lambda: self.cache.decode(todo), "material-thumbs", self)
        self._tasks.append(task)

        def done(decoded):
            self._decode_pending.difference_update(pending)
            if self._closing or gen != self._load_gen:
                return
            if isinstance(decoded, dict):
                self.cache.store(decoded)
                try:
                    self.grid.update()
                    self._sync_detail()
                except RuntimeError:   # 页面已关闭
                    pass
        task.done.connect(done)
        task.finished.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.start()

    # ---------------------------------------------------------- 右栏
    def _selected_key(self) -> str:
        e = self._current()
        return e.key if e else ""

    def _current(self) -> Entry | None:
        i = self.grid.selected
        return self._view[i] if 0 <= i < len(self._view) else None

    def _on_selected(self, _index: int):
        self._show_warn(False)
        self._sync_detail()

    def _grid_context_menu(self, pos: QPoint):
        i = self.grid.index_at(pos)
        if i >= 0:
            self.grid.select(i)
        e = self._current()
        if e is None:
            return
        from . import owner_actions
        menu = QMenu(self)
        act_ins = menu.addAction("插入到当前 PPT", self._insert)
        act_copy = menu.addAction("复制形状（回 PPT 粘贴）\tCtrl+C", self._copy_shape)
        act_img = menu.addAction("复制为图片", self._copy_image)
        for act in (act_ins, act_copy, act_img):
            act.setEnabled(not self._busy_op)
        menu.addSeparator()
        if e.source == "mine":
            act_edit = menu.addAction("改名 / 分类\tF2", self._edit_mine)
            act_del = menu.addAction("移出收藏\tDel", self._delete_mine)
            act_edit.setEnabled(not self._busy_op)
            act_del.setEnabled(not self._busy_op)
        else:
            act_save = menu.addAction("收藏一份到我的", self._save_to_mine)
            act_save.setEnabled(not self._busy_op)
            if e.source == "imported":
                act_rm = menu.addAction("移除这个导入包", self._remove_imported)
                act_rm.setEnabled(not self._busy_op)
        self._last_menu = menu
        owner_actions.popup(menu, self.grid.mapToGlobal(pos))

    def _sync_detail(self):
        e = self._current()
        has = e is not None
        for w in (self.insert_btn, self.copy_btn):
            w.setEnabled(has and not self._busy_op)
        self.copy_image_link.setVisible(has)
        self.save_mine_link.setVisible(has and e.source != "mine")
        self.edit_link.setVisible(has and e.source == "mine")
        self.delete_link.setVisible(has and e.source == "mine")
        self.remove_import_link.setVisible(has and e.source == "imported")
        for w in (self.save_mine_link, self.edit_link, self.delete_link, self.remove_import_link,
                  self.copy_image_link):
            w.setEnabled(not self._busy_op)
        if not has:
            self.preview.set_image(None, "")
            self.det_name.setText("选一个素材看大图")
            self.det_cat.setText("")
            self.det_pill.hide()
            self.det_source.setText("")
            self.det_kind.setText("")
            return
        self.preview.set_image(self.cache.get(e), e.name)
        self.det_name.setText(e.name)
        cat = e.category or "未分类"
        self.det_cat.setText(mat.category_group(e.category) if e.source != "mine" else "我的收藏")
        self.det_pill.setText(mat.short_category(cat))
        self.det_pill.setVisible(not self._warn_on)
        if e.source in ("team", "builtin"):
            version = getattr(e.reader, "version", "") or ""
            self.det_source.setText(f"{SOURCE_LABEL[e.source]} v{version}（只读）" if version
                                    else f"{SOURCE_LABEL[e.source]}（只读）")
        elif e.source == "imported":
            self.det_source.setText("导入的素材包（只读）")
        else:
            self.det_source.setText("我的收藏 · 可改名、移出")
        kind = getattr(e.item, "kind", "") if e.source != "mine" else ""
        self.det_kind.setText(KIND_LABEL.get(kind, KIND_LABEL[""]))

    # ---------------------------------------------------------- 状态胶囊 / 获取完整库
    def _refresh_status(self):
        reader = self._team_reader
        center = self._center
        snap = {}
        busy = False
        if center is not None:
            try:
                snap = center.snapshot("materials")
                busy = center.busy("materials")
            except Exception:  # noqa: BLE001
                snap, busy = {}, False
        source = getattr(reader, "source", "")
        n = len(reader.items) if reader is not None else 0
        version = getattr(reader, "version", "") or ""
        vtext = f" v{version}" if version else ""
        self.fetch_btn.hide()
        if reader is None:
            pill = "团队素材 · 未获取"
            self.subtitle.setText("完整团队库可从公司内源一键获取（约 5 MB）")
        elif source == "builtin":
            pill = f"团队素材{vtext} · 内置精选 {n} 条"
            self.subtitle.setText("完整团队库可从公司内源获取（约 5 MB）")
            self.fetch_btn.setText("获取完整库")
            self.fetch_btn.show()
        else:
            pill = f"团队素材{vtext} · {n} 条 · 离线可用"
            self.subtitle.setText("团队素材只读 · 我的收藏可改名、移出、导出")
            latest = ""
            try:
                latest = center.upgradable("materials") if center is not None else ""
            except Exception:  # noqa: BLE001
                latest = ""
            if latest:
                self.fetch_btn.setText(f"更新团队素材（v{latest}）")
                self.fetch_btn.show()
        self.notice.hide()
        self.notice.setProperty("warn", False)
        phase = snap.get("phase", "")
        if busy:
            st = snap.get("status") or {}
            got, total = st.get("received", 0), st.get("total") or snap.get("bytes") or 0
            pill = "正在获取完整库"
            if phase == "checking":
                text = "正在连接公司内源…下面的素材照常可用"
            elif phase == "installing":
                text = "正在校验并切换到新版本…下面的素材照常可用"
            else:
                text = (f"正在从公司内源获取完整库 {got / 1048576:.1f} / {total / 1048576:.1f} MB；下面的素材照常可用"
                        if total else "正在从公司内源获取完整库…下面的素材照常可用")
            self.notice_bar.setValue(int(got * 1000 / total) if total else 0)
            self._notice(text, warn=False, bar=True)
        elif phase == "error":
            pill = pill.replace(" · 离线可用", "") + " · 离线可用 · 内源暂时连不上"
            self._notice(f"公司内源暂时连不上，现有 {n} 条照常可用。在公司网络里稍后重试；"
                         "也可以导入同事发来的素材包（.zip / .pptx）。", warn=True, bar=False, actions=True)
        elif self._personal_bytes > PERSONAL_SOFT_LIMIT:
            self._notice(f"我的收藏已占 {self._personal_bytes / 1048576:.0f} MB，超过 200 MB 建议值：可以把不用的移出"
                         "（移出的在素材库回收区，可找回）。不会自动删除，也不影响继续收藏。", warn=True, bar=False)
        self.pill.setText(pill)
        summary = pill if reader is not None else "团队素材未获取"
        self.statusTextChanged.emit(summary)

    def _notice(self, text, *, warn: bool, bar: bool, actions: bool = False):
        self.notice_text.setText(text)
        self.notice.setProperty("warn", warn)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice_bar.setVisible(bar)
        self.notice_import.setVisible(actions)
        self.notice_retry.setVisible(actions)
        self.notice.show()

    def _on_fetch(self):
        if self._center is None:
            self._toast("下载中心不可用：可以导入同事发来的素材包（.zip / .pptx）。", "warn")
            return
        try:
            if not self._center.busy("materials"):
                self._center.start("materials")
        except Exception as exc:  # noqa: BLE001
            log.exception("start materials download failed")
            self._toast(f"获取完整库失败：{exc}", "err")
        self._refresh_status()

    def _on_center_state(self, key):
        if key == "materials":
            try:
                self._refresh_status()
            except RuntimeError:   # 控件已销毁
                pass

    def _on_center_installed(self, key):
        if key == "materials":
            try:
                self._toast("团队素材已更新，离线可用。", "ok")
                self.refresh_library()
            except RuntimeError:
                pass

    # ---------------------------------------------------------- 动作
    def _toast(self, message: str, level: str = "info"):
        self.status.setText(message)
        self.toastRequested.emit(message, level)

    def _run(self, fn, done, label, *, op: bool = True):
        if op and self._busy_op:
            self._toast("上一个操作还在进行，稍等一下。", "warn")
            return
        if op:
            self._busy_op = True
            self._op_entry = self._current()
            self._sync_detail()
            self.status.setText(f"正在{label}…")

        def work():
            try:
                return {"value": fn()}
            except ppt_transfer.MaterialTransferError as exc:
                return {"code": exc.code, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("material operation failed: %s", label)
                return {"error": str(exc) or exc.__class__.__name__}

        def complete(result):
            if self._closing:
                return
            if op:
                self._busy_op = False
            try:
                self._sync_detail()
                if not isinstance(result, dict) or ("error" in result and "value" not in result):
                    error = result.get("error") if isinstance(result, dict) else "任务未完成，请重试。"
                    code = result.get("code", "") if isinstance(result, dict) else ""
                    self._failed(label, error, code)
                else:
                    if op:
                        self.status.setText("")
                    done(result["value"])
            except RuntimeError:
                pass

        task = BackgroundTask(work, "materials", self)
        self._tasks.append(task)
        task.done.connect(complete)
        task.finished.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.start()

    def _failed(self, label: str, error: str, code: str):
        """失败一律在详情栏就地写清原因和下一步（不只闪一下 toast），并给兜底按钮。"""
        self._last_error_code = code
        if code:
            reason = error or ppt_transfer.MATERIAL_ERRORS[code]
        else:
            reason = f"{label}没完成：{error or '原因不明'}。"
        lines = [reason]
        is_copy = label == "复制形状"
        usable = code != "pack_missing"
        if is_copy and usable:
            if self._copy_preview_image():
                lines.append("已先复制成图片（不可编辑），回 PPT 按 Ctrl+V 可先用。")
            else:
                lines.append("可以处理后再试，或用 PPT 打开素材再复制。")
        self.warn_text.setText("\n".join(lines))
        self.warn_copy.setText("再试复制形状" if is_copy else "改为复制形状")
        self.warn_copy.setVisible(usable and (is_copy or (label == "插入到当前 PPT" and code in _OFFER_COPY)))
        self.warn_open.setVisible(usable and (is_copy or (label == "插入到当前 PPT" and code in _OFFER_OPEN)))
        self._show_warn(True)
        self.status.setText("")
        self.toastRequested.emit(reason if code else f"{label}失败：{error}", "warn" if code else "err")

    def _show_warn(self, on: bool):
        """失败提示占地方：显示时收起来源 / 类型和说明文字，保证主窗 1280×672 下提示与按钮完整可见。"""
        self._warn_on = on
        self._fit_detail()                 # 先收起别的，再显示提示，避免瞬间撑高窗口
        self.warn.setVisible(on)

    def _fit_detail(self):
        """提示显示期间收起分类胶囊、来源类型、次要链接和说明（预览图会自动缩小），
        矮窗（主窗 1280×672 内嵌 520 高）下提示与两个主按钮也能一屏放下。"""
        on = getattr(self, "_warn_on", False)
        for w in (self.det_meta, self.links_row):
            w.setVisible(not on)
        self.det_pill.setVisible(not on and self._current() is not None)
        self.caption.setVisible(not on and self.height() >= 640)

    def _copy_preview_image(self) -> bool:
        e = getattr(self, "_op_entry", None) or self._current()
        image = self.cache.get(e) if e else None
        if image is None or image.isNull():
            return False
        QGuiApplication.clipboard().setImage(image)
        return True

    def _open_in_powerpoint(self):
        e = getattr(self, "_op_entry", None) or self._current()
        if e is None:
            return
        path, page = self._target(e)
        name = e.name
        self._show_warn(False)

        def work():
            target = ppt_transfer.export_material_deck(path, page, name)
            try:
                os.startfile(str(target))  # noqa: S606  用户点的按钮，系统默认程序打开
            except OSError as exc:
                raise RuntimeError(f"这台电脑没有能打开 .pptx 的程序（{exc.strerror or exc}）；"
                                   f"单页文件已存到 {target}") from exc
            return target
        self._run(work, lambda target: self._toast(
            f"已打开单页素材「{name}」：在里面选中形状 Ctrl+C，再回目标 PPT 按 Ctrl+V（可编辑）。", "ok"),
            "用 PowerPoint 打开这个素材")

    def _target(self, e: Entry) -> tuple[Path, int]:
        if e.source == "mine":
            return self.library.folder(e.item.id) / "material.pptx", 1
        return Path(e.item.pack_path), int(e.item.page)

    def _insert(self):
        e = self._current()
        if e is None:
            return
        path, page = self._target(e)
        self._show_warn(False)

        def done(info):
            self._toast(f"已插入到「{info['presentation']}」第 {info['slide']} 页"
                        f"（{info['shapes']} 个形状，可编辑，未自动保存）", "ok")
        self._run(lambda: ppt_transfer.call_with_timeout(
            lambda: ppt_transfer.insert_material(path, page), 60), done, "插入到当前 PPT")

    def _copy_shape(self):
        e = self._current()
        if e is None:
            return
        self._show_warn(False)
        path, page = self._target(e)
        if e.source == "mine":
            fn = lambda: self.library.copy(e.item.id)  # noqa: E731
        else:
            fn = lambda: ppt_transfer.copy_material(path, page)  # noqa: E731
        self._run(lambda: ppt_transfer.call_with_timeout(fn, 60),
                  lambda _: self._toast("已复制形状，回 PPT 按 Ctrl+V。", "ok"), "复制形状")

    def _copy_image(self):
        e = self._current()
        image = self.cache.get(e) if e else None
        if image is None:
            self._toast("这个素材没有预渲染图，可以用「复制形状」。", "warn")
            return
        QGuiApplication.clipboard().setImage(image)
        self._toast("已复制为 PNG 图片（透明底）。", "ok")

    def _save_to_mine(self):
        e = self._current()
        if e is None or e.source == "mine":
            return
        path, page = self._target(e)
        name, category = e.name, e.category

        def work():
            ppt_transfer.copy_material(path, page)
            return self.library.capture(name, category)
        self._run(lambda: ppt_transfer.call_with_timeout(work, 120),
                  lambda item: (self._toast(f"已收藏一份：{item.name} → 我的收藏", "ok"), self.refresh_library()),
                  "收藏到我的")

    def _edit_mine(self):
        e = self._current()
        if e is None or e.source != "mine" or self._busy_op:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑素材信息")
        layout = QFormLayout(dialog)
        name, category = QLineEdit(e.item.name), QLineEdit(e.item.category)
        name.setMaxLength(120)
        category.setMaxLength(80)
        layout.addRow("名称", name)
        layout.addRow("分类", category)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            new_name, new_cat = name.text(), category.text()
            self._run(lambda: self.library.rename(e.item.id, new_name, new_cat),
                      lambda _: (self._toast("素材信息已更新。", "ok"), self.refresh_library()), "保存素材信息")

    def _delete_mine(self):
        e = self._current()
        if e is None or e.source != "mine" or self._busy_op:
            return
        from . import owner_actions
        if owner_actions.ask(self, "移出收藏", f"把「{e.name}」移出我的收藏？\n移出的素材留在素材库回收区，可找回。",
                             "移出", "取消"):
            self._run(lambda: self.library.delete([e.item.id]),
                      lambda _: (self._toast("已移出我的收藏。", "ok"), self.refresh_library()), "移出收藏")

    def _remove_imported(self):
        e = self._current()
        if e is None or e.source != "imported" or self._busy_op:
            return
        from . import owner_actions
        if owner_actions.ask(self, "移除导入的素材包", "移除这个导入的素材包？不影响团队素材和我的收藏。", "移除", "取消"):
            reader = e.reader
            self._run(lambda: team_materials.remove_imported(reader),
                      lambda _: (self._toast("已移除导入的素材包。", "ok"), self.refresh_library()), "移除素材包")

    def _export_mine(self):
        ids = [e.item.id for e in self._view if e.source == "mine"]
        if not ids:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出收藏为素材包", "我的素材包.pptx", "PPTX 素材包 (*.pptx)")
        if not path:
            return
        if not path.lower().endswith(".pptx"):
            path += ".pptx"
        self._run(lambda: self.library.export_pack(ids[:mat.MAX_PACK_ITEMS], Path(path)),
                  lambda _: self._toast(f"已导出 {min(len(ids), mat.MAX_PACK_ITEMS)} 个素材：{path}", "ok"),
                  "导出素材包")

    def _pick_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入素材包", "", "素材包 (*.pptx *.zip)")
        if path:
            self.import_material_file(path)

    # ---------------------------------------------------------- 拖放导入
    def _drop_paths(self, mime) -> list[str]:
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and u.toLocalFile().lower().endswith((".pptx", ".zip"))]

    def dragEnterEvent(self, e):  # noqa: N802
        if e.source() is None and self._drop_paths(e.mimeData()):
            self._drop_hint.setGeometry(self.mid.rect().adjusted(8, 8, -8, -8))
            self._drop_hint.raise_()
            self._drop_hint.show()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):  # noqa: N802
        self._drop_hint.hide()

    def dropEvent(self, e):  # noqa: N802
        self._drop_hint.hide()
        paths = self._drop_paths(e.mimeData())
        if paths:
            e.acceptProposedAction()
            self.import_material_file(paths[0])

    # ---------------------------------------------------------- 尺寸
    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        narrow = self.width() < NARROW_WIDTH
        self.left.setFixedWidth(176 if narrow else 196)
        self.right.setFixedWidth(270 if narrow else 320)
        self.search.setMaximumWidth(240 if narrow else 380)
        self.pager_hint.setVisible(not narrow)
        # 矮窗：预览图按剩余高度缩（右栏其余内容约 360 px），说明文字收起
        short = self.height() < 640
        self._fit_detail()
        self.left_foot.setVisible(not short)
        if self._loaded and not self._closing:
            self._relayout_timer.start()

    # ---------------------------------------------------------- 嵌入主窗的补充接口
    def set_header_compact(self, compact: bool = True) -> None:
        """主窗页头已显示标题和状态时调用：只保留右侧操作按钮（m1 MF-4）。"""
        for w in (self.title, self.subtitle, self.pill):
            w.setVisible(not compact)

    def shutdown(self) -> None:
        """主窗关闭时调用（嵌入时收不到 closeEvent）；可重复调用（m1 MF-2）。

        停止定时器、断开下载中心回调、等后台任务跑完（它们不可中断，最多各等 5 s），
        再关掉素材包读取器。之后的回调一律不再碰界面。
        """
        if self._closing:
            return
        self._closing = True
        for timer in (self._search_timer, self._relayout_timer):
            timer.stop()
        if self._center is not None:
            for signal, slot in ((self._center.state_changed, self._on_center_state),
                                 (self._center.installed, self._on_center_installed)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        for task in list(self._tasks):
            try:
                task.wait(5000)
            except RuntimeError:
                pass
        for reader in [self._team_reader, *self._imported_readers]:
            if reader is not None:
                reader.close()

    def closeEvent(self, e):  # noqa: N802
        self.shutdown()
        super().closeEvent(e)


def sizes_for_tests(widget: MaterialTabWidget) -> dict:
    """测试用：三栏实际宽度与网格每页张数。"""
    return {"left": widget.left.width(), "mid": widget.mid.width(), "right": widget.right.width(),
            "per_page": widget.grid.per_page(), "layout": widget.grid.layout_info()}


__all__ = ["MaterialTabWidget", "MaterialGrid", "PreviewView", "resolve_tokens", "Entry"]
