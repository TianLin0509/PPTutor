"""涓荤獥鍙ｏ細鍙屼富棰?+ 绮捐嚧缁撴灉椤癸紙鐒︾偣鍙屾€?鍗婇€忔槑楂樹寒/瀛楅噸灞傜骇锛? P0 浜や簰
锛堝嵆鏃舵悳绱?/ 鍏ㄩ敭鐩樺鑸?/ 鍛戒腑椤电缉鐣ュ浘鏉?/ 绱㈠紩杩涘害鏉★級銆?

鍙祴璇曟€э細conn 涓?render_worker 鍙敞鍏ワ紱do_index=False 鏃朵笉鍚姩纾佺洏绱㈠紩銆?
"""
from __future__ import annotations

import datetime
import html
import logging
import os
import re
from collections import deque

import sys
import time

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPropertyAnimation, QSize, Qt, QStringListModel, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

try:
    import ctypes
    from ctypes import wintypes
    _WIN = sys.platform == "win32"
except Exception:  # noqa: BLE001
    _WIN = False

from .. import actions, db, history, indexer as indexer_mod, renderer as renderer_mod, search as search_mod, updater, __version__
from ..config import (
    DOCX_EXT, PDF_EXT, PPTX_EXT, PPT_EXTS, SUPPORTED_EXTS, db_path as cfg_db_path,
    enabled_index_exts as cfg_enabled_index_exts, ext_path,
    ensure_completed_index_feature_signature,
    get_completed_index_feature_signature,
    get_document_search_enabled, get_hotkey, get_smart_grouping_enabled, get_theme,
    index_feature_signature,
    is_first_run, mark_welcomed,
    set_completed_index_feature_signature,
    set_theme as cfg_set_theme, update_base_url,
)
from ..models import FileResult
from ..query_explain import explain_query, mode_label, suggestion_keys
from . import theme
from .bg_task import BackgroundTask
from .index_worker import IndexWorker
from .index_activity_bar import IndexActivityBar
from .live_indexer import LiveIndexer
from .path_helpers import ensure_pptx_suffix
from .render_worker import RenderWorker
from .result_utils import (
    facet_counts,
    facet_filter,
    group_by_time,
    sort_results as _sort_results,
    empty_suggestions as _empty_suggestions,  # noqa: F401  供测试从本模块 re-export
    time_bucket as _time_bucket,  # noqa: F401  供测试从本模块 re-export
)
from .search_worker import SearchWorker
from .detail_panel import DetailPanel
from .facet_panel import FacetPanel
from .aurora_bg import AuroraCentral
from .dashboard_view import DashboardView
from .update_ui import UpdateController

_log = logging.getLogger(__name__)


def _make_icon(draw, color: str = "#8A8A8A", size: int = 18) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), max(1.7, size * 0.075))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    draw(p)
    p.end()
    return QIcon(pm)


def _icon_search(color: str = "#8A8A8A", size: int = 18) -> QIcon:
    scale = size / 18
    return _make_icon(
        lambda p: (
            p.drawEllipse(round(3 * scale), round(3 * scale), round(8 * scale), round(8 * scale)),
            p.drawLine(round(10 * scale), round(10 * scale), round(15 * scale), round(15 * scale)),
        ),
        color,
        size,
    )


def _icon_folder(color: str = "#8A8A8A", size: int = 18) -> QIcon:
    scale = size / 18
    return _make_icon(
        lambda p: (
            p.drawLine(round(2 * scale), round(6 * scale), round(7 * scale), round(6 * scale)),
            p.drawLine(round(7 * scale), round(6 * scale), round(9 * scale), round(8 * scale)),
            p.drawLine(round(9 * scale), round(8 * scale), round(16 * scale), round(8 * scale)),
            p.drawRect(round(2 * scale), round(8 * scale), round(14 * scale), round(8 * scale)),
        ),
        color,
        size,
    )


def _icon_theme(color: str = "#8A8A8A", size: int = 18) -> QIcon:
    scale = size / 18
    return _make_icon(
        lambda p: (
            p.drawEllipse(round(3 * scale), round(3 * scale), round(12 * scale), round(12 * scale)),
            p.drawLine(round(9 * scale), round(3 * scale), round(9 * scale), round(15 * scale)),
        ),
        color,
        size,
    )


def _icon_settings(color: str = "#8A8A8A", size: int = 18) -> QIcon:
    """齿轮简化：圆心 + 四向短齿。"""
    scale = size / 18

    def _draw(p):
        p.drawEllipse(round(5.5 * scale), round(5.5 * scale), round(7 * scale), round(7 * scale))
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            p.drawLine(round((9 + dx * 5.2) * scale), round((9 + dy * 5.2) * scale),
                       round((9 + dx * 7.3) * scale), round((9 + dy * 7.3) * scale))

    return _make_icon(_draw, color, size)


def _icon_film(color: str = "#8A8A8A", size: int = 18) -> QIcon:
    """胶片框：外框 + 两侧齿孔轨。"""
    scale = size / 18
    return _make_icon(
        lambda p: (
            p.drawRect(round(2 * scale), round(4 * scale), round(14 * scale), round(10 * scale)),
            p.drawLine(round(5.5 * scale), round(4 * scale), round(5.5 * scale), round(14 * scale)),
            p.drawLine(round(12.5 * scale), round(4 * scale), round(12.5 * scale), round(14 * scale)),
        ),
        color,
        size,
    )


def _icon_clear() -> QIcon:
    return _make_icon(lambda p: (p.drawLine(5, 5, 13, 13), p.drawLine(13, 5, 5, 13)))


def _asset_path(name: str) -> str:
    """璧勬簮鏂囦欢璺緞锛歞ev=repo/assets锛宖rozen=_MEIPASS/assets锛坰pec 宸?bundle锛夈€?"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets", name)
    return os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets", name)


def _sqlite_file_path(conn) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None
        path = row["file"] if hasattr(row, "keys") else row[2]
        return path or None
    except Exception:  # noqa: BLE001
        return None


def _backend_supports(manager, name: str) -> bool:
    """Check lazy backend capabilities without running its factory on the UI thread."""
    supports = getattr(type(manager), "supports", None)
    if callable(supports):
        try:
            return bool(supports(manager, name))
        except Exception:  # noqa: BLE001 optional capability protocol
            return False
    return callable(getattr(manager, name, None))


def _backend_is_initialized(manager) -> bool:
    probe = getattr(type(manager), "is_initialized", None)
    if not callable(probe):
        return True
    try:
        return bool(probe(manager))
    except Exception:  # noqa: BLE001 diagnostics must stay non-blocking
        return False


def _scan_known_index_changes(
    conn_path: str,
    *,
    limit: int = 200,
    supported_exts: tuple[str, ...] | set[str] | None = None,
) -> dict:
    """Cheap startup reconciliation that never walks the disks.

    Only stat paths already present in the index. Changed and missing paths are
    handed to ``LiveIndexer`` later, so this worker performs no parsing and
    cannot monopolise a CPU core like the former 24-hour full-drive scan.
    """
    conn = db.connect(conn_path)
    now = time.time()
    candidates: list[str] = []
    pending_paths: list[str] = []
    try:
        allowed_exts = {
            ext.lower()
            for ext in (SUPPORTED_EXTS if supported_exts is None else supported_exts)
        }
        rows = [
            row for row in conn.execute(
                "SELECT path, ext, size, mtime, status, parse_failures, retry_after FROM files"
            ).fetchall()
            if str(row["ext"] or "").lower() in allowed_exts
        ]
        known_paths = {os.path.normcase(str(row["path"])) for row in rows}
        known_dirs = {os.path.dirname(str(row["path"])) for row in rows}
        for row in rows:
            path = str(row["path"])
            changed = False
            try:
                stat = os.stat(ext_path(path))
            except FileNotFoundError:
                # A missing path is ambiguous at startup: the file may be on a
                # detached/remapped drive. Live watcher deletes are definitive;
                # startup reconciliation deliberately preserves the searchable
                # row until the user runs a full rescan.
                continue
            except OSError:
                # Offline/cloud drives can make stat fail transiently. Treating
                # that as deletion would create a false-negative search result.
                continue
            else:
                if row["status"] == "pending":
                    pending_paths.append(path)
                    continue
                changed = (
                    int(stat.st_size) != int(row["size"])
                    or abs(float(stat.st_mtime) - float(row["mtime"])) > 1e-6
                )
                if not changed and row["status"] == "cloud_placeholder":
                    # Hydration can flip only file attributes while preserving
                    # logical size and mtime.
                    changed = not indexer_mod._is_cloud_placeholder(path, stat)
                elif not changed and row["status"] == "error":
                    changed = (
                        int(row["parse_failures"] or 0)
                        < indexer_mod.MAX_UNCHANGED_PARSE_FAILURES
                        and now >= float(row["retry_after"] or 0)
                    )
            if changed:
                candidates.append(path)

        # Discover offline-created siblings without recursing across disks.
        # This covers normal "saved a new deck next to existing work" while
        # keeping the daily check proportional to known folders.
        folders_checked = 0
        new_paths = 0
        for directory in sorted(known_dirs, key=str.casefold):
            if not directory:
                continue
            try:
                entries = os.scandir(ext_path(directory))
            except OSError:
                continue
            folders_checked += 1
            with entries:
                for entry in entries:
                    try:
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if not is_file or entry.name.startswith("~$"):
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in allowed_exts:
                        continue
                    path = os.path.join(directory, entry.name)
                    if os.path.normcase(path) in known_paths:
                        continue
                    candidates.append(path)
                    known_paths.add(os.path.normcase(path))
                    new_paths += 1

        ordered = sorted(set(candidates), key=str.casefold)
        cap = max(1, int(limit))
        cursor = db.meta_value(conn, db.META_KNOWN_RECONCILE_CURSOR, "")
        start = 0
        if cursor and ordered:
            if cursor in ordered:
                start = (ordered.index(cursor) + 1) % len(ordered)
            else:
                cursor_key = cursor.casefold()
                start = next(
                    (i for i, path in enumerate(ordered) if path.casefold() > cursor_key),
                    0,
                )
        rotated = ordered[start:] + ordered[:start]
        selected = rotated[:cap]
        remaining = max(0, len(ordered) - len(selected))
        # Advance the freshness marker only after a clean pass. When changes
        # are merely queued, keep it stale so a quick shutdown cannot lose
        # those paths for the next 24 hours.
        if not ordered and not pending_paths:
            db.set_meta(conn, db.META_LAST_KNOWN_RECONCILE_AT, str(now))
            db.delete_meta(conn, db.META_KNOWN_RECONCILE_CURSOR)
        elif selected:
            # Rotate capped batches across launches so a permanently failing
            # early path cannot starve later changes.
            db.set_meta(conn, db.META_KNOWN_RECONCILE_CURSOR, selected[-1])
        conn.commit()
        return {
            "checked": len(rows),
            "paths": selected,
            "pending_paths": sorted(set(pending_paths), key=str.casefold),
            "remaining": remaining,
            "folders_checked": folders_checked,
            "new_paths": new_paths,
        }
    finally:
        conn.close()


def _app_logo() -> QPixmap:
    """鍝佺墝 logo锛歅PTutor 鍚夌ゥ鐗╋紙瀛﹀＋甯?+ 鎼滅储/PPT锛夛紝鍔犺浇鎵撳寘鍐?assets/logo.png銆?"""
    pm = QPixmap(_asset_path("logo.png"))
    if not pm.isNull():
        img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
        corners = [
            img.pixelColor(0, 0),
            img.pixelColor(max(0, img.width() - 1), 0),
            img.pixelColor(0, max(0, img.height() - 1)),
            img.pixelColor(max(0, img.width() - 1), max(0, img.height() - 1)),
        ]
        # 当前 logo 已自带透明背景。旧代码仍逐像素走约 47 万次 Python/Qt
        # 边界调用，实测仅这一处就阻塞主线程约 0.84 秒。四角透明即可直接缩放。
        if all(base.alpha() <= 0 for base in corners):
            return pm.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if c.alpha() <= 0 or max(c.red(), c.green(), c.blue()) < 185:
                    continue
                if any(
                    base.alpha() > 0
                    and abs(c.red() - base.red()) <= 24
                    and abs(c.green() - base.green()) <= 24
                    and abs(c.blue() - base.blue()) <= 24
                    for base in corners
                ):
                    c.setAlpha(0)
                    img.setPixelColor(x, y, c)
        return QPixmap.fromImage(img).scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    fb = QPixmap(28, 28)
    fb.fill(Qt.transparent)
    return fb


def _load_theme() -> str:
    # 主题持久化集中在 config.ui.json（与热键等共用一个文件，合并写不互相清键）
    return get_theme()


def _save_theme(name: str) -> None:
    cfg_set_theme(name)


def _highlight(snippet: str, hlcss: str) -> str:
    """鎶婄墖娈甸噷鐨勩€愬懡涓€戣浆鎴愬崐閫忔槑搴曢珮浜紙涓嶅彉鑹蹭笉鍔犵矖锛夈€?"""
    s = html.escape(snippet)
    s = s.replace("\u3010", f'<span style="{hlcss}">').replace("\u3011", "</span>")
    return s


def _fmt_mtime(ts: float) -> str:
    """淇敼鏃堕棿锛氬悓骞?'MM-DD HH:MM'锛岃法骞?'YYYY-MM-DD'銆?"""
    try:
        dt = datetime.datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return ""
    if dt.year == datetime.datetime.now().year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def _fmt_size(n: int) -> str:
    """瀛楄妭鏁拌浆浜虹被鍙锛?2.3 MB' / '456 KB' / '18 B'銆?"""
    if not n or n <= 0:
        return ""
    f = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def _elide_middle(s: str, maxlen: int = 72) -> str:
    """璺緞杩囬暱鏃朵腑闂寸渷鐣ワ紝淇濈暀鐩樼涓庢枃浠跺悕涓ょ銆?"""
    if len(s) <= maxlen:
        return s
    head = (maxlen - 1) * 2 // 3
    tail = maxlen - 1 - head
    return s[:head] + "\u2026" + s[-tail:]


def _file_mime_for_path(path: str) -> QMimeData:
    mime = QMimeData()
    clean = str(path or "")
    if clean:
        mime.setUrls([QUrl.fromLocalFile(clean)])
        mime.setText(clean)
    return mime


# 底部状态栏「分类型索引进度」（设计 D）：每类一条迷你条 + x/y 计数。
# 颜色取各 Office 类型品牌色，一眼区分；浅/深主题下都可读。
_TYPE_BUCKETS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("PPT", (PPTX_EXT, ".ppt"), "#D35230"),
    ("Word", (DOCX_EXT,), "#2B6CB0"),
    ("PDF", (PDF_EXT,), "#E04A3F"),
)


class _TypeBar(QWidget):
    """单类型迷你进度：上行「标签 x/y(✓)」+ 下行细进度条（填到 x/y 比例）。"""

    def __init__(self, label: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._color = color
        self.setFixedWidth(88)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._cap = QLabel(f"{label} —")
        self._cap.setObjectName("typeBarCap")
        self._cap.setStyleSheet(f"font-size:10px;font-weight:700;color:{color};")
        lay.addWidget(self._cap)
        self._bar = QProgressBar()
        self._bar.setObjectName("typeBar")
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        self._bar.setStyleSheet(
            "QProgressBar{background:rgba(140,140,140,0.22);border:0;border-radius:3px;}"
            f"QProgressBar::chunk{{background:{color};border-radius:3px;}}"
        )
        lay.addWidget(self._bar)

    def update_counts(self, built: int, total: int) -> None:
        if total <= 0:
            self._cap.setText(f"{self._label} —")
            self._bar.setRange(0, 1)
            self._bar.setValue(0)
            self.setToolTip(f"{self._label}：未发现此类文件")
            return
        tick = " ✓" if built >= total else ""
        self._cap.setText(f"{self._label} {built:,}/{total:,}{tick}")
        self._bar.setRange(0, total)
        self._bar.setValue(min(built, total))
        self.setToolTip(f"{self._label}：已建 {built:,} / 发现 {total:,}")


class ResultItem(QWidget):
    """鍗曟潯缁撴灉鍗＄墖锛氬乏缂╃暐鍥?棣栭〉/鍛戒腑椤? + 鍙?鏂囦欢鍚?+ 鍛戒腑椤佃兌鍥?+ 楂樹寒鐗囨 + mtime)銆?"""

    activated = Signal()

    def __init__(self, r: FileResult, tok: dict, hlcss: str, ginfo: dict | None = None,
                 on_toggle_group=None, on_select=None):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._tok = tok
        self._sel = False
        self.path = r.path
        self._on_select = on_select
        self._drag_start_pos: QPoint | None = None
        # 版本组：ginfo 为组主卡时带 count（折叠起来的历史版本数）；为成员行时 member=True
        self._gid = ginfo.get("gid") if ginfo else None
        self._exp_btn = None  # 版本组展开器按钮（仅组主卡有），供就地切换文字 ▾/▴
        self._dup_badge = None
        self._dup_hint = None
        is_member = bool(ginfo and ginfo.get("member"))
        vcount = int(ginfo.get("count", 0)) if ginfo else 0
        dup_paths = [p for p in (r.duplicate_paths or []) if p]
        if len(dup_paths) > 1:
            self.setToolTip("同一文件存在于这些路径：\n" + "\n".join(dup_paths))

        outer = QHBoxLayout(self)
        outer.setContentsMargins(11 + (24 if is_member else 0), 9, 12, 9)  # 历史版本行左缩进，视觉归属上方组主卡
        outer.setSpacing(0)
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # 绗?1 琛岋細鏂囦欢鍚?+ 鍛戒腑椤靛窘绔狅紙P1 / P3 P8锛屾渶澶?3 涓級
        row = QHBoxLayout()
        row.setSpacing(6)
        if is_member:
            vtag = QLabel("历史版本")
            vtag.setStyleSheet(
                f"font-size:10px;font-weight:700;color:{tok['ink4']};"
                f"border:1px solid {tok['bd2']};border-radius:5px;padding:1px 6px;background:transparent;")
            row.addWidget(vtag, 0)
        fn = QLabel(html.escape(r.name))
        _fn_color = tok['ink2'] if is_member else tok['ink1']
        fn.setStyleSheet(f"font-size:14px;font-weight:600;color:{_fn_color};background:transparent;")
        fn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        fn.setToolTip(r.name)  # 长文件名被裁时悬停可看全名
        row.addWidget(fn, 1)

        match_kind = getattr(r, "match_kind", "partial")
        if match_kind in {
            "filename_phrase", "content_phrase", "filename_exact", "content_exact"
        }:
            exact = QLabel("文件名全字" if match_kind.startswith("filename_") else "内容全字")
            exact.setObjectName("exactMatchBadge")
            exact.setStyleSheet(
                f"font-size:10px;font-weight:600;color:{tok['ink3']};"
                f"background:{tok['field']};border:none;border-radius:5px;padding:2px 7px;"
            )
            row.addWidget(exact, 0)

        if r.hits:
            # \u547d\u4e2d\u9875\u7801\u5408\u5e76\u4e3a\u4e00\u679a\u7b49\u5bbd\u7070\u6807\u7b7e\uff08\u84dd\u8272\u9884\u7b97\u8ba9\u4f4d\u7ed9\u9009\u4e2d\u6001\u4e0e\u4e3b\u6309\u94ae\uff09\uff1b
            # \u53d6\u76f8\u5173\u5ea6\u524d\u4e09\uff0c\u518d\u6309\u9875\u7801\u5347\u5e8f\u5c55\u793a\uff08\u9875\u7801\u5373\u4f4d\u7f6e\uff0c\u6392\u5e8f\u66f4\u76f4\u89c9\uff09
            pages = " \u00b7 ".join(
                f"P{h.page_no}" for h in sorted(r.hits[:3], key=lambda h: h.page_no))
            if len(r.hits) > 3:
                pages += " \u2026"
            pg = QLabel(pages)
            pg.setStyleSheet(
                f"font-size:11px;font-weight:600;color:{tok['ink3']};"
                'font-family:"Consolas","Microsoft YaHei UI";background:transparent;padding:1px 2px;')
            pg.setToolTip("\u547d\u4e2d\u9875\u7801")
            row.addWidget(pg, 0)
        elif r.name_hit:
            nh = QLabel("\u6587\u4ef6\u540d\u547d\u4e2d")
            nh.setStyleSheet(
                f"font-size:10.5px;font-weight:600;color:{tok['ink3']};"
                f"background:{tok['field']};border:none;border-radius:5px;padding:2px 7px;")
            row.addWidget(nh, 0)
        if r.status == "filename_only":
            ext = QLabel(".ppt")
            ext.setStyleSheet(f"font-size:10px;color:{tok['ink4']};border:1px solid {tok['bd2']};border-radius:5px;padding:1px 6px;background:transparent;")
            row.addWidget(ext, 0)
        if r.hits and r.status not in ("ok", "filename_only"):
            stale = QLabel("上次索引")
            stale.setObjectName("staleIndexBadge")
            stale.setToolTip("当前文件尚未成功重建索引；命中来自最后一次成功解析的内容")
            stale.setStyleSheet(
                "font-size:10px;font-weight:700;color:#D97706;"
                f"border:1px solid {tok['bd2']};border-radius:5px;padding:1px 6px;background:transparent;"
            )
            row.addWidget(stale, 0)
        if vcount > 0 and on_toggle_group is not None:
            exp = QToolButton()
            exp.setObjectName("verExpand")
            _expanded = bool(ginfo.get("expanded"))
            exp.setText("▴ 收起版本" if _expanded else f"▾ {vcount} 个历史版本")
            exp.setCursor(Qt.PointingHandCursor)
            exp.setToolTip("同一文档的其它版本副本（按修改时间），折叠以减少结果刷屏")
            exp.clicked.connect(lambda _=False, g=self._gid: on_toggle_group(g))
            self._exp_btn = exp
            row.addWidget(exp, 0)
        if len(dup_paths) > 1:
            dup = QLabel(f"{len(dup_paths)} 个路径")
            dup.setObjectName("duplicatePathBadge")
            dup.setToolTip("同一文件的完全相同副本")
            dup.setStyleSheet(
                f"font-size:10.5px;font-weight:700;color:{tok['grn']};"
                f"border:1px solid {tok['bd2']};border-radius:6px;padding:1px 7px;background:transparent;")
            self._dup_badge = dup
            row.addWidget(dup, 0)
        lay.addLayout(row)

        # 绗?2 琛岋細楂樹寒鐗囨锛堝唴瀹瑰懡涓級/ 鑰佹牸寮忚鏄庯紙.ppt锛?
        if r.hits and r.hits[0].snippet:
            sn = QLabel(_highlight(r.hits[0].snippet, hlcss))
            sn.setTextFormat(Qt.RichText)
            sn.setStyleSheet(f"font-size:12px;color:{tok['ink2']};background:transparent;")
            sn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(sn)
        elif r.status == "filename_only":
            sub = QLabel("\u8001\u683c\u5f0f \u00b7 \u4ec5\u6587\u4ef6\u540d\u641c\u7d22\u4e0e\u9884\u89c8")
            sub.setStyleSheet(f"font-size:11.5px;color:{tok['ink4']};background:transparent;")
            lay.addWidget(sub)

        # 绗?3 琛岋細淇敼鏃堕棿锛堟樉寮忎綋鐜版柊鏃э級
        tm = _fmt_mtime(r.mtime)
        if tm:
            if len(dup_paths) > 1:
                tm = f"{tm} · 同一文件 · {len(dup_paths)} 个位置"
            t = QLabel(tm)
            t.setStyleSheet(
                f"font-size:11px;color:{tok['ink4']};background:transparent;"
                'font-family:"Consolas","Microsoft YaHei UI";')
            t.setToolTip("\n".join(dup_paths) if len(dup_paths) > 1 else "")
            lay.addWidget(t)
        if len(dup_paths) > 1:
            primary_path = dup_paths[0]
            other_count = max(0, len(dup_paths) - 1)
            hint = QLabel(f"当前打开：{_elide_middle(primary_path, 64)} · 另有 {other_count} 个完全相同副本")
            hint.setObjectName("duplicatePathHint")
            hint.setStyleSheet(f"font-size:10.5px;color:{tok['ink4']};background:transparent;")
            hint.setToolTip("\n".join(dup_paths))
            self._dup_hint = hint
            lay.addWidget(hint)
        outer.addLayout(lay, 1)
        self._apply("normal", True)

    def set_version_expanded(self, expanded: bool, count: int) -> None:
        """切换组主卡展开器的文字（就地展开/折叠后调用，不重建卡片）。"""
        if self._exp_btn is not None:
            self._exp_btn.setText("▴ 收起版本" if expanded else f"▾ {count} 个历史版本")

    def enterEvent(self, e):  # noqa: N802
        if not self._sel:
            self._apply("hover", True)

    def leaveEvent(self, e):  # noqa: N802
        if not self._sel:
            self._apply("normal", True)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_start_pos = e.position().toPoint()
            if callable(self._on_select):
                self._on_select()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag_start_pos is not None and e.buttons() & Qt.LeftButton:
            delta = e.position().toPoint() - self._drag_start_pos
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._drag_start_pos = None
                self._start_file_drag()
                e.accept()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_start_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.activated.emit()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def _start_file_drag(self) -> None:
        if not self.path:
            return
        drag = QDrag(self)
        drag.setMimeData(_file_mime_for_path(self.path))
        pm = self.grab()
        if not pm.isNull():
            if pm.width() > 280:
                pm = pm.scaledToWidth(280, Qt.SmoothTransformation)
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(min(24, pm.width()), min(24, pm.height())))
        drag.exec(Qt.CopyAction)

    def set_selected(self, sel: bool, active: bool = True) -> None:
        self._sel = sel
        self._apply("sel" if sel else "normal", active)

    def _apply(self, state: str, active: bool) -> None:
        t = self._tok
        if state == "sel":
            bg = t["sel"] if active else t["selblur"]
            bar = t["acc"] if active else t["ink4"]
        elif state == "hover":
            bg, bar = t["hover"], t["bd2"]
        else:
            bg, bar = "transparent", "transparent"
        self.setStyleSheet(f"ResultItem{{background:{bg};border-radius:{t['radius']}px;border-left:3px solid {bar};}}")


class MainWindow(QMainWindow):
    _SEARCH_SLOW_HINT_MS = 1000
    _AUTO_PREVIEW_DELAY_MS = 420
    _RESIZE_PREVIEW_DELAY_MS = 45
    _HISTORY_HINT_DELAY_MS = 900
    _UI_LOOP_INTERVAL_MS = 1000
    _UI_LOOP_SLOW_GAP_MS = 250
    _RECENT_CACHE_MS = 1000
    _LIVE_FLUSH_BATCH = 64
    _LIVE_FLUSH_YIELD_MS = 1
    _DEFERRED_LIVE_SEARCH_YIELD_MS = 1
    _LIVE_STATUS_REFRESH_MS = 1500
    _DETAIL_UPDATE_DELAY_MS = 80
    _DETAIL_DOT_DELAY_MS = 80
    _VERSION_SHIELD_REFRESH_MS = 250
    _INDEX_PROGRESS_UI_MS = 100
    _INDEX_STATUS_CACHE_MS = 1000
    _KNOWN_RECONCILE_INTERVAL_SEC = 24 * 60 * 60
    _FULL_COVERAGE_INTERVAL_SEC = 7 * 24 * 60 * 60
    _FULL_COVERAGE_DELAY_MS = 30_000
    _FULL_COVERAGE_RETRY_MS = 5_000
    _BG_LIGHT_SHUTDOWN_WAIT_MS = 250
    _BG_LIGHT_SHUTDOWN_TOTAL_WAIT_MS = 1000
    _SEARCH_SHUTDOWN_WAIT_MS = 500
    _BG_HEAVY_SHUTDOWN_WAIT_MS = 3000
    _BG_HEAVY_LABELS = {
        "open", "restore", "export", "version-restore", "version-export", "version-recover",
        "version-restore-prepare",
        "ppt-slim-analyze", "ppt-slim-create",
    }

    def __init__(self, conn=None, render_worker=None, thumb_worker=None, version_mgr=None,
                 do_index=True, roots: list[str] | None = None, workers: int | None = None,
                 document_search_enabled: bool | None = None,
                 smart_grouping_enabled: bool | None = None):
        super().__init__()
        self.setWindowTitle(f"PPT Doctor · PPT 查询助手   v{__version__}")
        app_icon = QApplication.instance().windowIcon()
        if app_icon.isNull():
            app_icon = QIcon(_asset_path("app.ico"))
        self.setWindowIcon(app_icon)
        self.resize(1180, 760)
        self._title_h = 52  # 鑷粯鐜荤拑鏍囬鏍忛珮搴︼紙nativeEvent 鎷栧姩鍖?缂╂斁杈瑰垽瀹氱敤锛?
        self.setWindowFlag(Qt.FramelessWindowHint, True)  # 鏃犺竟妗?鈫?鑷粯鐜荤拑鏍囬鏍?

        self._theme = _load_theme()
        self._tok = theme.tok(self._theme)
        self._document_search_enabled = (
            get_document_search_enabled()
            if document_search_enabled is None else bool(document_search_enabled)
        )
        self._smart_grouping_enabled = (
            get_smart_grouping_enabled()
            if smart_grouping_enabled is None else bool(smart_grouping_enabled)
        )
        ensure_completed_index_feature_signature(self._current_index_feature_signature())
        self._feature_change_cb = None
        self._db_path = str(cfg_db_path())
        self._conn = conn or db.connect(self._db_path)
        db.init_db(self._conn)

        self._results: list[FileResult] = []
        self._results_raw: list[FileResult] = []
        self._render_gen = 0
        self._bg_tasks: list[BackgroundTask] = []
        self._settings_dialogs: list = []
        self._active_heavy_op: str | None = None
        self._closing = False  # 鍏崇獥涓細鍚庡彴浠诲姟鍥炶皟涓嶅啀纰?UI锛堥槻瑙﹁揪宸查攢姣佹帶浠讹級
        self._showing_recent = False
        self._recent_cache: list[FileResult] | None = None
        self._recent_cache_at = 0.0
        self._recent_load_inflight_token: int | None = None
        self._status_refresh_inflight_token: int | None = None
        self._index_status_cache: dict | None = None
        self._index_status_cache_at = 0.0
        self._cur: FileResult | None = None
        self._cur_item_widget: ResultItem | None = None
        self._search_seq = 0
        self._search_pending_req: int | None = None
        self._search_worker: SearchWorker | None = None
        self._async_search = conn is None and do_index
        self._live_refresh_after_search = False
        self._live_deferred_paths: set[str] = set()
        self._search_slow_hint_req_id = 0
        self._search_slow_hint_query = ""
        self._auto_preview_token = 0
        self._auto_preview_seq = 0
        self._clipboard_copy_token = 0
        self._page_text_copy_token = 0
        self._detail_update_token = 0
        self._detail_update_force = False
        self._detail_load_inflight_token: int | None = None
        self._detail_load_inflight_path: str | None = None
        self._detail_load_inflight_file_id: int | None = None
        self._version_preview_inflight: set[str] = set()
        self._restore_diff_inflight: set[tuple[str, str]] = set()
        self._detail_dot_token = 0
        self._detail_dot_inflight_token: int | None = None
        self._detail_dot_inflight_path: str | None = None
        self._detail_dot_has = False  # 最近一次红点检查结果（切 Tab 时复用，不重复查库）
        self._detail_hint_token = 0
        self._detail_hint_inflight_token: int | None = None
        self._detail_hint_inflight_path: str | None = None
        self._recent_load_token = 0
        self._status_refresh_token = 0
        self._empty_status_token = 0
        self._empty_status_inflight_token: int | None = None
        self._empty_status_inflight_mode: str | None = None
        self._empty_suggest_token = 0
        self._empty_suggest_inflight_token: int | None = None
        self._empty_query_suggestion = ""
        self._startup_index_token = 0
        self._startup_index_check_started_at = 0.0
        self._startup_index_check_last_ms = 0.0
        self._startup_index_check_last_files = 0
        self._startup_index_check_last_pages = 0
        self._startup_index_check_last_pending = 0
        self._startup_index_check_decision = "pending"
        self._startup_index_check_error = ""
        self._startup_known_checked = 0
        self._startup_known_changed = 0
        self._startup_known_remaining = 0
        self._startup_pending_queue: deque[str] = deque()
        self._startup_pending_timer: QTimer | None = None
        self._index_rebuild_reason = ""
        self._version_shield_token = 0
        self._version_shield_inflight_token: int | None = None
        self._suppress_select_preview = False
        self._preview_deferred_due_to_busy = False
        self._index_started_at = 0.0
        self._index_search_ready = False
        self._index_last_done = 0
        self._index_last_total = 0
        self._index_last_current = ""
        self._index_last_summary: dict | None = None
        self._index_progress_last_ui_at = 0.0
        self._index_progress_last_phase: str | None = None
        self._index_rate_ema = 0.0
        self._index_rate_last_done = 0
        self._index_rate_last_at = 0.0
        self._hit_idx = 0
        self._view_page = 1  # 褰撳墠棰勮椤碉紙鍘熷椤靛簭锛屾粴杞彲鑴辩鍛戒腑椤佃嚜鐢辩炕锛?
        self._preview_direction = 1  # 最近一次翻页方向；邻页预取优先沿该方向
        self._req_id = 0
        self._cur_pixmap: QPixmap | None = None
        self._zoom = 1.0  # 棰勮缂╂斁锛?.0=閫傞厤绐楀彛锛?1 鏀惧ぇ鐪嬬粏鑺?
        self._to_tray_on_close = False
        self._thumb_btns: list[QToolButton] = []
        self._last_result_activate_at = 0.0
        # 版本组折叠（#1）：默认折叠同一 MinHash 组的历史副本为一条，点「N 个历史版本」就地展开
        self._expanded_groups: set[int] = set()              # 当前已展开的 group_id
        self._group_others: dict[int, list] = {}             # group_id -> [(idx, FileResult), ...] 隐藏的历史版本
        self._group_primary_item: dict[int, QListWidgetItem] = {}   # group_id -> 组主卡列表项
        self._group_member_items: dict[int, list] = {}       # group_id -> 已插入的成员列表项（折叠时移除）
        self._open_settings_cb = None                        # app.py 注入：状态栏热键标签点击 → 打开设置（#2）
        self._open_version_cb = None                         # app.py 注入：搜索结果 → 版本历史窗口（D6）
        self._history_hint_query = ""                        # 跨版本搜命中提示对应的 query（D3）
        self._history_hint_pending_query = ""
        self._render_plan: list = []
        self._render_plan_pos = 0
        self._render_plan_hlcss = ""
        self._render_more_item: QListWidgetItem | None = None

        self._render = render_worker or RenderWorker(self)
        self._render.rendered.connect(self._on_rendered)
        self._owns_render = render_worker is None
        if self._owns_render:
            self._render.start()

        # ``thumb_worker`` remains a compatibility argument for callers on the
        # previous API. Result cards are text-only, so it is never started or used.
        self._version_mgr = version_mgr  # 鐗堟湰绠＄悊锛坅pp.py 娉ㄥ叆锛岃鎯呴潰鏉跨敤锛涘彲 None锛?
        self._version_backend = version_mgr
        self._facet_filters: dict[str, set] = {}  # 褰撳墠 facet 绛涢€夛紙08锛?

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(280)
        self._debounce.timeout.connect(self._do_search)
        self._search_slow_hint_timer = QTimer(self)
        self._search_slow_hint_timer.setSingleShot(True)
        self._search_slow_hint_timer.setInterval(self._SEARCH_SLOW_HINT_MS)
        self._search_slow_hint_timer.timeout.connect(
            lambda: self._show_search_slow_hint(
                self._search_slow_hint_req_id,
                self._search_slow_hint_query,
            )
        )
        self._auto_preview_timer = QTimer(self)
        self._auto_preview_timer.setSingleShot(True)
        self._auto_preview_timer.setInterval(self._AUTO_PREVIEW_DELAY_MS)
        self._auto_preview_timer.timeout.connect(
            lambda: self._run_auto_preview(self._auto_preview_token, self._auto_preview_seq)
        )
        self._resize_preview_timer = QTimer(self)
        self._resize_preview_timer.setSingleShot(True)
        self._resize_preview_timer.setInterval(self._RESIZE_PREVIEW_DELAY_MS)
        self._resize_preview_timer.timeout.connect(lambda: self._update_pixmap())
        self._history_hint_timer = QTimer(self)
        self._history_hint_timer.setSingleShot(True)
        self._history_hint_timer.setInterval(self._HISTORY_HINT_DELAY_MS)
        self._history_hint_timer.timeout.connect(self._run_history_hint_search)
        self._detail_update_timer = QTimer(self)
        self._detail_update_timer.setSingleShot(True)
        self._detail_update_timer.setInterval(self._DETAIL_UPDATE_DELAY_MS)
        self._detail_update_timer.timeout.connect(
            lambda: self._run_detail_update(self._detail_update_token)
        )
        self._detail_dot_timer = QTimer(self)
        self._detail_dot_timer.setSingleShot(True)
        self._detail_dot_timer.setInterval(self._DETAIL_DOT_DELAY_MS)
        self._detail_dot_timer.timeout.connect(
            lambda: self._run_detail_dot_refresh(self._detail_dot_token)
        )
        # live 绱㈠紩鍒锋柊鍘绘姈锛歸atcher 鍦?OneDrive/PowerPoint 鍙嶅瀛樼洏鏃朵細椋庢毚寮忚Е鍙?        # _after_live_index锛岃嫢姣忔鍚屾閲嶈窇 _do_search 浼氭妸涓荤嚎绋嬬硦姝伙紙瀹炴祴 maxgap 7.89s
        # 鏈搷搴旓級銆傚悎骞舵垚涓€娆″欢杩熷埛鏂帮細椋庢毚 N 涓簨浠?鈫?鏈€澶?1 娆℃悳绱€?
        self._live_refresh = QTimer(self)
        self._live_refresh.setSingleShot(True)
        self._live_refresh.setInterval(2500)
        self._live_refresh.timeout.connect(self._do_live_refresh)
        self._live_status_refresh = QTimer(self)
        self._live_status_refresh.setSingleShot(True)
        self._live_status_refresh.setInterval(self._LIVE_STATUS_REFRESH_MS)
        self._live_status_refresh.timeout.connect(lambda: self._refresh_status())
        self._version_shield_refresh_timer = QTimer(self)
        self._version_shield_refresh_timer.setSingleShot(True)
        self._version_shield_refresh_timer.setInterval(self._VERSION_SHIELD_REFRESH_MS)
        self._version_shield_refresh_timer.timeout.connect(
            lambda: self._run_version_shield_refresh(self._version_shield_token)
        )
        self._ui_loop_samples = 0
        self._ui_loop_last_tick = time.monotonic()
        self._ui_loop_last_gap_ms = 0.0
        self._ui_loop_max_gap_ms = 0.0
        self._ui_loop_slow_gaps = 0
        self._ui_loop_timer = QTimer(self)
        self._ui_loop_timer.setInterval(self._UI_LOOP_INTERVAL_MS)
        self._ui_loop_timer.timeout.connect(self._record_ui_loop_tick)
        self._render_cache_maintenance_timer = QTimer(self)
        self._render_cache_maintenance_timer.setSingleShot(True)
        self._render_cache_maintenance_timer.setInterval(60_000)
        self._render_cache_maintenance_timer.timeout.connect(
            self._schedule_render_cache_maintenance
        )

        self._build_ui()
        self._apply_theme(self._theme, persist=False)
        if self._async_search:
            self._search_worker = SearchWorker(self._db_path, self)
            self._search_worker.searched.connect(self._on_search_done)
            self._search_worker.start()

        self._indexer: IndexWorker | None = None
        self._coverage_scan_roots: list[str] | None = None
        self._coverage_scan_reason = ""
        self._starting_automatic_coverage = False
        self._coverage_scan_timer = QTimer(self)
        self._coverage_scan_timer.setSingleShot(True)
        self._coverage_scan_timer.setInterval(self._FULL_COVERAGE_DELAY_MS)
        self._coverage_scan_timer.timeout.connect(self._run_scheduled_coverage_scan)
        # 瀹炴椂绱㈠紩鍚庡彴绾跨▼锛氫繚瀛樹簨浠朵笉鍦ㄤ富绾跨▼ parse/鍐欏簱锛堥槻 UI 鍐荤粨锛夈€?
        # do_index=False 鐨勬祴璇曟棤姝ょ嚎绋嬶紝璧?_index_file_live 鐨勫悓姝ュ厹搴曘€?
        self._live: LiveIndexer | None = None
        if do_index:
            self._live = LiveIndexer(
                self._db_path,
                allowed_exts_provider=self._enabled_index_exts,
                compute_content_hash_provider=lambda: self._smart_grouping_enabled,
            )
            self._live.indexed.connect(self._on_live_indexed)
            self._live.start()
        if do_index:
            self._schedule_startup_index_check(roots, workers)
            self._render_cache_maintenance_timer.start()
        else:
            self._refresh_status()
        self._show_recent()  # 鍚姩鍗冲垪鏈€杩戞枃浠讹紙鈶?榛樿瑙嗗浘锛屾棤闇€鍏堣緭鍏ュ啀娓呯┖锛?
        self._welcome = None  # 棣栨娆㈣繋瑕嗙洊灞傦紙app.py 鍦?show 鍚庤皟 maybe_show_welcome锛?
        self._enable_native_frame()  # 鏃犺竟妗嗙獥鍙ｆ仮澶嶇郴缁熺缉鏀?Snap/鏈€澶у寲/Win11 鍦嗚
        # 澧為噺鑷姩鏇存柊锛氫粎鎵撳寘鎬佸悗鍙伴潤榛樻鏌ワ紱鍙戠幇鏂扮増鍦ㄦ爣棰樻爮缁欓潪妯℃€?chip銆俤ev/娴嬭瘯涓嶈仈缃戜笉鎵撴壈
        self._updater = None
        if do_index and updater.is_frozen():
            self._updater = UpdateController(
                self.update_chip, update_base_url(), self.force_quit, self)
            QTimer.singleShot(4000, self._updater.start_check)
        QTimer.singleShot(0, self._start_ui_loop_monitor)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _record_ui_loop_tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        expected = self._UI_LOOP_INTERVAL_MS / 1000.0
        gap_ms = max(0.0, (now - self._ui_loop_last_tick - expected) * 1000.0)
        self._ui_loop_last_tick = now
        self._ui_loop_samples += 1
        self._ui_loop_last_gap_ms = gap_ms
        self._ui_loop_max_gap_ms = max(self._ui_loop_max_gap_ms, gap_ms)
        if gap_ms >= self._UI_LOOP_SLOW_GAP_MS:
            self._ui_loop_slow_gaps += 1

    def _start_ui_loop_monitor(self) -> None:
        if self._closing:
            return
        self._ui_loop_last_tick = time.monotonic()
        if not self._ui_loop_timer.isActive():
            self._ui_loop_timer.start()

    def _schedule_render_cache_maintenance(self) -> None:
        """Trim old page PNGs in a background lane, never on preview response."""
        if self._closing:
            return
        if (
            (self._indexer is not None and self._indexer.isRunning())
            or self._search_pending_req is not None
            or self._active_heavy_op is not None
        ):
            self._render_cache_maintenance_timer.start(60_000)
            return
        task = BackgroundTask(
            renderer_mod.maintain_render_cache,
            "render-cache-maintenance",
        )
        self._bg_tasks.append(task)
        task.finished.connect(
            lambda task=task: self._bg_tasks.remove(task)
            if task in self._bg_tasks else None
        )
        task.start()

    def diagnostic_lines(self) -> list[str]:
        lines = [
            "ui_loop: "
            f"samples={self._ui_loop_samples} "
            f"last_gap={self._ui_loop_last_gap_ms:.0f} ms "
            f"max_gap={self._ui_loop_max_gap_ms:.0f} ms "
            f"slow_gaps={self._ui_loop_slow_gaps} "
            f"threshold={self._UI_LOOP_SLOW_GAP_MS} ms"
        ]
        indexer_running = self._indexer is not None and self._indexer.isRunning()
        if indexer_running:
            elapsed_ms = max(0.0, (time.monotonic() - self._index_started_at) * 1000.0) if self._index_started_at else 0.0
            phase = "search-ready" if self._index_search_ready else "scanning"
            lines.append(
                "index_active: "
                f"phase={phase} elapsed={elapsed_ms:.0f} ms "
                f"done={self._index_last_done} total={self._index_last_total} "
                f"current={os.path.basename(self._index_last_current or '') or '-'} "
                f"deferred_live={len(self._live_deferred_paths)}")
        elif self._index_last_summary is not None:
            summary = self._index_last_summary
            lines.append(
                "index_last: "
                f"indexed={summary.get('indexed', 0)} "
                f"deleted={summary.get('deleted', 0)} "
                f"error={summary.get('error', '') or '-'}")
        elif self._live_deferred_paths:
            lines.append(f"index_deferred_live: {len(self._live_deferred_paths)}")
        lines.append(
            "startup_index_check: "
            f"decision={self._startup_index_check_decision} "
            f"last_ms={self._startup_index_check_last_ms:.0f} "
            f"files={self._startup_index_check_last_files} "
            f"pages={self._startup_index_check_last_pages} "
            f"pending={self._startup_index_check_last_pending} "
            f"known_checked={self._startup_known_checked} "
            f"known_changed={self._startup_known_changed} "
            f"known_remaining={self._startup_known_remaining} "
            f"pending_resume_queued={len(self._startup_pending_queue)} "
            f"rebuild={self._index_rebuild_reason or '-'} "
            f"error={self._startup_index_check_error or '-'}"
        )
        if self._version_mgr is not None:
            if not _backend_is_initialized(self._version_mgr):
                lines.append("version_reconcile: backend=starting")
            elif _backend_supports(self._version_mgr, "diagnostic_lines"):
                try:
                    lines.extend(self._version_mgr.diagnostic_lines())
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"version_reconcile: diagnostic_error={type(exc).__name__}")
        feature_runtime = getattr(self, "_feature_runtime", None)
        if feature_runtime is not None and hasattr(feature_runtime, "diagnostic_lines"):
            try:
                lines.extend(feature_runtime.diagnostic_lines())
            except Exception as exc:  # noqa: BLE001
                lines.append(f"feature_runtime: diagnostic_error={type(exc).__name__}")
        for name, worker in (("render_worker", self._render),):
            if worker is not None and hasattr(worker, "diagnostic_lines"):
                try:
                    lines.extend(worker.diagnostic_lines())
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{name}: diagnostic_error={type(exc).__name__}")
        if hasattr(renderer_mod, "diagnostic_lines"):
            lines.extend(renderer_mod.diagnostic_lines())
        return lines

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = AuroraCentral(self)  # 鑷粯鏋佸厜搴曪紙璇?self._tok锛夛紝objectName 浠?"central"
        self._central = central
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_glass_title())  # 鏃犺竟妗嗙獥鍙ｇ殑鑷粯鐜荤拑鏍囬鏍?

        # 合一工具栏后，topBar 仅承载搜索语法提示行（hint 隐藏时高度为 0）
        top = QWidget()
        top.setObjectName("topBar")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(2, 0, 2, 0)
        tl.setSpacing(0)
        self.query_hint = QLabel("")
        self.query_hint.setObjectName("queryHint")
        self.query_hint.setWordWrap(True)
        self.query_hint.hide()
        tl.addWidget(self.query_hint)
        root.addWidget(top)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left.setObjectName("listPane")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        self.list_head = QWidget()
        self.list_head.setObjectName("listHeadBar")
        hr = QHBoxLayout(self.list_head)
        hr.setContentsMargins(12, 6, 8, 4)
        hr.setSpacing(8)
        # 左侧条件行：已选 facet 条件 chip（点 ✕ 移除）+ 虚线「+ 筛选」浮层入口，
        # 随 listHeadBar 一起显隐；零选中时只剩「+ 筛选」
        self.facet_bar = QWidget()
        self.facet_bar.setObjectName("facetBar")
        fb = QHBoxLayout(self.facet_bar)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.setSpacing(6)
        self.facet_add_chip = QPushButton("+ 筛选")
        self.facet_add_chip.setObjectName("facetAdd")
        self.facet_add_chip.setCursor(Qt.PointingHandCursor)
        self.facet_add_chip.setAccessibleName("筛选")
        self.facet_add_chip.setToolTip("按时间 / 类型 / 页数 / 文件夹筛选")
        self.facet_add_chip.clicked.connect(self._toggle_facet)
        fb.addWidget(self.facet_add_chip)
        hr.addWidget(self.facet_bar, 0)
        self.result_count = QLabel("")
        self.result_count.setObjectName("listHead")
        hr.addWidget(self.result_count, 1)
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("sortCombo")
        self.sort_combo.addItems(["相关度", "最近修改", "文件名"])
        self.sort_combo.setToolTip("第一排序条件")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        hr.addWidget(self.sort_combo, 0)
        self.sort_secondary = QComboBox()
        self.sort_secondary.setObjectName("sortSecondary")
        self.sort_secondary.addItems(["不叠加", "最近修改", "文件名", "相关度"])
        self.sort_secondary.setToolTip("第二排序条件；例如先按文件名，再按最近修改")
        self.sort_secondary.currentIndexChanged.connect(self._on_sort_changed)
        hr.addWidget(self.sort_secondary, 0)
        self.case_sensitive_btn = QPushButton("Aa 大小写")
        self.case_sensitive_btn.setObjectName("chip")
        self.case_sensitive_btn.setCheckable(True)
        self.case_sensitive_btn.setChecked(False)
        self.case_sensitive_btn.setAccessibleName("区分大小写")
        self.case_sensitive_btn.setToolTip(
            "区分英文大小写；默认关闭。开启后 AI 不再匹配 ai"
        )
        self.case_sensitive_btn.toggled.connect(self._on_case_sensitive_changed)
        hr.addWidget(self.case_sensitive_btn, 0)
        self.list_head.hide()
        ll.addWidget(self.list_head)
        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.currentItemChanged.connect(self._on_select)
        self.result_list.itemActivated.connect(self._on_activate)
        self.result_list.itemDoubleClicked.connect(self._on_activate)
        self.result_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._context_menu)
        result_scroll = self.result_list.verticalScrollBar()
        result_scroll.actionTriggered.connect(
            lambda _action: QTimer.singleShot(0, self._load_more_if_near_bottom))
        ll.addWidget(self.result_list, 1)
        self._history_hint = QLabel("")
        self._history_hint.setObjectName("listHead")
        self._history_hint.setCursor(Qt.PointingHandCursor)
        self._history_hint.setWordWrap(True)
        self._history_hint.setContentsMargins(12, 2, 12, 6)
        self._history_hint.hide()
        self._history_hint.mousePressEvent = lambda _e: self._open_history_hint()
        ll.addWidget(self._history_hint)
        self._build_empty_hint(ll)
        # 鍒楄〃鍖虹敤 QStackedWidget 鍖呫€岀粨鏋滃垪琛?left銆嶄笌銆屼华琛ㄧ洏棣栧睆銆嶄簩閫変竴鍒囨崲锛?
        # left锛堝惈 result_list 鍙婂叏閮ㄤ俊鍙风粦瀹氾級鍘熸牱淇濈暀锛屼粎澶氫竴灞?stack 瀹瑰櫒銆?
        self._list_stack = QStackedWidget()
        self._list_stack.setObjectName("listStack")
        self._list_stack.addWidget(left)                  # index 0锛氱粨鏋滃垪琛ㄥ尯
        self.dashboard = DashboardView(self)
        self._list_stack.addWidget(self.dashboard)        # index 1锛氫华琛ㄧ洏棣栧睆
        # facet 不再占 splitter 栏位：改为「+ 筛选」chip 呼出的非模态浮层（点外面自动关闭）
        self.facet_panel = FacetPanel(self._tok, parent=self)
        self.facet_panel.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.facet_panel.setMinimumWidth(240)
        self.facet_panel.setMaximumHeight(460)
        self.facet_panel.filters_changed.connect(self._apply_facet)
        self.facet_panel.filters_changed.connect(self._refresh_facet_chips)
        self.facet_panel.hide()
        split.addWidget(self._list_stack)
        split.addWidget(self._build_preview())
        # 详情区嵌入预览卡（四 Tab：预览/大纲/版本/详情，在 _build_preview 内构造）；信号接线不变
        self.detail_panel.restore_requested.connect(self._act_restore_version)
        self.detail_panel.export_requested.connect(self._act_export_version)
        self.detail_panel.preview_requested.connect(self._request_version_preview)
        self.detail_panel.page_requested.connect(self._act_goto_page)
        self.detail_panel.slim_requested.connect(self._open_slim_window)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 6)
        split.setSizes([520, 660])
        self._split = split
        wrap = QWidget()
        wrap.setObjectName("contentWrap")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 6, 16, 10)  # 涓棿鍐呭鍖哄洓鍛ㄧ暀鐧斤紝涓嶈创绐楀彛杈?
        wl.addWidget(split)
        root.addWidget(wrap, 1)

        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status.setObjectName("statusBar")
        self.index_phase_label = QLabel("")
        self.index_phase_label.setObjectName("indexPhase")
        self.index_phase_label.hide()
        self.status.addWidget(self.index_phase_label)
        self.index_bar = IndexActivityBar()
        self.index_bar.setObjectName("indexBar")
        self.index_bar.set_accent_color(self._tok["acc"])
        self.index_bar.hide()
        self.status.addWidget(self.index_bar)
        self.index_count_label = QLabel("")
        self.index_count_label.setObjectName("indexCount")
        self.index_count_label.hide()
        self.status.addWidget(self.index_count_label)
        self.pct_label = QLabel("")
        self.pct_label.setObjectName("pctLabel")
        self.pct_label.hide()
        self.status.addWidget(self.pct_label)
        self.type_rail = self._build_type_rail()  # 分类型索引进度迷你条（设计 D）
        self.status.addWidget(self.type_rail)
        self.type_rail.hide()
        self._type_conn = None          # 分类型计数用的独立只读连接（懒开，避免主线程抢写锁）
        self._type_rail_last_at = 0.0   # 分类型条刷新节流时间戳
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_dot.hide()
        self.status.addWidget(self.status_dot)
        self.status_label = QLabel("准备中…")
        self.status.addWidget(self.status_label)
        self.last_index_label = QLabel("")
        self.last_index_label.setObjectName("lastIndexLabel")
        self.last_index_label.setToolTip("上次全量索引完成时间；实时增量索引持续进行中")
        self.last_index_label.hide()  # 首次全量索引完成后才显示
        self.status.addWidget(self.last_index_label)
        self.version_shield = QLabel("")
        self.version_shield.setObjectName("verShield")
        self.version_shield.hide()  # 鏈夌増鏈悗鎵嶆樉绀?
        self.status.addPermanentWidget(self.version_shield)
        self.hotkey_label = QLabel(f"全局热键 {get_hotkey()}")
        self.hotkey_label.setObjectName("hotkeyLabel")
        self.hotkey_label.setCursor(Qt.PointingHandCursor)
        self.hotkey_label.setToolTip("点击修改全局唤起热键")
        self.hotkey_label.installEventFilter(self)  # 点击 → 打开设置（#2 热键可改）
        self.status.addPermanentWidget(self.hotkey_label)

        # 瓒ｅ懗缁熻銆屾垜鐨勮兌鐗囨姤鍛娿€嶅叆鍙ｏ紙闈炰镜鍏ユ敞鍏ワ紝閫昏緫鍏ㄥ湪 stats_entry锛?
        from .stats_entry import install_stats_entry
        install_stats_entry(self)

        self._init_toast()
        self._init_spinner()
        self._install_shortcuts()

    # —— 分类型索引进度迷你条（设计 D）——
    def _build_type_rail(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("typeRail")
        rail.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        lay = QHBoxLayout(rail)
        lay.setContentsMargins(2, 0, 4, 0)
        lay.setSpacing(10)
        self._type_bars: dict[str, _TypeBar] = {}
        for label, _exts, color in _TYPE_BUCKETS:
            bar = _TypeBar(label, color)
            bar.setVisible(label == "PPT" or self._document_search_enabled)
            lay.addWidget(bar)
            self._type_bars[label] = bar
        rail.hide()
        return rail

    def _read_type_counts(self) -> dict[str, tuple[int, int]] | None:
        """取分类型 (已建, 总数)。文件库走独立只读连接（不抢索引器的写锁）；
        内存库（测试）无并发写者，直接读主连接。失败返 None，状态栏退化即可。"""
        try:
            conn_path = _sqlite_file_path(self._conn)
            if conn_path:
                if self._type_conn is None:
                    self._type_conn = db.connect(conn_path)
                conn = self._type_conn
            else:
                conn = self._conn
            return db.type_counts(conn)
        except Exception as e:  # noqa: BLE001
            _log.debug("type_counts failed: %s", e)
            return None

    def _update_type_rail(self, *, force: bool = False) -> None:
        """按 _TYPE_BUCKETS 分桶刷新各类型迷你条；节流约 0.6s（避免高频建库 tick 反复查库）。"""
        now = time.monotonic()
        if not force and (now - self._type_rail_last_at) < 0.6:
            self.type_rail.show()
            return
        self._type_rail_last_at = now
        per_ext = self._read_type_counts()
        if per_ext is None:
            return
        for label, exts, _color in self._enabled_type_buckets():
            built = sum(per_ext.get(e.lower(), (0, 0))[0] for e in exts)
            total = sum(per_ext.get(e.lower(), (0, 0))[1] for e in exts)
            self._type_bars[label].update_counts(built, total)
        self.type_rail.show()

    def _close_type_conn(self) -> None:
        if getattr(self, "_type_conn", None) is not None:
            try:
                self._type_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._type_conn = None

    def _install_shortcuts(self) -> None:
        """键盘补全（#4）：命中页跳转 + 聚焦搜索框。
        刻意用 Ctrl 修饰而非裸 n/N——搜索框默认持焦点，裸字母会被当成输入。"""
        QShortcut(QKeySequence("Ctrl+Down"), self, activated=lambda: self._step_hit(1))
        QShortcut(QKeySequence("Ctrl+Up"), self, activated=lambda: self._step_hit(-1))
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_search)

    def _focus_search(self) -> None:
        self.search_box.setFocus()
        self.search_box.selectAll()

    def set_hotkey_status(self, spec: str, ok: bool) -> None:
        """状态栏全局热键标签（#2）：成功显示热键；失败标黄「点此改」。app.py 注册后回调。"""
        if not hasattr(self, "hotkey_label"):
            return
        if ok:
            self.hotkey_label.setText(f"全局热键 {spec}")
            self.hotkey_label.setToolTip("点击修改全局唤起热键")
        else:
            self.hotkey_label.setText(f"⚠ 热键 {spec} 被占用 · 点此改")
            self.hotkey_label.setToolTip("该热键注册失败（可能被占用），点击修改")

    def _build_preview(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("previewPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # 椤舵爮锛氬畬鏁磋矾寰勶紙鍙鍒讹級+ 鏂囦欢鍏冧俊鎭紙澶у皬路椤垫暟路淇敼鏃堕棿锛?
        head = QWidget()
        head.setObjectName("previewHeadBar")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(2, 0, 2, 4)
        hv.setSpacing(5)
        pr = QHBoxLayout()
        pr.setSpacing(8)
        self.path_label = QLabel("← 选中左侧结果查看预览")
        self.path_label.setObjectName("pathLabel")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pr.addWidget(self.path_label, 1)
        self.copy_text_btn = QPushButton("复制本页文字")
        self.copy_text_btn.setObjectName("linkBtn")
        self.copy_text_btn.setToolTip("复制当前预览页的文字（取自已索引内容，无需打开 PowerPoint）")
        self.copy_text_btn.clicked.connect(self._act_copy_page_text)
        self.copy_text_btn.hide()
        self.copy_path_btn = QPushButton("复制路径")
        self.copy_path_btn.setObjectName("linkBtn")
        self.copy_path_btn.clicked.connect(self._act_copy_path)
        self.copy_path_btn.hide()
        # 头部动作收敛为一行：两枚文字链；详情/大纲/版本入口已并入下方 Tab 条
        pr.addWidget(self.copy_text_btn, 0)
        pr.addWidget(self.copy_path_btn, 0)
        hv.addLayout(pr)
        lay.addWidget(head)

        # Tab1「预览」内容体：metaLabel + 原图画布 + 命中页分段缩略图 + 命中导航 + 操作行
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("metaLabel")
        bl.addWidget(self.meta_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.image_label = QLabel(
            f'<div style="font-size:15px;font-weight:600;color:{self._tok["ink2"]}">选择一个搜索结果</div>'
            f'<div style="color:{self._tok["ink4"]};font-size:12px;margin-top:8px">这里会显示命中页的 PowerPoint 原图</div>')
        self.image_label.setObjectName("previewImage")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.image_label)
        # 棰勮鍖烘粴杞?= 鎸夊師濮嬮〉搴忕炕椤碉紙鐪嬪墠鍑犻〉鍒ゆ柇鏄笉鏄鎵剧殑 PPT锛?
        self.scroll.viewport().installEventFilter(self)
        self.image_label.installEventFilter(self)
        bl.addWidget(self.scroll, 1)

        # 鍛戒腑椤电缉鐣ュ浘鏉?
        self.thumb_row = QHBoxLayout()
        self.thumb_row.setSpacing(7)
        self.thumb_row.setAlignment(Qt.AlignCenter)
        thumb_wrap = QWidget()
        thumb_wrap.setLayout(self.thumb_row)
        bl.addWidget(thumb_wrap)

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("‹ 上一命中页")
        self.prev_btn.setObjectName("navBtn")
        self.prev_btn.setToolTip("上一处命中页　(Ctrl+↑)")
        self.prev_btn.clicked.connect(lambda: self._step_hit(-1))
        self.page_label = QLabel("—")
        self.page_label.setObjectName("pageLabel")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_btn = QPushButton("下一命中页 ›")
        self.next_btn.setObjectName("navBtn")
        self.next_btn.setToolTip("下一处命中页　(Ctrl+↓)")
        self.next_btn.clicked.connect(lambda: self._step_hit(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next_btn)
        bl.addLayout(nav)

        ops = QHBoxLayout()
        self.goto_btn = QPushButton("打开并跳到此页")
        self.goto_btn.setObjectName("primary")
        self.goto_btn.clicked.connect(self._act_goto)
        self.open_btn = QPushButton("打开文件")
        self.open_btn.clicked.connect(self._act_open)
        self.folder_btn = QPushButton("打开文件夹")
        self.folder_btn.clicked.connect(self._act_folder)
        self.clip_btn = QPushButton("复制到剪贴板")
        self.clip_btn.setToolTip("复制文件到剪贴板，可直接粘贴到邮件 / 聊天 / 资源管理器")
        self.clip_btn.clicked.connect(self._act_copy_clipboard)
        for b in (self.goto_btn, self.open_btn, self.folder_btn, self.clip_btn):
            b.setMinimumHeight(38)
            ops.addWidget(b)
        bl.addLayout(ops)
        # 预览卡 = 头部 + 四 Tab（预览/大纲/版本/详情）；详情数据管线沿用 _update_detail
        self.detail_panel = DetailPanel(self._tok)
        self.detail_panel.set_preview_widget(body)
        self.detail_panel.tabs.currentChanged.connect(self._on_detail_tab_changed)
        # 红点挂在 Tab 条上：选中文件有历史版本时指向「版本」Tab
        self._detail_dot = QLabel("●", self.detail_panel.tabs.tabBar())
        self._detail_dot.setObjectName("navDot")
        self._detail_dot.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._detail_dot.hide()
        lay.addWidget(self.detail_panel, 1)
        self._set_ops_enabled(False)
        return panel

    # ---------- 鐜荤拑鏍囬鏍忥紙鏃犺竟妗嗙獥鍙ｈ嚜缁橈級 ----------
    def _mk_title_icon_btn(self, tip: str, checkable: bool = False) -> QPushButton:
        """合一工具栏的 32px 方形图标按钮：图标色由 _apply_theme 统一刷新。"""
        b = QPushButton()
        b.setObjectName("iconBtn")
        b.setFixedSize(32, 32)
        b.setIconSize(QSize(16, 16))
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        return b

    def _build_glass_title(self) -> QWidget:
        tb = QWidget()
        tb.setObjectName("glassTitle")
        tb.setFixedHeight(self._title_h)
        lay = QHBoxLayout(tb)
        lay.setContentsMargins(16, 0, 6, 0)
        lay.setSpacing(9)
        dot = QLabel("◆")
        dot.setObjectName("gtDot")
        name = QLabel("PPT Doctor")
        name.setObjectName("gtName")
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("gtVer")
        self.gt_theme = QLabel(dict(theme.THEMES).get(self._theme, self._theme))
        self.gt_theme.setObjectName("gtTheme")
        self.gt_theme.hide()  # 当前主题已由工具栏按钮显示，标题栏不重复占位
        self.update_chip = QPushButton("")  # 澧為噺鏇存柊 chip锛氬彂鐜版柊鐗堟墠鏄剧ず锛岄潪妯℃€併€佷笉鎵撴柇鎼滅储
        self.update_chip.setObjectName("updateChip")
        self.update_chip.setCursor(Qt.PointingHandCursor)
        self.update_chip.hide()
        lay.addWidget(dot)
        lay.addWidget(name)
        lay.addWidget(ver)
        lay.addWidget(self.gt_theme)
        lay.addWidget(self.update_chip)
        lay.addSpacing(2)

        # —— 合一工具栏中部：搜索框是窗口的物理中心 ——
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("搜索 PPT 内容 / 文件名…")
        self.search_box.setToolTip(
            '多词完整短语优先；空格 = 同时含；"引号" = 只搜短语'
        )
        self.search_box.setFixedHeight(34)
        self.search_box.setMaximumWidth(680)
        self.search_box.addAction(_icon_search(), QLineEdit.LeadingPosition)
        self._clear_act = self.search_box.addAction(_icon_clear(), QLineEdit.TrailingPosition)
        self._clear_act.setVisible(False)
        self._clear_act.setToolTip("清空")
        self._clear_act.triggered.connect(self._clear_search_now)
        self.search_box.textChanged.connect(lambda: self._debounce.start())
        self.search_box.textChanged.connect(lambda t: self._clear_act.setVisible(bool(t)))
        self.search_box.installEventFilter(self)
        self._history_model = QStringListModel(self)
        self._completer = QCompleter(self._history_model, self)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.search_box.setCompleter(self._completer)
        self._completer.popup().setObjectName("historyPopup")
        lay.addWidget(self.search_box, 1)

        self.type_filter = QComboBox()
        self.type_filter.setObjectName("typeFilter")
        self.type_filter.setToolTip("选择要搜索的文件类型（默认 PPT）")
        type_options = [("PPT", (".pptx", ".ppt"))]
        if self._document_search_enabled:
            type_options.extend((
                ("Word", (".docx",)),
                ("PDF", (".pdf",)),
                ("全部", self._enabled_index_exts()),
            ))
        for _label, _exts in type_options:
            self.type_filter.addItem(_label, _exts)
        self.type_filter.setCurrentIndex(0)  # 默认 PPT，保住产品焦点
        self.type_filter.currentIndexChanged.connect(lambda _=0: self._do_search())
        self.type_filter.setFixedHeight(30)
        lay.addWidget(self.type_filter)
        self.mode = QComboBox()
        self.mode.addItems(["全部", "仅文件名", "仅内容"])
        self.mode.setFixedHeight(30)
        self.mode.currentIndexChanged.connect(self._do_search)
        lay.addWidget(self.mode)
        lay.addSpacing(2)

        # —— 右侧：低频功能一律图标化（悬停出说明），视觉主次让位给搜索 ——
        self.settings_btn = self._mk_title_icon_btn("打开设置")
        self.settings_btn.setAccessibleName("设置")
        self.settings_btn.clicked.connect(self._open_settings_from_button)
        lay.addWidget(self.settings_btn)
        self.theme_btn = self._mk_title_icon_btn("切换界面主题")
        self.theme_btn.setAccessibleName("主题")
        self.theme_btn.clicked.connect(self._show_theme_menu)
        lay.addWidget(self.theme_btn)
        self.title_actions = lay  # stats_entry 等外部入口在窗口控制键前插入图标按钮
        self._title_actions_insert_at = lay.count()
        lay.addSpacing(2)
        for txt, slot, oid, tip in (("—", self.showMinimized, "winMin", "最小化"),
                                    ("□", self._win_toggle_max, "winMax", "最大化 / 还原"),
                                    ("×", self.close, "winClose", "关闭")):
            btn = QPushButton(txt)
            btn.setObjectName(oid)
            btn.setFixedSize(46, self._title_h)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            lay.addWidget(btn)
        return tb

    def _win_toggle_max(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def _enable_native_frame(self) -> None:
        """鏃犺竟妗嗙獥鍙ｆ仮澶嶇郴缁熻兘鍔涳細鍙嫋鎷夌缉鏀?/ 鏈€澶у寲 / 鏈€灏忓寲 / Win11 鍦嗚銆?"""
        if not _WIN:
            return
        try:
            hwnd = int(self.winId())
            GWL_STYLE = -16
            WS_THICKFRAME = 0x00040000
            WS_MAXIMIZEBOX = 0x00010000
            WS_MINIMIZEBOX = 0x00020000
            u = ctypes.windll.user32
            style = u.GetWindowLongW(hwnd, GWL_STYLE)
            u.SetWindowLongW(hwnd, GWL_STYLE,
                             style | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX)
            # DWMWA_WINDOW_CORNER_PREFERENCE=33, value 2=ROUND锛圵in11锛涙棫绯荤粺闈欓粯澶辫触锛?
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:  # noqa: BLE001
            pass

    def nativeEvent(self, et, message):  # noqa: N802
        """WM_NCHITTEST锛氭爣棰樻爮鍖哄洖 HTCAPTION锛堟嫋鍔?鍙屽嚮鏈€澶у寲/Snap锛夛紝鍥涜竟瑙掑洖缂╂斁鐮併€?

        鐢?QCursor.pos 閫昏緫鍧愭爣锛堥潪 lParam 鐗╃悊鍧愭爣锛夐伩鍏嶉珮 DPI 缂╂斁閿欎綅锛?
        鍙充晶鎸夐挳鍖轰笉鍥?HTCAPTION锛屼互渚跨獥鍙ｆ寜閽彲鐐瑰嚮銆?
        """
        if _WIN and et == "windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
            except Exception:  # noqa: BLE001
                return super().nativeEvent(et, message)
            if msg.message == 0x0084:  # WM_NCHITTEST
                pos = self.mapFromGlobal(QCursor.pos())
                x, y, w, h, b = pos.x(), pos.y(), self.width(), self.height(), 6
                if not self.isMaximized():
                    L = x < b
                    Rr = x > w - b
                    Tp = y < b
                    Bt = y > h - b
                    if Tp and L:
                        return True, 13   # HTTOPLEFT
                    if Tp and Rr:
                        return True, 14   # HTTOPRIGHT
                    if Bt and L:
                        return True, 16   # HTBOTTOMLEFT
                    if Bt and Rr:
                        return True, 17   # HTBOTTOMRIGHT
                    if L:
                        return True, 10   # HTLEFT
                    if Rr:
                        return True, 11   # HTRIGHT
                    if Tp:
                        return True, 12   # HTTOP
                    if Bt:
                        return True, 15   # HTBOTTOM
                # 鏍囬鏍忔嫋鍔ㄥ尯锛氶伩寮€椤堕儴 b 缂╂斁杈?+ 鍙充晶 146px 绐楀彛鎸夐挳鍖?
                if b <= y < self._title_h and x < w - 146:
                    # 合一工具栏：搜索框/下拉/按钮等交互控件区域必须回 HTCLIENT，
                    # 否则点击会被系统当成标题栏拖拽，控件收不到鼠标事件。
                    child = self.childAt(pos)
                    wgt = child
                    while wgt is not None and wgt is not self:
                        if isinstance(wgt, (QLineEdit, QComboBox, QPushButton, QToolButton)):
                            return super().nativeEvent(et, message)
                        wgt = wgt.parentWidget()
                    return True, 2        # HTCAPTION
        return super().nativeEvent(et, message)

    # ---------- 鍒楄〃 / 浠〃鐩?棣栧睆鍒囨崲 ----------
    def _show_dashboard(self, *, force_refresh: bool = False) -> None:
        """鍒囧埌浠〃鐩橀灞忥紙绌烘悳绱㈤粯璁よ鍥撅級銆備笉鍔?result_list 鏈韩銆?"""
        if getattr(self, "dashboard", None) is not None:
            self._list_stack.setCurrentWidget(self.dashboard)
            self.dashboard.schedule_refresh(force=force_refresh)

    def _show_list(self) -> None:
        """鍒囧埌缁撴灉鍒楄〃鍖猴紙鏈夋悳绱㈣瘝 / 鏈夌粨鏋滄椂锛夈€?"""
        stack = getattr(self, "_list_stack", None)
        left = stack.widget(0) if stack is not None else None
        if stack is not None and left is not None:
            stack.setCurrentWidget(left)

    def _set_ops_enabled(self, on: bool) -> None:
        on = on and self._active_heavy_op is None and self._search_pending_req is None
        for w in (self.open_btn, self.folder_btn, self.clip_btn,
                  self.copy_path_btn, self.copy_text_btn,
                  self.prev_btn, self.next_btn):
            w.setEnabled(on)
        can_goto = on and self._cur is not None and (self._cur.ext or "").lower() in PPT_EXTS
        self.goto_btn.setEnabled(can_goto)
        self.goto_btn.setToolTip(
            "用 PowerPoint 打开并跳到当前页"
            if can_goto else "Word/PDF 暂不支持自动跳转；可使用“打开文件”"
        )
        for b in getattr(self, "_thumb_btns", []):
            b.setEnabled(on)
        panel = getattr(self, "detail_panel", None)
        set_detail_actions = getattr(panel, "set_file_actions_enabled", None)
        if callable(set_detail_actions):
            set_detail_actions(on)

    def _set_result_refine_enabled(self, enabled: bool) -> None:
        for w in (
            self.sort_combo,
            self.sort_secondary,
            self.case_sensitive_btn,
            self.facet_bar,
            self.facet_panel,
        ):
            w.setEnabled(enabled)

    def _clear_stale_result_context(self) -> None:
        self._results_raw = []
        self._results = []
        self._cur = None
        self._cur_item_widget = None
        self._preview_deferred_due_to_busy = False
        self._clear_detail_load_inflight()
        self.detail_panel.clear_selection()
        self._invalidate_preview_request()
        self._update_preview_header(None)
        self._clear_preview_empty()
        self.result_list.clear()
        self._set_ops_enabled(False)

    def _update_preview_header(self, r: FileResult | None) -> None:
        """棰勮椤舵爮锛氬畬鏁磋矾寰勶紙鍙鍒讹級+ 澶у皬路椤垫暟路淇敼鏃堕棿銆?"""
        if r is None:
            self.path_label.setText("← 选中左侧结果查看预览")
            self.path_label.setToolTip("")
            self.meta_label.setText("")
            self._hit_idx = 0
            self._view_page = 1
            self._zoom = 1.0
            if getattr(self, "thumb_row", None) is not None:
                self._populate_thumbs()
            if getattr(self, "page_label", None) is not None:
                self.page_label.setText("—")
            self.copy_path_btn.hide()
            self.copy_text_btn.hide()
            return
        # 面包屑式路径：目录弱化、文件名加粗——一眼先看到「哪份 PPT」，再看「在哪」
        d, f = os.path.split(r.path)
        self.path_label.setText(
            f'<span style="color:{self._tok["ink4"]}">{html.escape(_elide_middle(d, 56))}{os.sep}</span>'
            f'<span style="color:{self._tok["ink1"]};font-weight:600">{html.escape(f)}</span>'
        )
        self.path_label.setToolTip(r.path)
        self.copy_path_btn.show()
        self.copy_text_btn.show()
        parts = []
        sz = _fmt_size(r.size)
        if sz:
            parts.append(sz)
        if r.page_count:
            parts.append(f"共 {r.page_count} 页")
        tm = _fmt_mtime(r.mtime)
        if tm:
            parts.append(f"修改于 {tm}")
        self.meta_label.setText("　·　".join(parts))

    # ---------- 涓婚 ----------
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._start_ui_loop_monitor()
        self._apply_titlebar_theme()  # 绐楀彛鏄剧ず鍚庣郴缁熸爣棰樻爮鎵嶆帴鍙楁繁鑹插睘鎬?
        if not getattr(self, "_did_fade", False):
            self._did_fade = True
            from .spotlight import animations_enabled
            if animations_enabled():  # 灏婇噸绯荤粺銆屽噺寮卞姩鎬佹晥鏋溿€嶏紝鍏冲垯鐩存帴鏄剧ず
                self.setWindowOpacity(0.0)
                self._fade = QPropertyAnimation(self, b"windowOpacity", self)
                self._fade.setDuration(200)
                self._fade.setStartValue(0.0)
                self._fade.setEndValue(1.0)
                self._fade.start()
        self._maybe_show_version_intro()  # 鏈夈€岄娆＄暀鐗堛€嶅緟鍛婄煡涓旂獥鍙ｅ凡闇茶劯鍒欒ˉ寮?

    def hideEvent(self, e):  # noqa: N802
        # 托盘常驻时不需要每秒唤醒 GUI 做卡顿采样；重新显示时 showEvent 会恢复。
        self._ui_loop_timer.stop()
        super().hideEvent(e)

    def _apply_titlebar_theme(self) -> None:
        """Windows 绯荤粺鏍囬鏍忔繁娴呰窡闅忛鏍硷紙娣辫壊椋庢牸鈫掓繁鑹叉爣棰樻爮锛屾秷闄ょ櫧鏉″壊瑁傦級銆?"""
        try:
            import ctypes
            # 跟随主题明暗（修复：旧硬编码清单含不存在的 "raycast"、漏了 ocean/cyber 等深色主题，
            # 导致深海极光等的系统标题栏没深色化）。统一用 token 的 is_light 标志判定。
            dark = not self._tok.get("is_light", False)
            val = ctypes.c_int(1 if dark else 0)
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20锛圵in10 20H1+锛?
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()), 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:  # noqa: BLE001 闈?Windows / 鏃х郴缁熼潤榛樿烦杩?
            pass

    def _apply_theme(self, name: str, persist: bool = True) -> None:
        self._theme = name
        self._tok = theme.tok(name)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_qss(name))
        if getattr(self, "index_bar", None) is not None:
            self.index_bar.set_accent_color(self._tok["acc"])
        theme_label = dict(theme.THEMES).get(name, name)
        self.theme_btn.setToolTip(f"切换界面主题 · 当前：{theme_label}")
        self._refresh_toolbar_icons()
        if persist:
            _save_theme(name)
        if self._results_raw:
            self._apply_sort_render()
            self._select_first()
        self._apply_titlebar_theme()
        # 鐜荤拑鍖栭灞忓悓姝ュ埛鏂帮細central 鏋佸厜 + 鏍囬鏍忎富棰樺悕 + 浠〃鐩樺浘琛?
        if getattr(self, "_central", None) is not None:
            self._central.update()
        if getattr(self, "gt_theme", None) is not None:
            self.gt_theme.setText(dict(theme.THEMES).get(name, name))
        if getattr(self, "dashboard", None) is not None:
            self.dashboard.set_theme()
        if getattr(self, "_empty_icon", None) is not None:
            self._set_empty_icon(getattr(self, "_empty_icon_kind", "search"))

    def _refresh_toolbar_icons(self) -> None:
        """合一工具栏图标色跟随主题（ink3 静默灰），checked/hover 色由 QSS 承担。"""
        c = self._tok["ink3"]
        for btn, factory in (
            (getattr(self, "settings_btn", None), _icon_settings),
            (getattr(self, "theme_btn", None), _icon_theme),
            (getattr(self, "stats_report_btn", None), _icon_film),
        ):
            if btn is not None:
                btn.setIcon(factory(c, 16))
                btn.setIconSize(QSize(16, 16))

    def _show_theme_menu(self) -> None:
        """椤舵爮椋庢牸鎸夐挳 鈫?寮瑰嚭椋庢牸鑿滃崟锛堝綋鍓嶉鏍兼墦鍕撅級銆?"""
        menu = QMenu(self)
        for name, label in theme.THEMES:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == self._theme)
            act.triggered.connect(lambda _=False, n=name: self._apply_theme(n))
        menu.exec(self.theme_btn.mapToGlobal(self.theme_btn.rect().bottomLeft()))

    def _toggle_theme(self) -> None:
        """寰幆鍒囧埌涓嬩竴涓鏍硷紙淇濈暀鐨勫揩鎹峰垏鎹㈠叆鍙ｏ級銆?"""
        names = [n for n, _ in theme.THEMES]
        i = names.index(self._theme) if self._theme in names else 0
        self._apply_theme(names[(i + 1) % len(names)])

    # ---------- 鎼滅储 ----------
    def _refresh_history_model(self) -> None:
        self._history_model.setStringList(history.load_history(limit=10))

    def _mode_key(self) -> str:
        return {1: "filename", 2: "content"}.get(self.mode.currentIndex(), "all")

    def _update_query_hint(self, query: str) -> None:
        if not query:
            self.query_hint.hide()
            self.query_hint.setText("")
            return
        self.query_hint.setText(explain_query(
            query,
            self._mode_key(),
            case_sensitive=self.case_sensitive_btn.isChecked(),
        ).summary)
        self.query_hint.show()

    def _clear_search_now(self) -> None:
        self.search_box.clear()
        self._do_search()

    def _enabled_index_exts(self) -> tuple[str, ...]:
        return cfg_enabled_index_exts(self._document_search_enabled)

    def _current_index_feature_signature(self) -> str:
        return index_feature_signature(
            self._document_search_enabled,
            self._smart_grouping_enabled,
        )

    def _enabled_type_buckets(self):
        return tuple(
            bucket for bucket in _TYPE_BUCKETS
            if bucket[0] == "PPT" or self._document_search_enabled
        )

    def _rebuild_type_filter(self) -> None:
        tf = getattr(self, "type_filter", None)
        if tf is None:
            return
        previous = tf.currentText() or "PPT"
        blocked = tf.blockSignals(True)
        try:
            tf.clear()
            tf.addItem("PPT", PPT_EXTS)
            if self._document_search_enabled:
                tf.addItem("Word", (DOCX_EXT,))
                tf.addItem("PDF", (PDF_EXT,))
                tf.addItem("全部", self._enabled_index_exts())
            idx = tf.findText(previous)
            tf.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            tf.blockSignals(blocked)
        for label, bar in getattr(self, "_type_bars", {}).items():
            bar.setVisible(label == "PPT" or self._document_search_enabled)

    def apply_feature_flags(
        self,
        *,
        document_search_enabled: bool | None = None,
        smart_grouping_enabled: bool | None = None,
    ) -> None:
        """设置页热切换后的轻量 UI 更新；重建索引始终由后台 worker 承担。"""
        docs_changed = False
        if document_search_enabled is not None:
            next_value = bool(document_search_enabled)
            docs_changed = next_value != self._document_search_enabled
            self._document_search_enabled = next_value
        if smart_grouping_enabled is not None:
            self._smart_grouping_enabled = bool(smart_grouping_enabled)
        self._rebuild_type_filter()
        self._recent_cache = None
        self._index_status_cache = None
        if docs_changed:
            if self.search_box.text().strip():
                self._do_search()
            else:
                self._show_recent(recent_force_refresh=True)
            self._refresh_status()

    def set_version_manager(self, manager) -> None:
        if manager is not None:
            self._version_backend = manager
        self._version_mgr = manager
        if manager is None:
            self.version_shield.hide()
            self._history_hint_timer.stop()
            self._history_hint_pending_query = ""
            if hasattr(self, "_history_hint"):
                self._history_hint.hide()
        else:
            self.refresh_version_shield()
        if self._cur is not None:
            self._schedule_detail_update(force=True)

    def _note_user_activity(self) -> None:
        worker = getattr(self, "_indexer", None)
        note = getattr(worker, "note_user_activity", None)
        if callable(note):
            note()

    def _search_exts(self) -> tuple[str, ...] | None:
        """当前文件类型过滤；即使选“全部”也只覆盖用户已开启的类型。"""
        tf = getattr(self, "type_filter", None)
        return tf.currentData() if tf is not None else self._enabled_index_exts()

    def _do_search(self) -> None:
        if self._closing:
            return
        self._debounce.stop()
        self._history_hint_timer.stop()
        self._history_hint_pending_query = ""
        if hasattr(self, "_history_hint"):
            self._history_hint.hide()
        query = self.search_box.text().strip()
        self._search_seq += 1
        self._cancel_render_work_for_new_search()
        self._cancel_auto_preview()
        self._update_query_hint(query)
        if not query:
            self._clear_search_pending()
            cancel = getattr(self._search_worker, "cancel", None)
            if callable(cancel):
                cancel()
            self._show_recent()
            return
        self._recent_load_token += 1
        self._showing_recent = False
        self._show_list()  # 鏈夋悳绱㈣瘝 鈫?鏄剧ず缁撴灉鍒楄〃鍖猴紙闅愯棌浠〃鐩橀灞忥級
        if self._search_worker is not None:
            self._show_search_pending(query)
            request_args = (
                self._search_seq,
                query,
                self._mode_key(),
                self._search_exts(),
            )
            request_kwargs = {}
            if self.case_sensitive_btn.isChecked():
                request_kwargs["case_sensitive"] = True
            if isinstance(self._search_worker, SearchWorker) and not self._smart_grouping_enabled:
                request_kwargs["group_similar"] = False
            self._search_worker.request(*request_args, **request_kwargs)
            return
        started = time.perf_counter()
        results = SearchWorker._apply_mode(
            search_mod.search(
                self._conn,
                query,
                exts=self._search_exts(),
                case_sensitive=self.case_sensitive_btn.isChecked(),
                group_similar=self._smart_grouping_enabled,
            ),
            self._mode_key(),
        )
        self._finish_search(query, results, (time.perf_counter() - started) * 1000)

    def _on_case_sensitive_changed(self, _checked: bool) -> None:
        """大小写属于检索语义；切换后重跑当前词，而不是只重排旧结果。"""
        query = self.search_box.text().strip()
        self._update_query_hint(query)
        if query:
            self._do_search()

    def _show_search_pending(self, query: str) -> None:
        self._status_refresh_token += 1
        req_id = self._search_seq
        self._search_pending_req = req_id
        self.result_list.setEnabled(False)
        self._set_result_refine_enabled(False)
        has_visible_results = self.result_list.count() > 0 and not self.result_list.isHidden()
        if has_visible_results:
            self._hide_empty_hint()
            self.result_count.setText(f"搜索中… 当前结果暂留 · {len(self._results)} 个文件")
            self._set_ops_enabled(False)
        else:
            self._results_raw = []
            self._results = []
            self._cur = None
            self._clear_detail_load_inflight()
            self.detail_panel.clear_selection()
            self.result_list.clear()
            self._hide_empty_hint()
            self.result_count.setText("搜索中…")
            self._update_preview_header(None)
            self._set_ops_enabled(False)
        self.list_head.show()
        self.status_label.setText(f"\u6b63\u5728\u641c\u7d22\u300c{query}\u300d...")
        self._search_slow_hint_req_id = req_id
        self._search_slow_hint_query = query
        self._search_slow_hint_timer.setInterval(self._SEARCH_SLOW_HINT_MS)
        self._search_slow_hint_timer.start()

    def _cancel_render_work_for_new_search(self) -> None:
        self._render_gen += 1
        self._remove_load_more_item()
        self._render_plan = []
        self._render_plan_pos = 0
        self._render_plan_hlcss = ""
        self._invalidate_preview_request(clear_deferred=False)
        clear = getattr(self._render, "clear", None)
        if callable(clear):
            clear()

    def _clear_search_pending(self) -> None:
        self._search_pending_req = None
        self._search_slow_hint_timer.stop()

    def _maybe_run_deferred_live_refresh(self, query: str) -> None:
        if not self._live_refresh_after_search:
            return
        self._live_refresh_after_search = False
        if self._closing or self.search_box.text().strip() != query:
            return
        # 文件风暴期间不要刚搜完就原样重搜；等 watcher 真正空闲后至多合并跑一次。
        self._live_refresh.start()

    def _show_search_slow_hint(self, req_id: int, query: str) -> None:
        if self._closing or self._search_pending_req != req_id:
            return
        if req_id != self._search_seq or query != self.search_box.text().strip():
            return
        has_visible_results = self.result_list.count() > 0 and not self.result_list.isHidden()
        if has_visible_results:
            self.result_count.setText(
                f"搜索仍在进行… 当前结果暂留 · {len(self._results)} 个文件 · 可继续输入缩小范围")
        else:
            self.result_count.setText("搜索仍在进行… 可继续输入缩小范围")
        self.list_head.show()
        self.status_label.setText(f"\u6b63\u5728\u641c\u7d22\u300c{query}\u300d... \u67e5\u8be2\u8f83\u5927\uff0c\u53ef\u7ee7\u7eed\u8f93\u5165\u7f29\u5c0f\u8303\u56f4")

    def _on_search_done(self, req_id: int, query: str, results: object, elapsed_ms: float, error: object) -> None:
        if self._closing or req_id != self._search_seq or query != self.search_box.text().strip():
            return
        self._clear_search_pending()
        if error:
            error_text = str(error or "")
            if any(marker in error_text.casefold() for marker in ("database is locked", "database is busy")):
                self.status_label.setText("索引库正在后台收尾，请稍后再搜一次；当前结果没有丢。")
            else:
                self.status_label.setText(f"\u641c\u7d22\u5931\u8d25\uff1a{error_text}")
            self.result_count.setText("搜索失败 · 已保留当前结果" if self.result_list.count() else "搜索失败")
            if self.result_list.count():
                self.result_list.setEnabled(True)
                self._set_result_refine_enabled(True)
                self._set_ops_enabled(self._cur is not None)
                self._flush_deferred_preview_if_idle()
            else:
                self.list_head.hide()
                self._invalidate_preview_request()
                self._clear_preview_empty()
                self._update_preview_header(None)
                self._set_ops_enabled(False)
                self._show_empty_hint(query)
            self._maybe_run_deferred_live_refresh(query)
            return
        self._finish_search(query, list(results or []), elapsed_ms)
        # 索引统计不是搜索收口的一部分；缓存缺失时才补一次后台读取。
        if self._index_status_cache is None:
            self._refresh_status()
        self._maybe_run_deferred_live_refresh(query)

    def _finish_search(self, query: str, results: list[FileResult], elapsed_ms: float | None = None) -> None:
        self._clear_search_pending()
        self._results_raw = results
        self._refresh_facets()
        suffix = f" \u00b7 {elapsed_ms:.0f} ms" if elapsed_ms is not None else ""
        if results:
            self.result_count.setText(f"\u547d\u4e2d {len(results)} \u4e2a\u6587\u4ef6{suffix}")
            self.status_label.setText(f"搜索完成：命中 {len(results)} 个文件{suffix}")
        else:
            self.status_label.setText(f"搜索完成：没有命中{suffix}")
        self._apply_sort_render()
        if results:
            self.list_head.show()
            self._select_first(delayed_preview=True)
            self._animate_list_in()
            self._kick_history_search(query)
        else:
            self.list_head.hide()
            self._cur = None
            self._clear_detail_load_inflight()
            self.detail_panel.clear_selection()
            self._invalidate_preview_request()
            self._clear_preview_empty()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._show_empty_hint(query)
            if hasattr(self, "_history_hint"):
                self._history_hint.hide()

    def _animate_list_in(self) -> None:
        """结果出现时列表轻量淡入（#10 calm UI 功能性微动效）。动画结束即移除 effect，
        不留持久 GraphicsEffect 影响流式渲染性能；尊重系统「减弱动态效果」设置。"""
        try:
            from .spotlight import animations_enabled
            if not animations_enabled():
                return
            old = getattr(self, "_list_fade_anim", None)
            if old is not None:
                try:
                    old.stop()  # 停掉上一次（防 <140ms 连续搜索时旧 finished 清掉新 effect）
                except Exception:  # noqa: BLE001
                    pass
            eff = QGraphicsOpacityEffect(self._list_stack)
            self._list_stack.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(140)
            anim.setStartValue(0.55)
            anim.setEndValue(1.0)
            # 仅当 effect 仍是本次设置的那个才移除，避免旧回调误清新动画的 effect
            anim.finished.connect(
                lambda e=eff: self._list_stack.setGraphicsEffect(None)
                if self._list_stack.graphicsEffect() is e else None)
            anim.start()
            self._list_fade_anim = anim  # 防 GC
        except Exception:  # noqa: BLE001 动效失败绝不影响结果展示
            try:
                self._list_stack.setGraphicsEffect(None)
            except Exception:  # noqa: BLE001
                pass

    def _recent_files_cached(self, *, force: bool = False) -> list[FileResult] | None:
        now = time.monotonic()
        if (
            not force
            and self._recent_cache is not None
            and (now - self._recent_cache_at) * 1000 < self._RECENT_CACHE_MS
        ):
            return list(self._recent_cache)
        return None

    def _load_recent_files(self, conn_path: str | None) -> list[FileResult]:
        exts = self._enabled_index_exts()
        if conn_path:
            own = db.connect(conn_path)
            try:
                return list(db.recent_files(own, limit=20, exts=exts))
            finally:
                own.close()
        return list(db.recent_files(self._conn, limit=20, exts=exts))

    def _apply_recent_results(self, recents: list[FileResult]) -> None:
        self.result_list.setEnabled(True)
        self._set_result_refine_enabled(True)
        self._results_raw = list(recents)
        self._cur = None
        self._clear_detail_load_inflight()
        self.detail_panel.clear_selection()
        self._invalidate_preview_request()
        self._showing_recent = True
        if recents:
            self._refresh_facets()
            self._apply_sort_render()
            self.result_count.setText(f"最近修改 · {len(recents)} 个文件")
            self.list_head.show()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
        else:
            self.result_list.clear()
            self.list_head.hide()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._show_start_hint()

    def _on_recent_files_loaded(self, token: int, payload: object) -> None:
        if self._closing or token != self._recent_load_token:
            return
        if self.search_box.text().strip():
            return
        recents = list(payload or [])
        self._recent_cache = list(recents)
        self._recent_cache_at = time.monotonic()
        self._apply_recent_results(recents)

    def _finish_recent_files_load(self, task, token: int) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._recent_load_inflight_token == token:
            self._recent_load_inflight_token = None

    def _show_recent(self, *, dashboard_force_refresh: bool = False,
                     recent_force_refresh: bool = False) -> None:
        """绌烘煡璇㈤粯璁よ鍥撅細浠〃鐩橀灞?+ 澶囧ソ銆屾渶杩戞枃浠躲€嶇粨鏋滐紙鍒囧洖鎼滅储鍗崇敤锛夈€?"""
        self._clear_search_pending()
        self.query_hint.hide()
        self._clear_stale_result_context()
        already_on_recent_dashboard = (
            self._showing_recent
            and getattr(self, "dashboard", None) is not None
            and self._list_stack.currentWidget() is self.dashboard
        )
        self._showing_recent = True
        if dashboard_force_refresh or not already_on_recent_dashboard:
            self._show_dashboard(force_refresh=dashboard_force_refresh)
        recents = self._recent_files_cached(force=recent_force_refresh)
        if recents is not None:
            self._recent_load_token += 1
            self._recent_load_inflight_token = None
            self._apply_recent_results(recents)
            return
        if (
            self._recent_load_inflight_token is not None
            and self._recent_load_inflight_token == self._recent_load_token
        ):
            self.result_count.setText("\u6700\u8fd1\u4fee\u6539 \u00b7 \u52a0\u8f7d\u4e2d...")
            return
        conn_path = _sqlite_file_path(self._conn)
        self._recent_load_token += 1
        token = self._recent_load_token
        if not conn_path:
            recents = self._load_recent_files(conn_path)
            self._recent_cache = list(recents)
            self._recent_cache_at = time.monotonic()
            self._apply_recent_results(recents)
            return
        self.result_count.setText("\u6700\u8fd1\u4fee\u6539 \u00b7 \u52a0\u8f7d\u4e2d...")
        task = BackgroundTask(
            lambda conn_path=conn_path: self._load_recent_files(conn_path),
            "recent-files-load",
        )
        self._recent_load_inflight_token = token
        self._bg_tasks.append(task)
        task.done.connect(lambda payload, token=token: self._on_recent_files_loaded(token, payload))
        task.finished.connect(
            lambda task=task, token=token: self._finish_recent_files_load(task, token))
        task.start()

    def _build_empty_hint(self, parent_layout) -> None:
        """闆剁粨鏋滃紩瀵奸潰鏉匡紙榛樿闅愯棌锛岄浂缁撴灉鏃惰鐩栫粨鏋滃垪琛ㄤ綅缃級銆?"""
        self.empty_hint = QWidget()
        self.empty_hint.setObjectName("emptyHint")
        v = QVBoxLayout(self.empty_hint)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(11)
        self._empty_icon = QLabel()
        self._empty_icon.setObjectName("emptyIcon")
        self._empty_icon.setAlignment(Qt.AlignCenter)
        self._empty_icon_kind = "search"
        self._set_empty_icon("search")
        v.addWidget(self._empty_icon)
        self._empty_query_label = QLabel("没找到")
        self._empty_query_label.setObjectName("emptyTitle")
        self._empty_query_label.setAlignment(Qt.AlignCenter)
        self._empty_query_label.setWordWrap(True)
        v.addWidget(self._empty_query_label)
        self._empty_tip = QLabel("换个说法试试")
        self._empty_tip.setObjectName("emptyTip")
        self._empty_tip.setAlignment(Qt.AlignCenter)
        v.addWidget(self._empty_tip)
        self._empty_index_status = QLabel("")
        self._empty_index_status.setObjectName("emptyMeta")
        self._empty_index_status.setAlignment(Qt.AlignCenter)
        self._empty_index_status.setWordWrap(True)
        v.addWidget(self._empty_index_status)
        self._sugg_btns: dict[str, QPushButton] = {}
        for key, text in (
            ("query", "搜这个试试"),
            ("unquote", "去掉引号再搜"),
            ("fewer", "只用第一个词"),
            ("allmode", "恢复全部范围"),
            ("filename", "改搜文件名"),
        ):
            b = QPushButton(text)
            b.setObjectName("suggBtn")
            b.clicked.connect(lambda _=False, k=key: self._apply_suggestion(k))
            v.addWidget(b, 0, Qt.AlignCenter)
            self._sugg_btns[key] = b
        self._diagnose_btn = QPushButton("查看健康诊断")
        self._diagnose_btn.setObjectName("suggBtn")
        self._diagnose_btn.setToolTip("查看索引库、扫描范围、数据目录和 PowerPoint 状态")
        self._diagnose_btn.clicked.connect(self._open_health_diagnostics)
        v.addWidget(self._diagnose_btn, 0, Qt.AlignCenter)
        self._health_center_btn = QPushButton("给整个库做次体检")
        self._health_center_btn.setObjectName("suggBtn")
        self._health_center_btn.setToolTip("扫描重复 / 僵尸冷文件 / 终版诅咒 / 解析失败，并一键回收重复占用")
        self._health_center_btn.clicked.connect(self._open_health_center)
        v.addWidget(self._health_center_btn, 0, Qt.AlignCenter)
        self.empty_hint.hide()
        parent_layout.addWidget(self.empty_hint, 1)

    def _set_empty_icon(self, kind: str) -> None:
        """Use font-independent vector icons for empty and indexing states."""
        self._empty_icon_kind = "folder" if kind == "folder" else "search"
        factory = _icon_folder if self._empty_icon_kind == "folder" else _icon_search
        icon = factory(self._tok["accd"], 34)
        self._empty_icon.setText("")
        self._empty_icon.setPixmap(icon.pixmap(34, 34))

    def _index_status_text(self) -> str:
        try:
            s = self._index_status_stats_cached()
        except Exception:  # noqa: BLE001
            return f"\u7d22\u5f15\u72b6\u6001\uff1a\u8bfb\u53d6\u5931\u8d25 \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(self._mode_key())}"
        return self._index_status_text_from_stats(s, self._mode_key())

    def _index_status_text_from_stats(self, stats: dict, mode_key: str) -> str:
        try:
            files = int(stats.get("file_count", 0))
            pages = int(stats.get("page_count", 0))
        except Exception:  # noqa: BLE001
            return f"\u7d22\u5f15\u72b6\u6001\uff1a\u8bfb\u53d6\u5931\u8d25 \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(mode_key)}"
        scanning = self._indexer is not None and self._indexer.isRunning()
        if files <= 0:
            state = "\u6b63\u5728\u626b\u63cf \u00b7 \u7d22\u5f15\u5e93\u4e3a\u7a7a" if scanning else "\u7d22\u5f15\u5e93\u4e3a\u7a7a"
            return f"\u7d22\u5f15\u72b6\u6001\uff1a{state} \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(mode_key)}"
        state = "\u6b63\u5728\u626b\u63cf" if scanning else "\u7d22\u5f15\u5c31\u7eea"
        return f"\u7d22\u5f15\u72b6\u6001\uff1a{state} \u00b7 \u5df2\u7d22\u5f15 {files} \u4e2a\u6587\u4ef6 / {pages} \u9875 \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(mode_key)}"

    def _index_status_cache_hit(self) -> dict | None:
        now = time.monotonic()
        if (
            self._index_status_cache is not None
            and (now - self._index_status_cache_at) * 1000 < self._INDEX_STATUS_CACHE_MS
        ):
            return dict(self._index_status_cache)
        return None

    def _index_status_stats_cached(self, *, force: bool = False) -> dict:
        now = time.monotonic()
        if (
            not force
            and self._index_status_cache is not None
            and (now - self._index_status_cache_at) * 1000 < self._INDEX_STATUS_CACHE_MS
        ):
            return dict(self._index_status_cache)
        s = dict(db.stats(self._conn, exts=self._enabled_index_exts()))
        self._index_status_cache = dict(s)
        self._index_status_cache_at = now
        return s

    def _set_empty_index_status_async(self) -> None:
        mode_key = self._mode_key()
        cached = self._index_status_cache_hit()
        if cached is not None:
            self._empty_status_token += 1
            self._empty_status_inflight_token = None
            self._empty_status_inflight_mode = None
            self._empty_index_status.setText(self._index_status_text_from_stats(cached, mode_key))
            return
        if (
            self._empty_status_inflight_token is not None
            and self._empty_status_inflight_token == self._empty_status_token
            and self._empty_status_inflight_mode == mode_key
        ):
            self._empty_index_status.setText(f"\u7d22\u5f15\u72b6\u6001\uff1a\u8bfb\u53d6\u4e2d... \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(mode_key)}")
            return
        self._empty_status_token += 1
        token = self._empty_status_token
        self._empty_index_status.setText(f"\u7d22\u5f15\u72b6\u6001\uff1a\u8bfb\u53d6\u4e2d... \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(mode_key)}")
        conn_path = _sqlite_file_path(self._conn)
        if not conn_path:
            try:
                stats = self._load_status_stats(conn_path)
            except Exception:  # noqa: BLE001
                self._on_empty_index_status_loaded(token, mode_key, None)
                return
            self._on_empty_index_status_loaded(token, mode_key, stats)
            return
        task = BackgroundTask(
            lambda conn_path=conn_path: self._load_status_stats(conn_path),
            "empty-index-status-refresh",
        )
        self._empty_status_inflight_token = token
        self._empty_status_inflight_mode = mode_key
        self._bg_tasks.append(task)
        task.done.connect(
            lambda payload, token=token, mode_key=mode_key:
                self._on_empty_index_status_loaded(token, mode_key, payload)
        )
        task.finished.connect(
            lambda task=task, token=token: self._finish_empty_index_status(task, token))
        task.start()

    def _finish_empty_index_status(self, task, token: int) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._empty_status_inflight_token == token:
            self._empty_status_inflight_token = None
            self._empty_status_inflight_mode = None

    def _on_empty_index_status_loaded(self, token: int, mode_key: str, payload: object) -> None:
        if self._closing or token != self._empty_status_token:
            return
        if self._mode_key() != mode_key:
            return
        if not isinstance(payload, dict):
            self._empty_index_status.setText(f"\u7d22\u5f15\u72b6\u6001\uff1a\u8bfb\u53d6\u5931\u8d25 \u00b7 \u5f53\u524d\u8303\u56f4\uff1a{mode_label(mode_key)}")
            return
        self._index_status_cache = dict(payload)
        self._index_status_cache_at = time.monotonic()
        self._empty_index_status.setText(self._index_status_text_from_stats(payload, mode_key))

    def _show_empty_hint(self, query: str) -> None:
        """闆剁粨鏋滃紩瀵硷細鍒楄〃璁╀綅锛岀粰銆屾病鎵惧埌 + 鍙偣寤鸿銆嶃€?"""
        self.result_list.hide()
        self._set_empty_icon("search")
        self._empty_tip.setText("换个说法试试")
        self._empty_query_label.setText(f"\u6ca1\u627e\u5230\u300c{query}\u300d")
        self._set_empty_index_status_async()
        sugg = suggestion_keys(query, self._mode_key())
        for key, btn in self._sugg_btns.items():
            btn.setVisible(key in sugg)
        self._sugg_btns["query"].hide()
        self.empty_hint.show()
        self._set_empty_query_suggestion_async(query)

    def _show_start_hint(self) -> None:
        """鏃犳渶杩戞枃浠讹紙鍒氳 / 杩樺湪绱㈠紩锛夋椂鐨勮捣姝ュ紩瀵硷紝澶嶇敤 emptyHint 瀹瑰櫒锛堥殣钘忓缓璁寜閽級銆?"""
        self.result_list.hide()
        self._set_empty_icon("folder")
        self._empty_query_label.setText("\u8fd8\u5728\u6574\u7406\u4f60\u7684 PPT...")
        self._empty_tip.setText("索引好后这里会列出最近文件；现在就能在上方搜索框直接搜你写过的字")
        self._set_empty_index_status_async()
        self._empty_suggest_token += 1
        self._empty_suggest_inflight_token = None
        self._empty_query_suggestion = ""
        for btn in self._sugg_btns.values():
            btn.hide()
        self.empty_hint.show()

    def _hide_empty_hint(self, *, invalidate_status: bool = True) -> None:
        if getattr(self, "empty_hint", None) is not None:
            if invalidate_status:
                self._empty_status_token += 1
                self._empty_status_inflight_token = None
                self._empty_status_inflight_mode = None
                self._empty_suggest_token += 1
                self._empty_suggest_inflight_token = None
                self._empty_query_suggestion = ""
            self.empty_hint.hide()
            self.result_list.show()

    def _apply_suggestion(self, key: str) -> None:
        q = self.search_box.text()
        search_started = False
        if key == "query":
            if self._empty_query_suggestion:
                self.search_box.setText(self._empty_query_suggestion)
        elif key == "unquote":
            for ch in ('"', "\u201c", "\u201d"):
                q = q.replace(ch, "")
            self.search_box.setText(q)
        elif key == "fewer":
            parts = q.split()
            if parts:
                self.search_box.setText(parts[0])
        elif key == "filename":
            old_index = self.mode.currentIndex()
            self.mode.setCurrentIndex(1)
            search_started = self.mode.currentIndex() != old_index
        elif key == "allmode":
            old_index = self.mode.currentIndex()
            self.mode.setCurrentIndex(0)
            search_started = self.mode.currentIndex() != old_index
        if not search_started:
            self._do_search()

    def _load_query_suggestions(self, conn_path: str | None, query: str) -> list[str]:
        if conn_path:
            conn = db.connect(conn_path)
            try:
                return search_mod.suggest_queries(conn, query, limit=1)
            finally:
                conn.close()
        return search_mod.suggest_queries(self._conn, query, limit=1)

    def _set_empty_query_suggestion_async(self, query: str) -> None:
        self._empty_suggest_token += 1
        token = self._empty_suggest_token
        self._empty_suggest_inflight_token = token
        self._empty_query_suggestion = ""
        conn_path = _sqlite_file_path(self._conn)
        if not conn_path:
            try:
                suggestions = self._load_query_suggestions(conn_path, query)
            except Exception:  # noqa: BLE001
                suggestions = []
            self._on_empty_query_suggestion_loaded(token, query, suggestions)
            return
        task = BackgroundTask(
            lambda conn_path=conn_path, query=query: self._load_query_suggestions(conn_path, query),
            "empty-query-suggest",
        )
        self._bg_tasks.append(task)
        task.done.connect(
            lambda payload, token=token, query=query:
                self._on_empty_query_suggestion_loaded(token, query, payload)
        )
        task.finished.connect(
            lambda task=task, token=token: self._finish_empty_query_suggestion(task, token))
        task.start()

    def _finish_empty_query_suggestion(self, task, token: int) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._empty_suggest_inflight_token == token:
            self._empty_suggest_inflight_token = None

    def _on_empty_query_suggestion_loaded(self, token: int, query: str, payload: object) -> None:
        if self._closing or token != self._empty_suggest_token:
            return
        if self.empty_hint.isHidden() or self.search_box.text() != query:
            return
        suggestions = payload if isinstance(payload, list) else []
        if not suggestions:
            self._sugg_btns["query"].hide()
            return
        suggestion = str(suggestions[0]).strip()
        if not suggestion:
            self._sugg_btns["query"].hide()
            return
        self._empty_query_suggestion = suggestion
        btn = self._sugg_btns["query"]
        shown = _elide_middle(suggestion, 18)
        btn.setText(f"搜「{shown}」")
        btn.setToolTip(f"把搜索词改成：{suggestion}")
        btn.show()

    def _open_settings_from_button(self) -> None:
        cb = self._open_settings_cb
        if callable(cb):
            cb()
            return
        self._open_health_diagnostics()

    def _open_health_diagnostics(self) -> None:
        """闆剁粨鏋?璧锋鎬佺殑涓€閿帓鏌ュ叆鍙ｏ細鎵撳紑璁剧疆骞跺畾浣嶅埌鍋ュ悍璇婃柇銆?"""
        from .settings_dialog import SettingsDialog

        for dlg in list(self._settings_dialogs):
            try:
                if getattr(dlg, "_closing", False) or not dlg.isVisible():
                    self._settings_dialogs.remove(dlg)
                    continue
                dlg.tabs.setCurrentIndex(1)
                dlg.raise_()
                dlg.activateWindow()
                return
            except RuntimeError:
                self._settings_dialogs.remove(dlg)
        dlg = SettingsDialog(
            self._version_backend,
            self,
            on_rescan=self._request_full_rescan,
            on_feature_change=self._feature_change_cb,
        )
        dlg.tabs.setCurrentIndex(1)
        self._settings_dialogs.append(dlg)
        dlg.destroyed.connect(lambda _=None, d=dlg: self._settings_dialogs.remove(d) if d in self._settings_dialogs else None)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _health_db_path(self) -> str:
        return _sqlite_file_path(self._conn) or self._db_path

    def _recycle_health_paths_and_sync_index(self, paths: list[str]) -> dict:
        from .. import health

        res = dict(health.recycle_paths(paths) or {})
        recycled_paths = [
            str(p) for p in (res.get("recycled_paths") or [])
            if p
        ]
        deleted = 0
        if recycled_paths:
            conn = db.connect(self._health_db_path())
            try:
                for path in recycled_paths:
                    if db.get_file_by_path(conn, path) is not None:
                        deleted += 1
                    db.delete_file(conn, path)
                conn.commit()
            except Exception as exc:  # noqa: BLE001 索引同步失败不能影响回收站可撤销语义
                res["index_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                conn.close()
        res["index_deleted"] = deleted
        return res

    def _after_health_recycle(self, result: object) -> None:
        if self._closing or not isinstance(result, dict):
            return
        try:
            deleted = int(result.get("index_deleted", 0) or 0)
        except (TypeError, ValueError):
            deleted = 0
        if result.get("index_error"):
            self._toast("文件已进回收站，但索引同步失败；稍后全量扫描会自动修正")
        if deleted <= 0:
            return
        self._index_status_cache = None
        self._recent_cache = None
        self._recent_cache_at = 0.0
        self._refresh_status({"indexed": 0, "deleted": deleted})
        if self.search_box.text().strip():
            self._do_search()
        elif self._showing_recent:
            self._show_recent(dashboard_force_refresh=True, recent_force_refresh=True)
        elif getattr(self, "dashboard", None) is not None:
            schedule = getattr(self.dashboard, "schedule_refresh", None)
            if callable(schedule):
                schedule(force=True)

    def _open_health_center(self) -> None:
        """打开「库体检中心」：扫描重复 / 僵尸 / 诅咒 / 解析失败 + 一键回收重复占用。"""
        from .. import health
        from .health_window import HealthWindow

        wins = getattr(self, "_health_windows", None)
        if wins is None:
            wins = []
            self._health_windows = wins
        for w in list(wins):
            try:
                if getattr(w, "_closing", False) or not w.isVisible():
                    wins.remove(w)
                    continue
                w.refresh()
                w.raise_()
                w.activateWindow()
                return
            except RuntimeError:
                if w in wins:
                    wins.remove(w)

        db_path = self._health_db_path()

        def _scan():
            conn = db.connect(db_path)
            try:
                return health.scan_health(conn)
            finally:
                conn.close()

        win = HealthWindow(self._tok, _scan, self._recycle_health_paths_and_sync_index, self)
        wins.append(win)
        win.destroyed.connect(
            lambda _=None, w=win: wins.remove(w) if w in wins else None)
        win.show()
        win.raise_()
        win.activateWindow()

    def _open_slim_window(self, path: str | None = None) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        target = path or (self._cur.path if self._cur is not None else "")
        if not target:
            return
        if not str(target).lower().endswith(".pptx"):
            self._toast("PPT 瘦身目前只支持 .pptx 文件")
            return
        target = os.path.abspath(str(target))
        if self._activate_existing_slim_window(target):
            return

        # A stale OneDrive/network path can make even exists() block for many
        # seconds.  Keep the probe off the GUI thread and cap it to one at a
        # time so repeated clicks cannot occupy every background lane.
        if getattr(self, "_slim_open_pending", ""):
            self._toast("正在检查 PPT 文件，请稍候…")
            return
        self._slim_open_pending = target

        def _after_exists(exists: object) -> None:
            if getattr(self, "_slim_open_pending", "") == target:
                self._slim_open_pending = ""
            if not exists:
                self._toast("文件已移动、离线或无权限，无法瘦身")
                return
            self._show_slim_window(target)

        if not self._run_bg(
            lambda target=target: os.path.exists(target),
            _after_exists,
            "ppt-slim-open",
        ):
            self._slim_open_pending = ""

    def _activate_existing_slim_window(self, target: str) -> bool:
        wins = getattr(self, "_slim_windows", None)
        if wins is None:
            return False
        norm = os.path.normcase(os.path.abspath(target))
        for w in list(wins):
            try:
                if getattr(w, "_closing", False) or not w.isVisible():
                    wins.remove(w)
                    continue
                if os.path.normcase(os.path.abspath(getattr(w, "_path", ""))) == norm:
                    w.raise_()
                    w.activateWindow()
                    return True
            except RuntimeError:
                if w in wins:
                    wins.remove(w)
        return False

    def _show_slim_window(self, target: str) -> None:
        if self._closing or self._activate_existing_slim_window(target):
            return
        from .slim_window import SlimWindow

        wins = getattr(self, "_slim_windows", None)
        if wins is None:
            wins = []
            self._slim_windows = wins
        win = SlimWindow(self._tok, target, self)
        wins.append(win)
        win.destroyed.connect(lambda _=None, w=win: wins.remove(w) if w in wins else None)
        win.show()
        win.raise_()
        win.activateWindow()

    def _after_slim_created(self, result: object) -> None:
        output_path = getattr(result, "output_path", "")
        if output_path:
            self._toast(f"已生成瘦身副本：{os.path.basename(output_path)}")

    def _open_version_window_for(self, *, path: str | None = None, query: str | None = None):
        """打开版本管理窗口，并按需定位到某文件 / 直接跨版本搜某词（D3 / D6）。"""
        if self._version_mgr is None:
            return
        cb = getattr(self, "_open_version_cb", None)
        if not callable(cb):
            return
        try:
            win = cb()
        except Exception:  # noqa: BLE001
            return
        if win is None:
            return
        if query:
            fn = getattr(win, "search_history", None)
            if callable(fn):
                fn(query)
        elif path:
            fn = getattr(win, "focus_doc", None)
            if callable(fn):
                fn(path)

    def _kick_history_search(self, query: str) -> None:
        """用户停顿后再补搜历史，避免与主搜收口、排序和首屏预览争 CPU。"""
        if not hasattr(self, "_history_hint"):
            return
        self._history_hint.hide()
        self._history_hint_query = ""
        vm = self._version_mgr
        q = (query or "").strip()
        # 历史版本 FTS 只保存归一化 token、没有保留大小写原文，无法可靠二次验证；
        # 区分大小写时宁可不显示历史提示，也不混入语义不一致的假命中。
        if self.case_sensitive_btn.isChecked():
            self._history_hint_pending_query = ""
            return
        if vm is None or len(q) < 2 or not _backend_supports(vm, "search_history_details"):
            self._history_hint_pending_query = ""
            return
        self._history_hint_pending_query = q
        self._history_hint_timer.setInterval(self._HISTORY_HINT_DELAY_MS)
        self._history_hint_timer.start()

    def _run_history_hint_search(self) -> None:
        q = self._history_hint_pending_query
        self._history_hint_pending_query = ""
        vm = self._version_mgr
        if (
            self._closing
            or vm is None
            or not q
            or q != self.search_box.text().strip()
            or self._search_pending_req is not None
            or self.case_sensitive_btn.isChecked()
        ):
            return
        seq = self._search_seq
        task = BackgroundTask(lambda: vm.search_history_details(q, limit=200), "history-hint-search")
        self._bg_tasks.append(task)
        task.done.connect(lambda result, q=q, seq=seq: self._on_history_hint(seq, q, result))
        task.finished.connect(
            lambda task=task: self._bg_tasks.remove(task) if task in self._bg_tasks else None)
        task.start()

    def _on_history_hint(self, seq: int, query: str, result: object) -> None:
        if self._closing or seq != self._search_seq:
            return
        if query != self.search_box.text().strip():
            return
        total = int(result.get("total", 0)) if isinstance(result, dict) else 0
        if total <= 0:
            self._history_hint.hide()
            self._history_hint_query = ""
            return
        self._history_hint_query = query
        self._history_hint.setText(
            f"\U0001F4DC 历史版本里另有 {total} 处命中「{query}」 · 点击在版本管理中查看 →")
        self._history_hint.show()

    def _open_history_hint(self) -> None:
        q = self._history_hint_query
        if q:
            self._open_version_window_for(query=q)

    def _request_full_rescan(self) -> bool:
        """鍋ュ悍璇婃柇閲岀殑涓€閿噸鎵叆鍙ｏ細鍙彂璧峰悗鍙扮储寮曪紝涓嶉樆濉炶缃璇濇銆?"""
        return self._start_indexing(None, None)

    def _sort_key(self) -> str:
        return {"相关度": "relevance", "最近修改": "recent", "文件名": "name"}.get(
            self.sort_combo.currentText(), "relevance")

    def _sort_keys(self) -> tuple[str, ...]:
        primary = self._sort_key()
        secondary = {
            "最近修改": "recent",
            "文件名": "name",
            "相关度": "relevance",
        }.get(self.sort_secondary.currentText())
        return tuple(dict.fromkeys(k for k in (primary, secondary) if k))

    def _apply_sort_render(self) -> None:
        base = self._results_raw
        if self._facet_filters:
            base = facet_filter(base, self._facet_filters, datetime.datetime.now().timestamp())
        self._results = _sort_results(base, self._sort_keys())
        self._render_results(self._results)

    def _on_sort_changed(self) -> None:
        if self._search_pending_req is not None:
            return
        if self._results_raw:
            self._cancel_auto_preview()
            self._apply_sort_render()
            if self._results:
                self._select_first(delayed_preview=True)

    # 只同步创建首屏结果卡片；其余结果在滚到底部或点击“继续显示”时分批物化。
    # 这样排序/筛选不再销毁并重建数百个 QWidget；结果卡保持纯文本，不派发图片任务。
    _RENDER_FIRST = 12
    _RENDER_CHUNK = 12
    _HIT_NAV_MAX = 12
    _RENDER_YIELD_MS = 1
    _PREVIEW_MIN_EDGE = 1920
    _PREVIEW_MAX_EDGE = 2560
    _PREFETCH_EDGE = 1920
    _PRIORITY_RIGHT_PREVIEW = 0
    _PRIORITY_NEIGHBOR_PREFETCH = 220
    # Render a short contiguous runway while the already-open, proven-owned
    # PowerPoint session is cheap to reuse.  Three full-resolution exports avoid
    # a ~2s COM cold start on the next several wheel turns without ever showing
    # a lower-resolution placeholder.
    # The worker remains serial and checks for a real preview between pages, so
    # this does not raise concurrency.
    _NEIGHBOR_PREFETCH_MAX = 3

    def _render_results(self, results: list[FileResult]) -> None:
        self.result_list.setEnabled(True)
        self._set_result_refine_enabled(True)
        if not self._showing_recent:
            self._recent_load_token += 1
        self._hide_empty_hint(invalidate_status=bool(results))
        self._render_more_item = None
        self.result_list.clear()
        # 版本组折叠状态随每次重渲染复位（新搜索/排序/筛选/换肤都回折叠态）；展开是就地插入，不走这里
        self._group_primary_item = {}
        self._group_member_items = {}
        self._group_others = {}
        self._expanded_groups = set()
        self._render_gen += 1      # 浣滃簾涓婁竴鎵逛粛鍦ㄦ祦鍏ョ殑鍒嗘壒娓叉煋
        hlcss = theme.highlight_css(self._theme)
        plan = self._build_render_plan(results)
        self._render_plan = plan
        self._render_plan_hlcss = hlcss
        self._render_plan_pos = self._flush_plan(
            plan, 0, self._RENDER_FIRST, hlcss)  # 只物化首屏，后续按滚动/点击加载
        self._add_load_more_item()

    def _remove_load_more_item(self) -> None:
        item = self._render_more_item
        self._render_more_item = None
        if item is None:
            return
        row = self.result_list.row(item)
        if row < 0:
            return
        widget = self.result_list.itemWidget(item)
        self.result_list.takeItem(row)
        if widget is not None:
            widget.deleteLater()

    def _add_load_more_item(self) -> None:
        self._remove_load_more_item()
        remaining = len(self._render_plan) - self._render_plan_pos
        if remaining <= 0:
            return
        item = QListWidgetItem()
        item.setData(Qt.UserRole, None)
        item.setFlags(Qt.ItemIsEnabled)
        button = QPushButton(f"继续显示 · 还剩 {remaining} 条")
        button.setObjectName("loadMoreResults")
        button.setToolTip("只在需要时创建更多结果卡片，避免后台无意义占用 CPU")
        button.clicked.connect(self._load_more_results)
        item.setSizeHint(button.sizeHint())
        self.result_list.addItem(item)
        self.result_list.setItemWidget(item, button)
        self._render_more_item = item

    def _load_more_results(self) -> None:
        if self._closing or self._render_plan_pos >= len(self._render_plan):
            return
        gen = self._render_gen
        self._remove_load_more_item()
        if gen != self._render_gen:
            return
        self._render_plan_pos = self._flush_plan(
            self._render_plan,
            self._render_plan_pos,
            self._render_plan_pos + self._RENDER_CHUNK,
            self._render_plan_hlcss,
        )
        self._add_load_more_item()

    def _load_more_if_near_bottom(self) -> None:
        if self._render_more_item is None or self._closing:
            return
        bar = self.result_list.verticalScrollBar()
        if bar.maximum() <= 0 or bar.value() >= max(0, bar.maximum() - 2):
            self._load_more_results()

    def _build_render_plan(self, results: list[FileResult]) -> list:
        """灞曞紑鎴愮嚎鎬ф覆鏌撹鍒掞細('h', 鏍囬)=鍒嗙粍澶?/ ('i', idx, r)=缁撴灉鏉＄洰銆?"""
        plan: list = []
        if self._should_group_by_time():
            now_ts = datetime.datetime.now().timestamp()
            idx = 0
            for label, items in group_by_time(results, now_ts):
                plan.append(("h", f"{label} 路 {len(items)}"))
                for r in items:
                    plan.append(("i", idx, r, None))
                    idx += 1
        elif self._sort_key() == "relevance":
            # 相关度默认视图：同一版本组（search 已把同组排成相邻）折叠为一条组主卡 + 可展开历史版本
            self._plan_with_version_folding(plan, results)
        else:
            for i, r in enumerate(results):
                plan.append(("i", i, r, None))
        return plan

    def _plan_with_version_folding(self, plan: list, results: list[FileResult]) -> None:
        """把连续同 group_id（>1 成员）折叠：只放 is_latest 那条组主卡（带历史版本数），
        已展开的组额外放出其它成员（成员行）。单文件 / 无组照常逐条放。idx 始终是
        该结果在 self._results 的真实下标（_on_select 依赖 UserRole→self._results[idx]）。"""
        self._group_others = {}
        n = len(results)
        i = 0
        while i < n:
            gid = results[i].group_id
            if gid is None:
                plan.append(("i", i, results[i], None))
                i += 1
                continue
            run = []  # (idx, result) 同组相邻成员
            while i < n and results[i].group_id == gid:
                run.append((i, results[i]))
                i += 1
            if len(run) <= 1:
                plan.append(("i", run[0][0], run[0][1], None))
                continue
            primary = next((p for p in run if p[1].is_latest), run[0])
            others = [p for p in run if p[0] != primary[0]]
            others.sort(key=lambda p: p[1].mtime, reverse=True)  # 历史版本按修改时间新→旧
            self._group_others[gid] = others
            expanded = gid in self._expanded_groups
            plan.append(("i", primary[0], primary[1],
                         {"gid": gid, "count": len(others), "expanded": expanded}))
            if expanded:
                for oidx, orr in others:
                    plan.append(("i", oidx, orr, {"member": True, "gid": gid}))

    def _flush_plan(self, plan: list, start: int, end: int, hlcss: str) -> int:
        """娓叉煋 plan[start:end]锛岃繑鍥炲疄闄呮覆鍒扮殑浣嶇疆锛堜緵缁壒锛夈€?"""
        for entry in plan[start:end]:
            if entry[0] == "h":
                self._add_section_header(entry[1])
            else:
                _, idx, r, ginfo = entry
                self._add_result_item(idx, r, hlcss, ginfo)
        return min(end, len(plan))

    def _stream_plan_rest(self, plan: list, pos: int, hlcss: str, gen: int) -> None:
        """鍓╀綑鏉＄洰鍒嗘壒娴佸叆锛氭瘡涓簨浠跺惊鐜?tick 琛ヤ竴鎵癸紝UI 淇濇寔鍙氦浜掋€佺粨鏋滈€愭潯娴幇銆?"""
        state = {"pos": pos}

        def step() -> None:
            if gen != self._render_gen:
                return  # 宸茶鏂颁竴娆℃悳绱?/ 鎺掑簭 / 鍏抽棴浣滃簾
            try:
                state["pos"] = self._flush_plan(
                    plan, state["pos"], state["pos"] + self._RENDER_CHUNK, hlcss)
            except RuntimeError as e:
                # 浠呫€岀獥鍙?鎺т欢 C++ 瀵硅薄宸叉瀽鏋勩€嶆槸棰勬湡鍐呰壇鎬т腑鏂紱鍏朵綑 RuntimeError
                # 鍙兘鏄湡 bug锛堜細璁╃粨鏋滃垪琛ㄩ潤榛樻埅鏂級锛岃鏃ュ織鍐嶅仠锛岀粷涓嶆棤澹板悶鎺夈€?
                if "already deleted" not in str(e).lower():
                    _log.error("娴佸紡娓叉煋寮傚父涓柇锛岀粨鏋滃彲鑳戒笉瀹屾暣", exc_info=True)
                return
            if state["pos"] < len(plan):
                QTimer.singleShot(self._RENDER_YIELD_MS, step)

        QTimer.singleShot(self._RENDER_YIELD_MS, step)

    def _should_group_by_time(self) -> bool:
        """鏃堕棿鍒嗙粍浠呭湪銆屾椂闂村簭銆嶄笅鐢熸晥锛氭渶杩戜慨鏀规帓搴忥紝鎴栫┖鏌ヨ榛樿瑙嗗浘銆?"""
        key = self._sort_key()
        if key == "recent":
            return True
        return self._showing_recent and key == "relevance"

    def _add_result_item(self, idx: int, r: FileResult, hlcss: str, ginfo: dict | None = None) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, idx)
        item.setToolTip(r.path)
        w = ResultItem(
            r,
            self._tok,
            hlcss,
            ginfo=ginfo,
            on_toggle_group=self._toggle_version_group,
            on_select=lambda item=item: self.result_list.setCurrentItem(item),
        )
        item.setSizeHint(w.sizeHint())
        self.result_list.addItem(item)
        self.result_list.setItemWidget(item, w)
        w.activated.connect(lambda item=item: self._activate_result_item(item))
        if ginfo and ginfo.get("count"):
            self._group_primary_item[ginfo["gid"]] = item  # 记录组主卡列表项，供就地展开定位

    def _toggle_version_group(self, gid: int) -> None:
        """点「N 个历史版本」：就地展开/折叠，不重渲染整表（保留选中/滚动/预览不闪）。"""
        if gid is None or self._group_primary_item.get(gid) is None:
            return
        if gid in self._expanded_groups:
            self._collapse_version_group(gid)
        else:
            self._expand_version_group(gid)
        w = self.result_list.itemWidget(self._group_primary_item[gid])
        if isinstance(w, ResultItem):
            w.set_version_expanded(gid in self._expanded_groups,
                                   len(self._group_others.get(gid) or []))

    def _expand_version_group(self, gid: int) -> None:
        primary_item = self._group_primary_item.get(gid)
        others = self._group_others.get(gid) or []
        base = self.result_list.row(primary_item) if primary_item is not None else -1
        if base < 0 or not others:
            return
        hlcss = theme.highlight_css(self._theme)
        inserted = []
        for k, (oidx, orr) in enumerate(others):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, oidx)
            item.setToolTip(orr.path)
            w = ResultItem(
                orr,
                self._tok,
                hlcss,
                ginfo={"member": True, "gid": gid},
                on_select=lambda item=item: self.result_list.setCurrentItem(item),
            )
            item.setSizeHint(w.sizeHint())
            self.result_list.insertItem(base + 1 + k, item)
            self.result_list.setItemWidget(item, w)
            w.activated.connect(lambda item=item: self._activate_result_item(item))
            # 成员是用户主动展开看的，直接请求缩略图（不受首屏 THUMB_FIRST 门限限制）
            inserted.append(item)
        self._group_member_items[gid] = inserted
        self._expanded_groups.add(gid)

    def _collapse_version_group(self, gid: int) -> None:
        members = self._group_member_items.pop(gid, [])
        cur_in_group = self.result_list.currentItem() in members
        for item in members:
            row = self.result_list.row(item)
            if row >= 0:
                w = self.result_list.itemWidget(item)
                self.result_list.takeItem(row)
                if w is not None:
                    w.deleteLater()
        self._expanded_groups.discard(gid)
        if cur_in_group:  # 选中的是被折叠的历史版本 → 选回组主卡，避免选中态丢失
            pi = self._group_primary_item.get(gid)
            if pi is not None:
                self.result_list.setCurrentItem(pi)

    def _add_section_header(self, label: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, None)
        item.setFlags(Qt.NoItemFlags)  # 鍒嗙粍澶达細涓嶅彲閫変笉鍙氦浜?
        lbl = QLabel(label)
        lbl.setObjectName("sectionHead")
        item.setSizeHint(lbl.sizeHint())
        self.result_list.addItem(item)
        self.result_list.setItemWidget(item, lbl)

    def _first_selectable_row(self) -> int:
        for i in range(self.result_list.count()):
            if self.result_list.item(i).data(Qt.UserRole) is not None:
                return i
        return -1

    def _select_first(self, *, delayed_preview: bool = False) -> None:
        row = self._first_selectable_row()
        if row >= 0:
            if delayed_preview:
                self._suppress_select_preview = True
            self.result_list.setCurrentRow(row)
            if delayed_preview:
                self._schedule_auto_preview(self._search_seq)
                QTimer.singleShot(0, lambda: setattr(self, "_suppress_select_preview", False))

    # ---------- 閫夋嫨 / 棰勮 ----------
    def _cancel_auto_preview(self) -> None:
        self._auto_preview_token += 1
        self._auto_preview_timer.stop()

    def _schedule_auto_preview(self, seq: int) -> None:
        self._auto_preview_token += 1
        self._auto_preview_seq = seq
        self._auto_preview_timer.setInterval(self._AUTO_PREVIEW_DELAY_MS)
        self._auto_preview_timer.start()

    def _run_auto_preview(self, token: int, seq: int) -> None:
        if self._closing or token != self._auto_preview_token or seq != self._search_seq:
            return
        if self._search_pending_req is not None:
            return
        if self.result_list.currentItem() is None or self._cur is None:
            return
        self._request_preview()

    def _on_select(self, cur: QListWidgetItem | None, prev: QListWidgetItem | None = None) -> None:
        if prev is not None:
            pw = self.result_list.itemWidget(prev)
            if isinstance(pw, ResultItem):
                pw.set_selected(False)
        if cur is None:
            self._detail_update_token += 1
            self._detail_update_force = False
            self._detail_dot_token += 1
            self._detail_update_timer.stop()
            self._detail_dot_timer.stop()
            self._cur = None
            self._cur_item_widget = None
            self._clear_detail_load_inflight()
            self.detail_panel.clear_selection()
            self._invalidate_preview_request()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._refresh_detail_dot()
            return
        if self._search_pending_req is not None:
            return
        idx = cur.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        self._cur = self._results[idx]
        self._hit_idx = 0
        self._preview_direction = 1
        self._view_page = self._current_page()  # 鍒濆瀹氫綅棣栦釜鍛戒腑椤碉紙鏃犲懡涓?绗?椤碉級
        w = self.result_list.itemWidget(cur)
        if isinstance(w, ResultItem):
            w.set_selected(True, self.isActiveWindow())
        self._cur_item_widget = w
        self._update_preview_header(self._cur)
        self._set_ops_enabled(True)
        self._populate_thumbs()
        self._zoom = 1.0
        if self._suppress_select_preview:
            self._show_preview_pending()
            self._schedule_detail_update()
            self._schedule_detail_dot_refresh()
            return
        self._cancel_auto_preview()
        self._request_preview()
        self._schedule_detail_update()
        self._schedule_detail_dot_refresh()

    def _relayout_split(self) -> None:
        avail = max(560, self.width() - 24)
        self._split.setSizes([int(avail * 0.44), int(avail * 0.56)])

    def _toggle_facet(self) -> None:
        """「+ 筛选」chip 呼出/收起 facet 浮层（Qt.Popup：点外面自动关闭）。"""
        if self._search_pending_req is not None:
            return
        if self.facet_panel.isHidden():
            pos = self.facet_add_chip.mapToGlobal(self.facet_add_chip.rect().bottomLeft())
            self.facet_panel.move(pos + QPoint(0, 4))
            self.facet_panel.show()
            self.facet_panel.raise_()
        else:
            self.facet_panel.hide()

    def _refresh_facet_chips(self, _filters: dict | None = None) -> None:
        """按 FacetPanel 当前选中重建结果顶栏的条件 chip 行（零选中时只剩「+ 筛选」）。"""
        lay = self.facet_bar.layout()
        while lay.count() > 1:  # 末尾固定是「+ 筛选」chip
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for dim, buckets in self.facet_panel.active_filters().items():
            for bucket in sorted(buckets):
                chip = QPushButton(f"{bucket} ✕")
                chip.setObjectName("facetActiveChip")
                chip.setCursor(Qt.PointingHandCursor)
                chip.setToolTip("点击移除该筛选条件")
                chip.clicked.connect(
                    lambda _=False, d=dim, b=bucket: self.facet_panel.remove_filter(d, b))
                lay.insertWidget(lay.count() - 1, chip)

    def _apply_facet(self, filters: dict) -> None:
        if self._search_pending_req is not None:
            return
        self._facet_filters = filters
        self._cancel_auto_preview()
        self._suppress_select_preview = True
        self._apply_sort_render()
        if self._results:
            self.result_count.setText(f"命中 {len(self._results)} 个文件")
            self.list_head.show()
            self._select_first(delayed_preview=True)
        else:
            QTimer.singleShot(0, lambda: setattr(self, "_suppress_select_preview", False))
            # 绛涢€夊悗鏃犵粨鏋滐細鍒暀銆屽懡涓?N 涓€嶉檲鏃ц鏁?+ 缁欑┖鐘舵€佹彁绀猴紙鍖哄埆浜庛€屾病鎼滃埌銆嶏級
            self.result_count.setText("筛选后无结果")
            self._cur = None
            self._clear_detail_load_inflight()
            self.detail_panel.clear_selection()
            self._invalidate_preview_request()
            self._clear_preview_empty("筛选后没有可预览结果")
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._show_facet_empty()

    def _show_facet_empty(self) -> None:
        """facet 鎶婄粨鏋滅瓫绌烘椂鐨勬彁绀衡€斺€旀槸绛涢€夊お绐勶紝涓嶆槸娌℃悳鍒般€?"""
        self.result_list.hide()
        self._set_empty_icon("search")
        self._empty_query_label.setText("筛选后没有结果")
        self._empty_tip.setText("筛选条件太窄，放宽或清掉筛选再看看")
        self._set_empty_index_status_async()
        for btn in self._sugg_btns.values():
            btn.hide()
        self.empty_hint.show()

    def _refresh_facets(self) -> None:
        """鏂扮粨鏋滈泦鏃堕噸绠楀悇缁村害鏁伴噺骞堕噸缃€変腑銆?"""
        self._facet_filters = {}
        self.facet_panel.update_counts(
            facet_counts(self._results_raw, datetime.datetime.now().timestamp()), keep=False)
        self._refresh_facet_chips()

    def _on_detail_tab_changed(self, index: int) -> None:
        """切到「版本」Tab 视同首次展开详情：触发一次性版本提示；红点随所在 Tab 显隐。"""
        if index == self.detail_panel.version_tab_index():
            self._maybe_hint_detail_versions()
        self._apply_detail_dot(getattr(self, "_detail_dot_has", False))

    def _maybe_hint_detail_versions(self) -> None:
        """棣栨灞曞紑璇︽儏涓斿綋鍓嶆枃浠舵湁鍘嗗彶鐗堟湰鏃讹紝鎻愮ず銆岃繖閲岃兘涓€閿洖鍒板巻鍙茬増鏈€嶃€?"""
        if getattr(self, "_detail_opened_once", False):
            return
        cur = getattr(self, "_cur", None)
        if cur is None or self._version_mgr is None:
            self._detail_hint_inflight_token = None
            self._detail_hint_inflight_path = None
            return
        if (
            self._detail_hint_inflight_token is not None
            and self._detail_hint_inflight_path == cur.path
        ):
            return
        self._detail_hint_token += 1
        token = self._detail_hint_token
        path = cur.path
        version_mgr = self._version_mgr
        task = BackgroundTask(
            lambda path=path, version_mgr=version_mgr: self._detail_has_versions(path, version_mgr),
            "detail-hint-check",
        )
        self._detail_hint_inflight_token = token
        self._detail_hint_inflight_path = path
        self._bg_tasks.append(task)
        task.done.connect(lambda has, token=token, path=path: self._on_detail_hint_checked(token, path, has))
        task.finished.connect(
            lambda task=task, token=token, path=path: self._finish_detail_hint_check(task, token, path))
        task.start()

    def _finish_detail_hint_check(self, task, token: int, path: str) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._detail_hint_inflight_token == token and self._detail_hint_inflight_path == path:
            self._detail_hint_inflight_token = None
            self._detail_hint_inflight_path = None

    def _on_detail_hint_checked(self, token: int, path: str, has: object) -> None:
        cur = getattr(self, "_cur", None)
        if self._closing or token != self._detail_hint_token or cur is None or cur.path != path:
            return
        if bool(has) and self.detail_panel.tabs.currentIndex() == self.detail_panel.version_tab_index():
            self._detail_opened_once = True
            self._toast("💡 这里能一键回到任意历史版本")

    def _schedule_detail_update(self, *, force: bool = False) -> None:
        if self._cur is None:  # 内容常驻：有选中文件即加载，无选中不调度
            return
        self._detail_update_token += 1
        self._detail_update_force = self._detail_update_force or force
        self._detail_update_timer.setInterval(self._DETAIL_UPDATE_DELAY_MS)
        self._detail_update_timer.start()

    def _run_detail_update(self, token: int) -> None:
        if self._closing or token != self._detail_update_token:
            return
        force = self._detail_update_force
        self._detail_update_force = False
        if force:
            self._update_detail(force=True)
        else:
            self._update_detail()

    def _clear_detail_load_inflight(self) -> None:
        self._detail_load_inflight_token = None
        self._detail_load_inflight_path = None
        self._detail_load_inflight_file_id = None

    def _update_detail(self, *, force: bool = False) -> None:
        if self._cur is None:
            self._clear_detail_load_inflight()
            self.detail_panel.clear_selection()
            return
        r = self._cur
        if (
            not force
            and self._detail_load_inflight_token is not None
            and self._detail_load_inflight_path == r.path
            and self._detail_load_inflight_file_id == r.file_id
        ):
            return
        self._detail_update_token += 1
        token = self._detail_update_token
        conn_path = _sqlite_file_path(self._conn)
        version_mgr = self._version_mgr
        task = BackgroundTask(
            lambda r=r, conn_path=conn_path, version_mgr=version_mgr: self._load_detail_payload(
                r,
                conn_path=conn_path,
                version_mgr=version_mgr,
            ),
            "detail-load",
        )
        self._detail_load_inflight_token = token
        self._detail_load_inflight_path = r.path
        self._detail_load_inflight_file_id = r.file_id
        self._bg_tasks.append(task)
        task.done.connect(lambda payload, token=token, path=r.path, file_id=r.file_id:
                          self._on_detail_payload(token, path, file_id, payload))
        task.finished.connect(
            lambda task=task, token=token, path=r.path, file_id=r.file_id:
            self._finish_detail_load(task, token, path, file_id))
        task.start()

    def _finish_detail_load(self, task, token: int, path: str, file_id: int) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if (
            self._detail_load_inflight_token == token
            and self._detail_load_inflight_path == path
            and self._detail_load_inflight_file_id == file_id
        ):
            self._detail_load_inflight_token = None
            self._detail_load_inflight_path = None
            self._detail_load_inflight_file_id = None

    def _load_detail_payload(self, r: FileResult, *, conn_path: str | None, version_mgr) -> dict:
        versions = []
        if version_mgr is not None:
            try:
                if hasattr(version_mgr, "list_versions_details"):
                    versions = version_mgr.list_versions_details(r.path)
                else:
                    versions = version_mgr.list_versions(r.path)
            except Exception:  # noqa: BLE001
                _log.warning("list_versions failed for %s", r.path, exc_info=True)
                versions = []
        titles = []
        if conn_path:
            own = db.connect(conn_path)
            try:
                titles = db.page_titles(own, r.file_id)
            except Exception:  # noqa: BLE001
                titles = []
            finally:
                own.close()
        return {"result": r, "versions": list(versions or []), "titles": list(titles or [])}

    def _on_detail_payload(self, token: int, path: str, file_id: int, payload: object) -> None:
        if self._closing or token != self._detail_update_token:
            return
        if self._cur is None:
            return
        if self._cur.path != path or self._cur.file_id != file_id:
            return
        if not isinstance(payload, dict):
            return
        r = payload.get("result") or self._cur
        versions = list(payload.get("versions") or [])
        titles = list(payload.get("titles") or [])
        self.detail_panel.update_for(
            r,
            versions,
            versioning_enabled=self._version_mgr is not None,
        )
        try:
            self.detail_panel.set_outline(titles)
        except Exception:  # noqa: BLE001
            self.detail_panel.set_outline([])
        self._set_ops_enabled(self._cur is not None)

    def _request_version_preview(self, version_id: str) -> None:
        if not version_id or self._version_mgr is None:
            return
        if not _backend_supports(self._version_mgr, "ensure_version_preview"):
            return
        if self._block_if_file_op_active():
            return
        if version_id in self._version_preview_inflight:
            return
        cur = self._cur
        if cur is None:
            return

        token = self._detail_update_token
        path = cur.path
        file_id = cur.file_id
        self._version_preview_inflight.add(version_id)
        self.detail_panel.set_version_preview_loading(version_id)
        version_mgr = self._version_mgr
        task = BackgroundTask(lambda: version_mgr.ensure_version_preview(version_id), "version-preview")
        self._bg_tasks.append(task)
        task.done.connect(
            lambda image_path, token=token, path=path, file_id=file_id, version_id=version_id:
            self._on_version_preview_ready(token, path, file_id, version_id, image_path)
        )
        task.finished.connect(lambda task=task, version_id=version_id: self._finish_version_preview(task, version_id))
        task.start()

    def _finish_version_preview(self, task, version_id: str) -> None:
        self._version_preview_inflight.discard(version_id)
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)

    def _on_version_preview_ready(
        self,
        token: int,
        path: str,
        file_id: int,
        version_id: str,
        image_path: object,
    ) -> None:
        cur = self._cur
        if self._closing or token != self._detail_update_token:
            return
        if cur is None:
            return
        if cur.path != path or cur.file_id != file_id:
            return
        self.detail_panel.set_version_preview(version_id, str(image_path) if image_path else None)

    def _run_bg(self, fn, on_done=None, label: str = "") -> bool:
        """鎶婂彲鑳介樆濉?UI 鐨勯噸娲讳涪鍚庡彴绾跨▼璺戯紝瀹屾垚缁忎俊鍙峰洖涓荤嚎绋嬨€備富绾跨▼鍙?start() 鍗宠繑鍥炪€?"""
        heavy = label in {"restore", "export", "open"}
        if heavy and self._active_heavy_op is not None:
            self._toast("已有文件操作正在进行，请稍候…")
            return False
        if heavy:
            self._active_heavy_op = label
            self._set_ops_enabled(False)
        task = BackgroundTask(fn, label)
        self._bg_tasks.append(task)

        def _safe_done(result):
            # 鍏崇獥涓?/ 鎺т欢宸查攢姣佹椂涓嶅啀纰?UI鈥斺€擟OM 鍐峰惎鍔ㄥ彲鑳芥參浜?_shutdown 鐨?wait 瓒呮椂锛?
            # Late callbacks may arrive after widgets are destroyed.
            if self._closing:
                return
            if on_done is not None:
                try:
                    on_done(result)
                except RuntimeError:
                    pass

        def _cleanup():
            if task in self._bg_tasks:
                self._bg_tasks.remove(task)
            if heavy and self._active_heavy_op == label:
                self._active_heavy_op = None
                if not self._closing:
                    self._set_ops_enabled(self._cur is not None)
                    self._flush_deferred_preview_if_idle()

        task.done.connect(_safe_done)
        task.finished.connect(_cleanup)
        task.start()
        return True

    def _block_if_file_op_active(self) -> bool:
        if self._active_heavy_op is None:
            return False
        self._toast("已有文件操作正在进行，请稍候…")
        return True

    def _block_if_search_pending(self) -> bool:
        if self._search_pending_req is None:
            return False
        self._toast("搜索还在进行，请等结果更新后再操作")
        return True

    def _preview_interaction_blocked(self) -> bool:
        return self._search_pending_req is not None or self._active_heavy_op is not None

    def _defer_preview_if_file_op_active(self) -> bool:
        if self._active_heavy_op is None:
            return False
        self._preview_deferred_due_to_busy = True
        self._show_preview_pending()
        return True

    def _flush_deferred_preview_if_idle(self) -> None:
        if not self._preview_deferred_due_to_busy:
            return
        if self._closing or self._active_heavy_op is not None or self._search_pending_req is not None:
            return
        self._preview_deferred_due_to_busy = False
        if self._cur is not None:
            self._request_preview()

    def _act_restore_version(self, path: str, version_id: str) -> None:
        if self._version_mgr is None:
            return
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        key = (path, version_id)
        if key in self._restore_diff_inflight:
            return
        if _backend_supports(self._version_mgr, "describe_version_diff"):
            self._restore_diff_inflight.add(key)
            self._toast("正在读取该版本的改动...")
            version_mgr = self._version_mgr
            task = BackgroundTask(lambda: version_mgr.describe_version_diff(version_id), "version-restore-diff")
            self._bg_tasks.append(task)
            task.done.connect(
                lambda diff, path=path, version_id=version_id: self._begin_restore_with_diff(path, version_id, diff)
            )
            task.finished.connect(
                lambda task=task, key=key: self._finish_restore_diff_task(task, key)
            )
            task.start()
            return
        if not self._confirm_restore():
            return
        self._toast("正在恢复...")

        def _after(ok):
            if ok == "locked":
                self._toast("无法恢复：该文件正被 PowerPoint 打开，请先关闭它再恢复")
                return
            self._toast("✓ 已恢复到该版本（当前内容已自动留底，不会丢）" if ok else "恢复失败")
            if ok:
                self._update_detail(force=True)

        self._run_bg(lambda: self._restore_version_off_ui(path, version_id), _after, "restore")

    def _finish_restore_diff_task(self, task, key: tuple[str, str]) -> None:
        self._restore_diff_inflight.discard(key)
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)

    def _begin_restore_with_diff(self, path: str, version_id: str, diff: object | None) -> None:
        if self._closing or self._version_mgr is None:
            return
        if self._active_heavy_op is not None or self._search_pending_req is not None:
            return
        try:
            confirmed = self._confirm_restore(diff)
        except TypeError:
            confirmed = self._confirm_restore()
        if not confirmed:
            return
        self._toast("正在恢复版本...")

        def _after(ok):
            if ok == "locked":
                self._toast("无法恢复：该文件正被 PowerPoint 打开，请先关闭后再试")
                return
            self._toast("已恢复到该版本（当前内容已自动留底）" if ok else "恢复失败")
            if ok:
                self._update_detail(force=True)

        self._run_bg(lambda: self._restore_version_off_ui(path, version_id), _after, "restore")

    def _restore_version_off_ui(self, path: str, version_id: str):
        if os.path.exists(path):
            try:
                with open(path, "r+b"):
                    pass
            except OSError:
                return "locked"
        return bool(self._version_mgr.restore_to(path, version_id))

    def _restore_diff_text(self, diff: object | None) -> str:
        if not isinstance(diff, dict):
            return ""
        lines = [str(x).strip() for x in (diff.get("lines") or []) if str(x).strip()]
        if not lines:
            return ""
        return "这版的主要变化：\n" + "\n".join(f"• {line}" for line in lines[:6])

    def _confirm_restore(self, diff: object | None = None) -> bool:
        """鎭㈠鍓嶅弸濂界‘璁わ細寮鸿皟銆屼細鑷姩鐣欏簳銆侀殢鏃跺垏鍥炪€嶏紝闄嶄綆鐮村潖鎬ф搷浣滅殑蹇冪悊璐熸媴銆?"""
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("恢复到这个版本？")
        box.setText("会用这个历史版本覆盖当前文件。")
        info = "别担心：覆盖前会自动把当前内容也留一版，之后随时能再切回来。"
        diff_text = self._restore_diff_text(diff)
        if diff_text:
            info = diff_text + "\n\n" + info
        box.setInformativeText(info)
        yes = box.addButton("恢复", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def _act_export_version(self, path: str, version_id: str) -> None:
        if self._version_mgr is None:
            return
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        from PySide6.QtWidgets import QFileDialog
        base = os.path.splitext(os.path.basename(path))[0]
        dest, _f = QFileDialog.getSaveFileName(self, "导出此版本", base + "_导出.pptx", "PowerPoint (*.pptx)")
        if not dest:
            return
        dest = ensure_pptx_suffix(dest)
        self._toast("正在导出...")
        self._run_bg(lambda: self._version_mgr.export(path, version_id, dest),
                     lambda ok: self._toast("已导出" if ok else "导出失败"), "export")

    def _act_goto_page(self, page_no: int) -> None:
        if self._preview_interaction_blocked():
            return
        if not self._cur:
            return
        self._preview_direction = 1 if page_no >= self._view_page else -1
        self._view_page = page_no
        self._request_preview()

    def _current_page(self) -> int:
        if self._cur and self._cur.hits:
            return self._cur.hits[self._hit_idx].page_no
        return 1

    def _populate_thumbs(self) -> None:
        while self.thumb_row.count():
            it = self.thumb_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._thumb_btns = []
        if not self._cur or not self._cur.hits:
            return
        hits = self._cur.hits
        unit = "段" if (self._cur.ext or "").lower() == DOCX_EXT else "页"
        # 分段控件按页码升序显示（空间直觉：页码即位置）；导航仍按 hits 的相关度顺序
        shown = sorted(range(min(len(hits), self._HIT_NAV_MAX)), key=lambda i: hits[i].page_no)
        for i in shown:
            h = hits[i]
            b = QToolButton()
            b.setObjectName("thumb")
            b.setText(f"第{h.page_no}{unit}")
            b.setCheckable(True)
            b.setChecked(i == self._hit_idx)
            b.setEnabled(self._active_heavy_op is None and self._search_pending_req is None)
            b.setFixedSize(64, 34)
            b.setProperty("hit_index", i)
            b.clicked.connect(lambda _=False, i=i: self._goto_hit(i))
            self.thumb_row.addWidget(b)
            self._thumb_btns.append(b)
        remaining = len(hits) - self._HIT_NAV_MAX
        if remaining > 0:
            more = QToolButton()
            more.setObjectName("thumbMore")
            more.setText(f"+{remaining}")
            more.setToolTip(f"还有 {remaining} 个命中页，可用上/下命中页继续切换")
            more.setEnabled(False)
            more.setFixedSize(52, 34)
            self.thumb_row.addWidget(more)

    def _goto_hit(self, i: int) -> None:
        if self._preview_interaction_blocked():
            return
        old_page = self._view_page
        self._hit_idx = i
        if self._cur and self._cur.hits:
            self._view_page = self._cur.hits[i].page_no
            self._preview_direction = 1 if self._view_page >= old_page else -1
        self._request_preview()

    def _step_hit(self, delta: int) -> None:
        if self._preview_interaction_blocked():
            return
        if not self._cur or not self._cur.hits:
            return
        self._preview_direction = 1 if delta >= 0 else -1
        self._hit_idx = max(0, min(len(self._cur.hits) - 1, self._hit_idx + delta))
        self._view_page = self._cur.hits[self._hit_idx].page_no
        self._request_preview()

    def _init_spinner(self) -> None:
        self._spin_idx = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._tick_spinner)

    def _tick_spinner(self) -> None:
        ch = "◐◓◑◒"[self._spin_idx % 4]
        self._spin_idx += 1
        accent = self._tok.get("acc", "#0A84FF")
        sub = self._tok.get("ink3", "#888")
        msg = (
            "正在连接 PowerPoint 生成原图…"
            if not getattr(self, "_preview_hinted", False)
            else "正在等待 PowerPoint 渲染原图…"
        )
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="font-size:30px;color:{accent}">{ch}</div>'
            f'<div style="color:{sub};font-size:13px;margin-top:12px">{msg}</div>')

    def _start_spinner(self) -> None:
        self._spin_idx = 0
        self._tick_spinner()
        self._spin_timer.start(90)

    def _stop_spinner(self) -> None:
        self._spin_timer.stop()

    def _invalidate_preview_request(self, *, clear_deferred: bool = True) -> None:
        if clear_deferred:
            self._preview_deferred_due_to_busy = False
        self._req_id += 1
        if hasattr(self, "_spin_timer"):
            self._stop_spinner()

    def _show_preview_pending(self) -> None:
        self._cur_pixmap = None
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="font-size:28px;color:{self._tok["ink4"]}">…</div>'
            f'<div style="color:{self._tok["ink3"]};font-size:13px;margin-top:12px">正在准备预览</div>')

    def _clear_preview_empty(self, message: str = "选中左侧结果查看预览") -> None:
        self._cur_pixmap = None
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="color:{self._tok["ink3"]};font-size:13px">{message}</div>')

    def _show_preview_unavailable(self) -> None:
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="font-size:15px;font-weight:600;color:{self._tok["ink2"]}">暂时无法生成原图预览</div>'
            f'<div style="color:{self._tok["ink3"]};font-size:12px;margin-top:10px;line-height:1.7">'
            '若 PowerPoint 正忙或有弹窗，完成当前操作后切换页码即可重试；<br>'
            '无需关闭正在编辑的文稿。也可能是文件加密、损坏或页码已失效。</div>')
        self._cur_pixmap = None

    def _show_non_powerpoint_preview(self, ext: str) -> None:
        kind = "Word" if ext == DOCX_EXT else "PDF"
        unit = "段落" if ext == DOCX_EXT else "页面"
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="font-size:15px;font-weight:600">{kind} 内容已全文索引</div>'
            f'<div style="color:#888;font-size:12px;margin-top:8px">可定位命中{unit}<br>'
            f'暂不支持页图预览；点“打开文件”查看原文</div>'
        )
        self._cur_pixmap = None

    def _show_cached_com_preview(self, path: str, page: int, required_edge: int) -> bool:
        """Show only a COM-only cache entry at the requested display resolution."""
        cached = None
        try:
            # Source paths may be cloud/offline locations where even ``stat`` can
            # freeze the GUI. Search results already carry indexed metadata, so
            # cache lookup stays local; source validation remains on the worker.
            cache_key = None
            if self._cur is not None and self._cur.path == path:
                cache_key = renderer_mod.cache_key_for_metadata(
                    path,
                    self._cur.mtime,
                    self._cur.size,
                )
            cached = renderer_mod.find_cached_render(
                path,
                page,
                cache_key=cache_key,
                min_long_edge=required_edge,
            )
        except Exception:  # noqa: BLE001
            cached = None
        if cached is None or not os.path.exists(str(cached)):
            return False
        pm = QPixmap(str(cached))
        if pm.isNull():
            return False
        self._cur_pixmap = pm
        self._stop_spinner()
        self._update_pixmap()
        return True

    def _request_preview(self) -> None:
        if not self._cur:
            return
        if self._search_pending_req is not None:
            return
        if self._defer_preview_if_file_op_active():
            return
        self._preview_deferred_due_to_busy = False
        self._zoom = 1.0
        page = self._view_page
        hits = self._cur.hits or []
        total = self._cur.page_count or 0
        n = len(hits)
        ext = (self._cur.ext or os.path.splitext(self._cur.path)[1]).lower()
        if ext not in PPT_EXTS:
            self._invalidate_preview_request(clear_deferred=False)
            ordn = next((i for i, h in enumerate(hits) if h.page_no == page), None)
            hit_tag = f" · 命中 {ordn + 1}/{n}" if ordn is not None else ""
            unit = "段" if ext == DOCX_EXT else "页"
            self.page_label.setText(
                f"第 {page} / {total} {unit}{hit_tag}"
                if total else f"第 {page} {unit}{hit_tag}"
            )
            nav_enabled = self._active_heavy_op is None and self._search_pending_req is None
            self.prev_btn.setEnabled(nav_enabled and n > 0 and self._hit_idx > 0)
            self.next_btn.setEnabled(nav_enabled and n > 0 and self._hit_idx < n - 1)
            self.goto_btn.setEnabled(False)
            self.goto_btn.setToolTip("Word/PDF 暂不支持自动跳转；可使用“打开文件”")
            self._show_non_powerpoint_preview(ext)
            return
        # 命中页判定与序号已并入下方 ordn 计算（不再单独维护 hit_pages 集合）
        # 椤电爜锛氱 X / 鍏?N 椤碉紙婊氳疆鍙湪鍘熷椤靛簭闂磋嚜鐢辩炕锛涘懡涓〉鍔犳爣璁帮級
        ordn = next((i for i, h in enumerate(hits) if h.page_no == page), None)
        hit_tag = f" \u00b7 \u547d\u4e2d {ordn + 1}/{n}" if ordn is not None else ""
        if total:
            self.page_label.setText(f"\u7b2c {page} / {total} \u9875{hit_tag}")
        else:
            self.page_label.setText(f"\u7b2c {page} \u9875{hit_tag}")
        # 涓?涓嬨€屽懡涓〉銆嶆寜閽細鍦ㄥ懡涓〉涔嬮棿璺?
        nav_enabled = self._active_heavy_op is None and self._search_pending_req is None
        self.prev_btn.setEnabled(nav_enabled and n > 0 and self._hit_idx > 0)
        self.next_btn.setEnabled(nav_enabled and n > 0 and self._hit_idx < n - 1)
        # 缂╃暐鍥鹃珮浜細褰撳墠椤垫濂芥槸鏌愬懡涓〉灏辩偣浜畠
        for b in self._thumb_btns:
            i = b.property("hit_index")
            b.setChecked(i is not None and i < n and hits[i].page_no == page)
        # 娓愯繘寮忛瑙堬細璇ラ〉缂╃暐鍥惧凡缂撳瓨灏辩珛鍗虫斁澶ф樉绀轰綔鍗犱綅锛堢鍑哄唴瀹广€侀伄浣忔覆鏌撶瓑寰咃級锛岄珮娓呮覆鏌?
        # 濂藉悗鍦?_on_rendered 鏃犵紳鏇挎崲銆傚懡涓〉閫氬父宸叉湁缂╃暐鍥撅紙缁撴灉鍗＄墖宸︿晶閭ｅ紶灏辨槸瀹冿級銆?
        preview_edge = self._preview_long_edge()
        cache_hit = self._show_cached_com_preview(
            self._cur.path,
            page,
            preview_edge,
        )
        if not cache_hit:
            self._start_spinner()
        self._req_id += 1
        if cache_hit:
            self._preview_hinted = True
            self._prefetch_neighbors()
        else:
            self._request_render(
                self._req_id,
                self._cur.path,
                page,
                long_edge=preview_edge,
                priority=self._PRIORITY_RIGHT_PREVIEW,
            )

    def _preview_long_edge(self) -> int:
        try:
            vp = self.scroll.viewport().size()
            pixel_ratio = max(1.0, float(self.devicePixelRatioF()))
            edge = int(max(vp.width(), vp.height()) * pixel_ratio * 1.5)
        except Exception:  # noqa: BLE001
            edge = self._PREVIEW_MAX_EDGE
        return max(self._PREVIEW_MIN_EDGE, min(self._PREVIEW_MAX_EDGE, edge))

    def _request_render(self, req_id: int, path: str, page: int, *, long_edge: int, priority: int) -> None:
        try:
            self._render.request(req_id, path, page, cache_key=None, long_edge=long_edge, priority=priority)
        except TypeError:
            try:
                self._render.request(req_id, path, page, cache_key=None, long_edge=long_edge)
            except TypeError:
                self._render.request(req_id, path, page, cache_key=None)

    def _prefetch_render(self, path: str, page: int, *, long_edge: int, priority: int) -> None:
        if not hasattr(self._render, "prefetch"):
            return
        try:
            self._render.prefetch(path, page, long_edge=long_edge, priority=priority)
        except TypeError:
            try:
                self._render.prefetch(path, page, long_edge=long_edge)
            except TypeError:
                self._render.prefetch(path, page)

    def _maybe_prewarm_render(self) -> None:
        if self._closing or not self._owns_render:
            return
        if self._search_pending_req is not None:
            return
        if self.search_box.text().strip():
            return
        if self._cur is not None or self._cur_pixmap is not None:
            return
        prewarm = getattr(self._render, "prewarm", None)
        if callable(prewarm):
            prewarm()

    def _wheel_page(self, delta_y: int) -> None:
        """棰勮鍖烘粴杞細鎸夊師濮嬮〉搴忎笂涓嬬炕椤碉紙鍚戜笂婊?涓婁竴椤碉紝鍚戜笅婊?涓嬩竴椤碉級銆?"""
        if not self._cur:
            return
        if self._preview_interaction_blocked():
            return
        total = self._cur.page_count or 0
        if total <= 0:
            return  # .ppt / 鏈В鏋愶紝椤垫暟鏈煡锛屼笉缈婚〉
        new = self._view_page + (-1 if delta_y > 0 else 1)
        new = max(1, min(total, new))
        if new == self._view_page:
            return
        self._preview_direction = 1 if new > self._view_page else -1
        self._view_page = new
        for i, h in enumerate(self._cur.hits or []):
            if h.page_no == new:
                self._hit_idx = i  # 缈诲埌鍛戒腑椤垫椂鍚屾锛岃涓?涓嬪懡涓〉鎸夐挳鎺ョ画
                break
        self._request_preview()

    def _zoom_by(self, factor: float) -> None:
        if self._preview_interaction_blocked():
            return
        if self._cur_pixmap is None:
            return
        self._zoom = max(1.0, min(5.0, self._zoom * factor))
        # Precision touchpads can emit dozens of Ctrl+wheel events in one
        # gesture. Scaling a 1280px preview up to 5x for every event freezes the
        # GUI; coalesce the burst and render only its final zoom level.
        self._resize_preview_timer.start()

    def _toggle_zoom(self) -> None:
        if self._preview_interaction_blocked():
            return
        if self._cur_pixmap is None:
            return
        self._zoom = 1.0 if self._zoom > 1.0 else 2.0
        self._resize_preview_timer.stop()
        self._update_pixmap()
        self._toast("原尺寸放大 · 再双击还原" if self._zoom > 1.0 else "已适配窗口")

    def _on_rendered(self, req_id: int, png: str) -> None:
        if self._closing:
            return
        if req_id != self._req_id:
            return
        self._stop_spinner()
        if not png or not os.path.exists(png):
            self._show_preview_unavailable()
            return
        pm = QPixmap(png)
        if pm.isNull():
            self._show_preview_unavailable()
            return
        self._cur_pixmap = pm
        self._preview_hinted = True  # 棣栨棰勮宸叉垚鍔燂紝涔嬪悗涓嶅啀鎻愩€屽敜璧?PowerPoint銆?
        self._update_pixmap()
        self._prefetch_neighbors()  # 鍚庡彴棰勬覆鏌撶浉閭?鍛戒腑椤碉紝缈昏繃鍘绘椂缂撳瓨鍛戒腑=鐬棿

    def _prefetch_neighbors(self) -> None:
        """鍚庡彴棰勬覆鏌撳綋鍓嶆枃浠躲€屽叾瀹冨懡涓〉 + 鍓嶅悗椤点€嶁啋 缈昏繃鍘绘椂缂撳瓨鍛戒腑銆佺灛闂村嚭鍥俱€?

        鏂囦欢宸叉墦寮€鐫€锛岄鍙栨瘡椤靛彧鏄瀵煎嚭 ~0.07s锛屼綆浼樺厛銆佽鏂伴瑙堥殢鏃舵姠鍗犲苟浣滃簾
        锛坃request_preview鈫抮ender_worker.request 浼氭竻绌哄緟棰勫彇锛夛紝鏁呭彧棰勫彇浣犲綋鍓嶅仠鐣欓〉鐨勯偦灞呫€?
        """
        if (
            self._cur is None
            or not self._owns_render
            or self._active_heavy_op is not None
            or self._search_pending_req is not None
        ):
            return
        if not hasattr(self._render, "prefetch"):
            return  # 娴嬭瘯娉ㄥ叆鐨?StubRender 鏃犳鏂规硶
        total = self._cur.page_count or 0
        cur = self._view_page
        direction = 1 if self._preview_direction >= 0 else -1
        # First cover ordinary wheel/page turning, then the next search-hit button.
        # A COM export already in progress cannot be interrupted safely, but the
        # worker checks for a real preview between each page.  Keep the runway
        # bounded so rapid result selection never leaves a large speculative tail.
        directional_hits = [
            h.page_no for h in (self._cur.hits or [])
            if (h.page_no - cur) * direction > 0
        ]
        directional_hits.sort(reverse=direction < 0)
        opposite_hits = [
            h.page_no for h in (self._cur.hits or [])
            if (h.page_no - cur) * direction < 0
        ]
        opposite_hits.sort(reverse=direction > 0)
        order: list[int] = [
            cur + direction * step
            for step in range(1, self._NEIGHBOR_PREFETCH_MAX + 1)
        ]
        order += directional_hits
        order += [cur - direction]
        order += opposite_hits
        seen = {cur}
        queued = 0
        for p in order:
            if p in seen or p < 1 or (total and p > total):
                continue
            seen.add(p)
            if queued >= self._NEIGHBOR_PREFETCH_MAX:
                break
            self._prefetch_render(
                self._cur.path,
                p,
                long_edge=self._PREFETCH_EDGE,
                priority=self._PRIORITY_NEIGHBOR_PREFETCH,
            )
            queued += 1

    def _update_pixmap(self) -> None:
        if self._cur_pixmap is None:
            return
        vp = self.scroll.viewport().size()
        fitted_size = QSize(self._cur_pixmap.width(), self._cur_pixmap.height())
        fitted_size.scale(
            max(1, vp.width() - 6),
            max(1, vp.height() - 6),
            Qt.KeepAspectRatio,
        )
        self.image_label.setText("")
        if self._zoom <= 1.0:
            self.scroll.setWidgetResizable(True)
            scaled = self._cur_pixmap.scaled(
                fitted_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
        else:
            self.scroll.setWidgetResizable(False)
            zoomed_size = QSize(
                max(1, int(fitted_size.width() * self._zoom)),
                max(1, int(fitted_size.height() * self._zoom)),
            )
            scaled = self._cur_pixmap.scaled(
                zoomed_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
            self.image_label.resize(scaled.size())

    def maybe_show_welcome(self) -> None:
        """棣栨杩愯鏃跺脊娆㈣繋瑕嗙洊灞傦紙app.py 鍦?win.show() 鍚庤皟鐢級銆?"""
        if not is_first_run() or self._welcome is not None:
            return
        from .welcome_overlay import WelcomeOverlay
        ov = WelcomeOverlay(
            self,
            on_start=self._dismiss_welcome,
            on_pick_theme=self._apply_theme,
            current_theme=self._theme)
        ov.move(0, 0)
        ov.resize(self.size())
        ov.show()
        ov.raise_()
        self._welcome = ov

    def _dismiss_welcome(self) -> None:
        mark_welcomed()
        if self._welcome is not None:
            self._welcome.hide()
            self._welcome.deleteLater()
            self._welcome = None
        QTimer.singleShot(350, self._show_search_coach)  # 璋㈠箷鍚庡紩瀵兼悳绱㈡

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        # Smooth QPixmap scaling is deliberately expensive. Windows emits a
        # resize event for every drag pixel; doing the transform in every event
        # made the whole app stutter at high DPI. Keep the previous frame while
        # dragging and perform one final high-quality scale after the burst.
        if self._cur_pixmap is not None:
            self._resize_preview_timer.start()
        if getattr(self, "_toast_label", None) is not None and self._toast_label.isVisible():
            self._reposition_toast()
        if getattr(self, "_welcome", None) is not None:
            self._welcome.resize(self.size())
        if getattr(self, "_stats_overlay", None) is not None:
            self._stats_overlay.resize(self.size())

    def changeEvent(self, e):  # noqa: N802
        if e.type() == QEvent.ActivationChange and self._cur_item_widget is not None:
            self._cur_item_widget.set_selected(True, self.isActiveWindow())
        super().changeEvent(e)

    # ---------- 閿洏 ----------
    def eventFilter(self, obj, ev):  # noqa: N802
        et = ev.type()
        if et in (
            QEvent.KeyPress,
            QEvent.MouseButtonPress,
            QEvent.MouseButtonDblClick,
            QEvent.Wheel,
            QEvent.TouchBegin,
        ):
            self._note_user_activity()
        if obj is getattr(self, "hotkey_label", None) and et == QEvent.MouseButtonPress:
            cb = self._open_settings_cb  # 状态栏热键标签点击 → 打开设置（#2）
            if callable(cb):
                cb()
            return True
        if et in (QEvent.Wheel, QEvent.MouseButtonDblClick) and obj in (self.image_label, self.scroll.viewport()):
            if et == QEvent.Wheel:
                if ev.modifiers() & Qt.ControlModifier:
                    self._zoom_by(1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)
                else:
                    self._wheel_page(ev.angleDelta().y())
            else:
                self._toggle_zoom()
            return True
        if obj is self.search_box and ev.type() == QEvent.FocusIn and not self.search_box.text():
            self._refresh_history_model()
            if self._history_model.stringList():
                self._completer.complete()
        if obj is self.search_box and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                if self._search_pending_req is not None:
                    return True
                n = self.result_list.count()
                if n:
                    cur = max(0, self.result_list.currentRow())
                    nr = min(n - 1, cur + 1) if k == Qt.Key_Down else max(0, cur - 1)
                    self.result_list.setCurrentRow(nr)
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self._cur:
                    self._act_goto()
                return True
            if k == Qt.Key_Escape:
                if self.search_box.text():
                    self._clear_search_now()
                elif self._to_tray_on_close:
                    self.hide()
                return True
        return super().eventFilter(obj, ev)

    # ---------- 鎵撳紑鍔ㄤ綔 ----------
    def _release_preview_session_before_open(self, path: str) -> None:
        """Synchronously hand off the render COM apartment before opening a PPT.

        This runs inside the existing background file-operation task.  Waiting in
        the GUI thread would freeze the app; calling ``renderer.shutdown`` here
        would be ineffective because the COM state belongs to RenderWorker.
        """
        if os.path.splitext(path)[1].lower() not in PPT_EXTS:
            return
        release = getattr(self._render, "release_session", None)
        if not callable(release):
            return
        if not bool(release(timeout_sec=6.0)):
            raise renderer_mod.PowerPointHandoffBusy(
                "preview renderer did not release its PowerPoint session"
            )
        ready = getattr(renderer_mod, "wait_for_external_open_ready", None)
        if callable(ready) and not bool(ready(timeout_sec=3.0)):
            raise renderer_mod.PowerPointHandoffBusy(
                "headless preview PowerPoint process is still exiting"
            )

    def _open_file_after_preview_release(self, path: str) -> bool | str:
        try:
            self._release_preview_session_before_open(path)
        except renderer_mod.PowerPointHandoffBusy:
            return "handoff_busy"
        return actions.open_file(path)

    def _open_at_page_after_preview_release(self, path: str, page: int):
        try:
            self._release_preview_session_before_open(path)
        except renderer_mod.PowerPointHandoffBusy:
            return "handoff_busy"
        return actions.open_at_page(path, page)

    def _open_file_path(self, path: str) -> None:
        def _after(ok):
            # Do not immediately recreate a hidden preview COM server while the
            # shell-launched user PowerPoint is still registering in the ROT.
            self._preview_deferred_due_to_busy = False
            if ok == "handoff_busy":
                self._toast("预览进程还在安全退出，请稍后再点一次；没有打开到低清会话。")
            elif ok is None:
                self._toast("打开文件时出错了，请稍后重试")
            elif not ok:
                self._toast("文件已移动或删除")

        if self._run_bg(lambda: self._open_file_after_preview_release(path), _after, "open"):
            self._toast("正在打开文件…")

    def _open_folder_path(self, path: str) -> None:
        def _after(ok):
            if ok is None:
                self._toast("打开文件夹时出错了，请稍后重试")
            elif not ok:
                self._toast("找不到所在文件夹")

        if self._run_bg(lambda: actions.open_folder(path), _after, "open"):
            self._toast("正在打开所在文件夹…")

    def _act_open(self) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        if self._cur:
            self._open_file_path(self._cur.path)

    def _act_folder(self) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        if self._cur:
            self._open_folder_path(self._cur.path)

    def _act_copy_path(self) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        if self._cur:
            self._copy_text_with_toast(self._cur.path, "已复制完整路径")

    def _copy_text_with_toast(self, text: str, message: str) -> None:
        QApplication.clipboard().setText(text)
        self._toast(message)

    def _act_copy_page_text(self) -> None:
        """复制已索引文字；文件库读取放后台，VACUUM/写锁也不能冻住按钮。"""
        if self._block_if_search_pending():
            return
        if not self._cur:
            return
        page = self._view_page
        file_id = self._cur.file_id
        path = self._cur.path
        conn_path = _sqlite_file_path(self._conn)
        if conn_path:
            self._page_text_copy_token += 1
            token = self._page_text_copy_token
            task = BackgroundTask(
                lambda conn_path=conn_path, file_id=file_id, page=page:
                self._load_page_text(conn_path, file_id, page),
                "copy-page-text",
            )
            self._bg_tasks.append(task)
            task.done.connect(
                lambda text, token=token, path=path, file_id=file_id, page=page:
                self._on_page_text_loaded(token, path, file_id, page, text)
            )
            task.finished.connect(
                lambda task=task: self._bg_tasks.remove(task) if task in self._bg_tasks else None
            )
            task.start()
            return
        try:
            text = db.get_page_text(self._conn, file_id, page)
        except Exception:  # noqa: BLE001 取文本失败不致命，提示即可
            _log.warning("复制本页文字失败", exc_info=True)
            text = ""
        if text.strip():
            self._copy_text_with_toast(text, f"已复制第 {page} 页文字（{len(text)} 字）")
        else:
            self._toast(f"第 {page} 页没有可复制的文字")

    @staticmethod
    def _load_page_text(conn_path: str, file_id: int, page: int) -> str:
        conn = db.connect(conn_path)
        try:
            return db.get_page_text(conn, file_id, page)
        finally:
            conn.close()

    def _on_page_text_loaded(
        self,
        token: int,
        path: str,
        file_id: int,
        page: int,
        text: object,
    ) -> None:
        if (
            self._closing
            or token != self._page_text_copy_token
            or self._cur is None
            or self._cur.path != path
            or self._cur.file_id != file_id
            or self._view_page != page
        ):
            return
        value = str(text or "")
        if value.strip():
            self._copy_text_with_toast(value, f"已复制第 {page} 页文字（{len(value)} 字）")
        else:
            self._toast(f"第 {page} 页没有可复制的文字")

    def _act_copy_clipboard(self) -> None:
        """澶嶅埗鏂囦欢鏈綋鍒板壀璐存澘锛圵indows CF_HDROP锛夛紝鍙矘璐村埌閭欢 / 鑱婂ぉ / 璧勬簮绠＄悊鍣ㄣ€?"""
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        if not self._cur:
            return
        path = self._cur.path
        self._clipboard_copy_token += 1
        token = self._clipboard_copy_token
        self._check_clipboard_file_exists_bg(path, token)
        self._set_file_clipboard(path)
        self._confirm_file_clipboard(path, token, 4)

    def _check_clipboard_file_exists_bg(self, path: str, token: int) -> None:
        def _after(ok):
            if self._closing or token != self._clipboard_copy_token:
                return
            if ok is False:
                self._toast("文件已移动或删除，剪贴板中的文件可能无法粘贴")

        self._run_bg(lambda: os.path.exists(path), _after, "copy-exists")

    def _set_file_clipboard(self, path: str) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path)])
        QApplication.clipboard().setMimeData(mime)

    def _confirm_file_clipboard(self, path: str, token: int, remaining: int) -> None:
        if self._closing or token != self._clipboard_copy_token:
            return
        md = QApplication.clipboard().mimeData()
        ok = md.hasUrls() and any(u.toLocalFile() == path for u in md.urls())
        if ok:
            self._toast("已复制文件到剪贴板，可粘贴到邮件 / 聊天")
            return
        if remaining <= 0:
            self._toast("剪贴板暂时不可用，请稍后重试")
            return
        self._set_file_clipboard(path)
        QTimer.singleShot(
            50,
            lambda path=path, token=token, remaining=remaining - 1:
                self._confirm_file_clipboard(path, token, remaining),
        )

    def _open_at_page_bg(self, path: str, page: int) -> None:
        def _after(res):
            self._preview_deferred_due_to_busy = False
            if res == "handoff_busy":
                self._toast("预览进程还在安全退出，请稍后再点一次；没有打开到低清会话。")
                return
            if res is None:
                self._toast("打开时出错了，请稍后重试")
                return
            opened, jumped = res
            if not opened:
                self._toast("文件已移动或删除")
            elif not jumped:
                self._toast(f"\u5df2\u6253\u5f00\uff0c\u4f46\u672a\u80fd\u81ea\u52a8\u8df3\u5230\u7b2c {page} \u9875")

        if self._run_bg(
            lambda: self._open_at_page_after_preview_release(path, page),
            _after,
            "open",
        ):
            self._toast(f"\u6b63\u5728\u6253\u5f00\u7b2c {page} \u9875...")

    def _act_goto(self) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        if not self._cur:
            return
        q = self.search_box.text().strip()
        if q:
            history.add_history(q)
            self._refresh_history_model()
        if (self._cur.ext or os.path.splitext(self._cur.path)[1]).lower() not in PPT_EXTS:
            kind = "Word" if (self._cur.ext or "").lower() == DOCX_EXT else "PDF"
            self._open_file_path(self._cur.path)
            self._toast(f"{kind} 暂不支持自动跳转，已按普通方式打开")
            return
        self._open_at_page_bg(self._cur.path, self._view_page)

    def _activate_result_item(self, item: QListWidgetItem | None) -> None:
        if item is not None and self.result_list.currentItem() is not item:
            self.result_list.setCurrentItem(item)
        self._activate_current_result()

    def _activate_current_result(self) -> None:
        now = time.monotonic()
        if now - self._last_result_activate_at < 0.25:
            return
        self._last_result_activate_at = now
        self._act_goto()

    def _on_activate(self, _item) -> None:
        self._activate_result_item(_item)

    def _run_context_menu_action(self, render_gen: int, callback) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        if render_gen != self._render_gen:
            self._toast("结果已更新，请重新打开右键菜单")
            return
        callback()

    def _context_menu(self, pos) -> None:
        if self._block_if_search_pending():
            return
        if self._block_if_file_op_active():
            return
        item = self.result_list.itemAt(pos)
        if item is None:
            return
        idx = item.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        r = self._results[idx]
        render_gen = self._render_gen
        menu = QMenu(self)
        menu.addAction("打开文件", lambda _checked=False, r=r, gen=render_gen:
                       self._run_context_menu_action(gen, lambda: self._open_file_path(r.path)))
        menu.addAction("打开并跳到命中页", lambda _checked=False, r=r, gen=render_gen:
                       self._run_context_menu_action(
                           gen,
                           lambda: self._open_at_page_bg(
                               r.path, r.hits[0].page_no if r.hits else 1)))
        menu.addAction("打开所在文件夹", lambda _checked=False, r=r, gen=render_gen:
                       self._run_context_menu_action(gen, lambda: self._open_folder_path(r.path)))
        if str(r.path).lower().endswith(".pptx"):
            menu.addAction("PPT 瘦身体检", lambda _checked=False, r=r, gen=render_gen:
                           self._run_context_menu_action(gen, lambda: self._open_slim_window(r.path)))
        if self._version_mgr is not None:
            menu.addAction("📜 在版本管理中查看版本历史", lambda _checked=False, r=r:
                           self._open_version_window_for(path=r.path))
        menu.addSeparator()
        menu.addAction("复制完整路径", lambda _checked=False, r=r, gen=render_gen:
                       self._run_context_menu_action(
                           gen,
                           lambda: self._copy_text_with_toast(r.path, "已复制完整路径")))
        menu.addAction("复制文件名", lambda _checked=False, r=r, gen=render_gen:
                       self._run_context_menu_action(
                           gen,
                           lambda: self._copy_text_with_toast(r.name, "已复制文件名")))
        menu.exec(self.result_list.mapToGlobal(pos))

    def _init_toast(self) -> None:
        """涓笅鏂规诞灞傛彁绀猴細涓€娆℃€ф搷浣滃弽棣堜笉鍐嶆薄鏌撶姸鎬佹爮銆?"""
        self._toast_hide_token = 0
        self._toast_label = QLabel(self)
        self._toast_label.setObjectName("toast")
        self._toast_label.setAlignment(Qt.AlignCenter)
        self._toast_label.hide()
        self._toast_fx = QGraphicsOpacityEffect(self._toast_label)
        self._toast_label.setGraphicsEffect(self._toast_fx)
        self._toast_fade = QPropertyAnimation(self._toast_fx, b"opacity", self)
        self._toast_fade.setDuration(160)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

    def _reposition_toast(self) -> None:
        lbl = self._toast_label
        x = (self.width() - lbl.width()) // 2
        y = self.height() - lbl.height() - 64  # 鎮簬鐘舵€佹爮涓婃柟
        lbl.move(max(8, x), max(8, y))

    def _toast(self, msg: str) -> None:
        self._toast_hide_token += 1
        lbl = self._toast_label
        lbl.setText(msg)
        lbl.adjustSize()
        self._reposition_toast()
        lbl.show()
        lbl.raise_()
        self._toast_fade.stop()
        self._toast_fade.setStartValue(self._toast_fx.opacity())
        self._toast_fade.setEndValue(1.0)
        self._toast_fade.start()
        self._toast_timer.start(1800)

    def _hide_toast(self) -> None:
        self._toast_hide_token += 1
        token = self._toast_hide_token
        self._toast_fade.stop()
        self._toast_fade.setStartValue(self._toast_fx.opacity())
        self._toast_fade.setEndValue(0.0)
        self._toast_fade.start()
        QTimer.singleShot(200, lambda token=token: self._finish_hide_toast(token))

    def _finish_hide_toast(self, token: int) -> None:
        if token != self._toast_hide_token:
            return
        try:
            self._toast_label.hide()
        except RuntimeError:
            pass

    # ---------- 鐗堟湰绠＄悊瀛樺湪鎰燂紙P0-1锛氭棩甯搁潤榛樼浘鐗?+ 浠呴娆″憡鐭ワ級 ----------
    def refresh_version_shield(self) -> None:
        """鐘舵€佹爮銆岀増鏈繚鎶ゃ€嶇浘鐗岋細鏄剧ず宸插畧鎶ゆ枃浠舵暟锛屾棩甯搁潤榛樼殑瀛樺湪鎰熴€?"""
        if getattr(self, "version_shield", None) is None or self._version_mgr is None:
            self._version_shield_inflight_token = None
            return
        if (
            self._version_shield_inflight_token is not None
            and self._version_shield_inflight_token == self._version_shield_token
        ):
            return
        self._version_shield_token += 1
        token = self._version_shield_token
        version_mgr = self._version_mgr
        task = BackgroundTask(
            lambda version_mgr=version_mgr: self._load_version_shield_count(version_mgr),
            "version-shield-refresh",
        )
        self._version_shield_inflight_token = token
        self._bg_tasks.append(task)
        task.done.connect(lambda count, token=token: self._on_version_shield_count(token, count))
        task.finished.connect(
            lambda task=task, token=token: self._finish_version_shield_refresh(task, token))
        task.start()

    def _finish_version_shield_refresh(self, task, token: int) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._version_shield_inflight_token == token:
            self._version_shield_inflight_token = None

    def _load_version_shield_count(self, version_mgr) -> int:
        try:
            if hasattr(version_mgr, "summary_stats"):
                return int((version_mgr.summary_stats() or {}).get("protected_docs", 0) or 0)
            if hasattr(version_mgr, "list_docs_details"):
                return len(version_mgr.list_docs_details())
            return len(version_mgr.list_docs())
        except Exception:  # noqa: BLE001
            return 0

    def _on_version_shield_count(self, token: int, count: object) -> None:
        if self._closing or token != self._version_shield_token:
            return
        try:
            n = int(count or 0)
        except (TypeError, ValueError):
            n = 0
        self._apply_version_shield_count(n)

    def _apply_version_shield_count(self, n: int) -> None:
        if n > 0:
            self.version_shield.setText(f"🛡️ 版本保护 · {n}")
            self.version_shield.setToolTip(f"已为 {n} 个你改过的 PPT 自动留底，改崩了能找回")
            self.version_shield.show()
        else:
            self.version_shield.hide()

    def _schedule_version_shield_refresh(self) -> None:
        self._version_shield_token += 1
        if self._closing:
            return
        self._version_shield_refresh_timer.setInterval(self._VERSION_SHIELD_REFRESH_MS)
        self._version_shield_refresh_timer.start()

    def _run_version_shield_refresh(self, token: int) -> None:
        if self._closing or token != self._version_shield_token:
            return
        self.refresh_version_shield()

    def _schedule_detail_dot_refresh(self) -> None:
        self._detail_dot_token += 1
        self._detail_dot_timer.setInterval(self._DETAIL_DOT_DELAY_MS)
        self._detail_dot_timer.start()

    def _run_detail_dot_refresh(self, token: int) -> None:
        if self._closing or token != self._detail_dot_token:
            return
        self._refresh_detail_dot()

    def _refresh_detail_dot(self) -> None:
        """閫変腑鏂囦欢鏈夊巻鍙茬増鏈椂锛岃鎯呮寜閽寒绾㈢偣锛涙湰 session 棣栨鍙戠幇鏃跺懠鍚镐竴娆″紩瀵笺€?"""
        cur = getattr(self, "_cur", None)
        if self._version_mgr is None or cur is None:
            self._detail_dot_inflight_token = None
            self._detail_dot_inflight_path = None
            self._apply_detail_dot(False)
            return
        token = self._detail_dot_token
        path = cur.path
        version_mgr = self._version_mgr
        self._apply_detail_dot(False)
        if (
            self._detail_dot_inflight_token == token
            and self._detail_dot_inflight_path == path
        ):
            return
        task = BackgroundTask(
            lambda path=path, version_mgr=version_mgr: self._detail_has_versions(path, version_mgr),
            "detail-dot-check",
        )
        self._detail_dot_inflight_token = token
        self._detail_dot_inflight_path = path
        self._bg_tasks.append(task)
        task.done.connect(lambda has, token=token, path=path: self._on_detail_dot_checked(token, path, has))
        task.finished.connect(
            lambda task=task, token=token, path=path: self._finish_detail_dot_check(task, token, path))
        task.start()

    def _finish_detail_dot_check(self, task, token: int, path: str) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._detail_dot_inflight_token == token and self._detail_dot_inflight_path == path:
            self._detail_dot_inflight_token = None
            self._detail_dot_inflight_path = None

    def _detail_has_versions(self, path: str, version_mgr) -> bool:
        try:
            if hasattr(version_mgr, "list_versions_details"):
                return bool(version_mgr.list_versions_details(path))
            return bool(version_mgr.list_versions(path))
        except Exception:  # noqa: BLE001
            return False

    def _on_detail_dot_checked(self, token: int, path: str, has: object) -> None:
        cur = getattr(self, "_cur", None)
        if self._closing or token != self._detail_dot_token or cur is None or cur.path != path:
            return
        self._apply_detail_dot(bool(has))

    def _apply_detail_dot(self, has: bool) -> None:
        self._detail_dot_has = bool(has)
        bar = self.detail_panel.tabs.tabBar()
        ver_idx = self.detail_panel.version_tab_index()
        if has and self.detail_panel.tabs.currentIndex() != ver_idx:  # 璇︽儏宸叉墦寮€灏变笉鐢ㄧ孩鐐瑰啀鎻愮ず
            rect = bar.tabRect(ver_idx)
            self._detail_dot.move(rect.right() - 14, rect.top() + 3)
            self._detail_dot.show()
            self._detail_dot.raise_()
            # 棣栨鍙戠幇 + 绐楀彛鍙鏃舵墠鍛煎惛寮曞锛堥殣钘忓埌鎵樼洏鏃朵笉娴垂鍔ㄧ敾锛岀暀鍒颁笅娆″啀璇曪級
            if not getattr(self, "_detail_hint_done", False) and self.isVisible():
                self._detail_hint_done = True
                from .spotlight import attention_pulse
                attention_pulse(bar,
                                color=self._tok.get("acc", "#0A84FF"), cycles=2)
        else:
            self._detail_dot.hide()

    def on_version_snapshot(self, path: str, version_id: str) -> None:
        """鍚庡彴鐣欑増浜嬩欢锛堢粡 VersionBridge 闃熷垪淇″彿锛屽凡鍒囧洖涓荤嚎绋嬶級銆?"""
        self._schedule_version_shield_refresh()
        self._index_file_live(path)
        cur = getattr(self, "_cur", None)
        if cur is not None and cur.path == path:
            self._schedule_detail_update(force=True)
            self._schedule_detail_dot_refresh()
        self._pending_version_intro = True
        self._maybe_show_version_intro()

    def on_content_changed(self, path: str) -> None:
        """Word/PDF 保存事件：只实时并入全文索引，不创建 PPT 版本快照。"""
        self._index_file_live(path)

    def _maybe_show_version_intro(self) -> None:
        """棣栨鐣欑増 + 涓荤獥宸查湶鑴告椂锛屽脊涓€娆¤仛鍏夌伅鍛婄煡銆岀増鏈繚鎶ゃ€嶏紝涔嬪悗姘镐箙闈欓粯銆?"""
        if not getattr(self, "_pending_version_intro", False):
            return
        from ..config import is_version_intro_done, mark_version_intro_done
        if is_version_intro_done():
            self._pending_version_intro = False
            return
        if (not self.isVisible() or self.isMinimized()
                or getattr(self, "_welcome", None) is not None):
            return  # 绐楀彛娌￠湶鑴?/ 娆㈣繋椤佃繕鍦?鈫?绛変笅娆?showEvent 琛ュ脊
        self._show_spotlight(
            self.detail_panel.tabs.tabBar(),
            "已自动给你改过的 PPT 留了底 🛡️\n"
            "改崩了、想找回旧版，点这里「版本」就能一键回到任意历史版本。")
        mark_version_intro_done()
        self._pending_version_intro = False

    def _show_spotlight(self, target, text: str) -> None:
        """缁熶竴寮硅仛鍏夌伅寮曞锛氬厛鍏虫棫鐨勫啀寮规柊鐨勶紝閬垮厤鍙犲姞 / 娉勬紡銆?"""
        old = getattr(self, "_spotlight", None)
        if old is not None:
            try:
                old.hide()
                old.deleteLater()
            except RuntimeError:
                pass  # widget C++ 瀵硅薄宸查攢姣佲€斺€擜ttributeError 绛夌被鍨嬮敊璇晠鎰忎笉鍚?
        from .spotlight import SpotlightOverlay
        self._spotlight = SpotlightOverlay(self.centralWidget(), target, text, tok=self._tok)

    def _show_search_coach(self) -> None:
        """棣栨娆㈣繋椤佃阿骞曞悗锛岃仛鍏夌伅寮曞鎼滅储妗嗭紙涓€鐢熶竴娆★紝闅忔杩庨〉 flag锛夈€?"""
        if not self.isVisible() or self.isMinimized() or self._welcome is not None:
            return
        self._show_spotlight(
            self.search_box,
            "在这里输入你 PPT 里写过的字 ↵\n"
            "记得哪页写过什么，就能搜出它在哪个文件、第几页。")

    # ---------- 绱㈠紩 ----------
    def _index_is_empty(self) -> bool:
        try:
            return db.stats(self._conn, exts=self._enabled_index_exts())["file_count"] == 0
        except Exception:  # noqa: BLE001
            return True

    def _schedule_startup_index_check(self, roots: list[str] | None, workers: int | None) -> None:
        self._startup_index_token += 1
        token = self._startup_index_token
        self._startup_index_check_started_at = time.monotonic()
        self._startup_index_check_decision = "checking"
        self._startup_index_check_error = ""
        conn_path = _sqlite_file_path(self._conn)
        if not conn_path:
            try:
                stats = self._load_status_stats(conn_path)
            except Exception:  # noqa: BLE001
                self._on_startup_index_checked(token, roots, workers, None)
                return
            self._on_startup_index_checked(token, roots, workers, stats)
            return
        task = BackgroundTask(
            lambda conn_path=conn_path: self._load_status_stats(conn_path),
            "startup-index-check",
        )
        self._bg_tasks.append(task)
        task.done.connect(
            lambda payload, token=token, roots=roots, workers=workers:
                self._on_startup_index_checked(token, roots, workers, payload)
        )
        task.finished.connect(
            lambda task=task: self._bg_tasks.remove(task) if task in self._bg_tasks else None)
        task.start()

    def _schedule_known_index_reconcile(self, token: int) -> None:
        """Reconcile only paths already in the DB; never walk whole drives."""
        conn_path = _sqlite_file_path(self._conn)
        if not conn_path:
            self._startup_index_check_error = "known_reconcile_no_db_path"
            return
        task = BackgroundTask(
            lambda conn_path=conn_path, exts=self._enabled_index_exts():
            _scan_known_index_changes(conn_path, supported_exts=exts),
            "startup-known-file-reconcile",
        )
        self._bg_tasks.append(task)
        task.done.connect(
            lambda payload, token=token: self._on_known_index_reconciled(token, payload)
        )
        task.finished.connect(
            lambda task=task: self._bg_tasks.remove(task) if task in self._bg_tasks else None
        )
        task.start()

    def _schedule_full_coverage_scan(self, roots: list[str] | None, reason: str) -> None:
        """Queue a complete but single-worker scan after the startup interaction burst."""
        self._coverage_scan_roots = list(roots) if roots else None
        self._coverage_scan_reason = reason
        self._coverage_scan_timer.setInterval(self._FULL_COVERAGE_DELAY_MS)
        self._coverage_scan_timer.start()

    def _run_scheduled_coverage_scan(self) -> None:
        if self._closing or not self._coverage_scan_reason:
            return
        index_busy = self._indexer is not None and self._indexer.isRunning()
        user_busy = (
            self._search_pending_req is not None
            or self._active_heavy_op is not None
            or (self.isVisible() and bool(self.search_box.text().strip()))
        )
        if index_busy or user_busy:
            self._coverage_scan_timer.setInterval(self._FULL_COVERAGE_RETRY_MS)
            self._coverage_scan_timer.start()
            return
        roots = self._coverage_scan_roots
        self._coverage_scan_roots = None
        self._coverage_scan_reason = ""
        # One parser worker caps automatic coverage at one CPU core. Manual and
        # first-run scans retain their normal parallelism.
        self._starting_automatic_coverage = True
        try:
            self._start_indexing(roots, 1)
        finally:
            self._starting_automatic_coverage = False

    def _on_known_index_reconciled(self, token: int, payload: object) -> None:
        if self._closing or token != self._startup_index_token:
            return
        if not isinstance(payload, dict):
            self._startup_index_check_error = "known_reconcile_failed"
            return
        paths = [p for p in payload.get("paths", []) if isinstance(p, str) and p]
        pending_paths = [
            p for p in payload.get("pending_paths", []) if isinstance(p, str) and p
        ]
        self._startup_known_checked = int(payload.get("checked", 0) or 0)
        self._startup_known_changed = len(paths) + len(pending_paths)
        self._startup_known_remaining = int(payload.get("remaining", 0) or 0)
        for path in paths:
            self._submit_live_index(path)
        self._queue_pending_index_resume(pending_paths)

    def _queue_pending_index_resume(self, paths: list[str]) -> None:
        """Resume interrupted first-build work gradually in this session."""
        queued = set(self._startup_pending_queue)
        for path in paths:
            if path not in queued:
                self._startup_pending_queue.append(path)
                queued.add(path)
        if not self._startup_pending_queue or self._closing:
            return
        if self._startup_pending_timer is None:
            self._startup_pending_timer = QTimer(self)
            self._startup_pending_timer.setInterval(1500)
            self._startup_pending_timer.timeout.connect(self._resume_one_pending_index)
        self._resume_one_pending_index()
        if self._startup_pending_queue and not self._startup_pending_timer.isActive():
            self._startup_pending_timer.start()

    def _resume_one_pending_index(self) -> None:
        if self._closing or not self._startup_pending_queue:
            if self._startup_pending_timer is not None:
                self._startup_pending_timer.stop()
            return
        self._submit_live_index(self._startup_pending_queue.popleft())
        if not self._startup_pending_queue and self._startup_pending_timer is not None:
            self._startup_pending_timer.stop()

    def _on_startup_index_checked(
        self,
        token: int,
        roots: list[str] | None,
        workers: int | None,
        payload: object,
    ) -> None:
        if self._closing or token != self._startup_index_token:
            return
        stats = dict(payload or {}) if isinstance(payload, dict) else {}
        self._startup_index_check_last_ms = (
            (time.monotonic() - self._startup_index_check_started_at) * 1000.0
            if self._startup_index_check_started_at else 0.0
        )
        try:
            file_count = int(stats.get("file_count", 0))
        except (TypeError, ValueError):
            file_count = 0
        try:
            page_count = int(stats.get("page_count", 0))
        except (TypeError, ValueError):
            page_count = 0
        status_counts = stats.get("status_counts") if isinstance(stats.get("status_counts"), dict) else {}
        try:
            pending_count = int(stats.get("pending_count", status_counts.get("pending", 0)))
        except (TypeError, ValueError):
            pending_count = 0
        self._index_rebuild_reason = str(stats.get("index_rebuild_reason") or "")
        try:
            last_completed_scan_at = float(stats.get("last_completed_scan_at", 0) or 0)
        except (TypeError, ValueError):
            last_completed_scan_at = 0.0
        try:
            last_known_reconcile_at = float(stats.get("last_known_reconcile_at", 0) or 0)
        except (TypeError, ValueError):
            last_known_reconcile_at = 0.0
        stored_scan_policy = str(stats.get("scan_policy_version") or "")
        completed_feature_signature = str(
            stats.get("completed_feature_signature") or ""
        )
        from ..scanner import SCAN_POLICY_VERSION

        last_reconcile_at = max(last_completed_scan_at, last_known_reconcile_at)
        self._startup_index_check_last_files = file_count
        self._startup_index_check_last_pages = page_count
        self._startup_index_check_last_pending = pending_count
        if not isinstance(payload, dict):
            self._startup_index_check_error = "stats_unavailable"
        if file_count <= 0:
            self._startup_index_check_decision = "start_scan_rebuild" if self._index_rebuild_reason else "start_scan"
            self._start_indexing(roots, workers)
            return
        if stored_scan_policy != SCAN_POLICY_VERSION:
            self._startup_index_check_decision = "schedule_full_coverage_upgrade"
            self._apply_status_stats(None, stats)
            self._schedule_full_coverage_scan(roots, "scan_policy_upgrade")
            return
        if completed_feature_signature != self._current_index_feature_signature():
            self._startup_index_check_decision = "schedule_full_coverage_feature_change"
            self._apply_status_stats(None, stats)
            self._schedule_full_coverage_scan(roots, "feature_change")
            return
        if time.time() - last_completed_scan_at >= self._FULL_COVERAGE_INTERVAL_SEC:
            self._startup_index_check_decision = "schedule_full_coverage"
            self._apply_status_stats(None, stats)
            self._schedule_full_coverage_scan(roots, "periodic_coverage")
            return
        if pending_count > 0:
            self._startup_index_check_decision = "reconcile_known"
            self._apply_status_stats(None, stats)
            self._schedule_known_index_reconcile(token)
            return
        if time.time() - last_reconcile_at >= self._KNOWN_RECONCILE_INTERVAL_SEC:
            self._startup_index_check_decision = "reconcile_known"
            self._apply_status_stats(None, stats)  # old index stays instantly searchable
            self._schedule_known_index_reconcile(token)
            return
        self._startup_index_check_decision = "use_existing"
        self._apply_status_stats(None, stats)

    def _index_file_live(self, path: str) -> None:
        """watcher 鎹曡幏淇濆瓨 鈫?鎶婅繖涓€涓枃浠跺苟鍏ユ悳绱㈢储寮曪紙鏃犻渶閲嶆壂鍏ㄧ洏锛夈€?
        鐢熶骇锛堟湁鍚庡彴 live 绾跨▼锛夛細浠呭叆闃熷嵆杩斿洖锛?*缁濅笉鍦ㄤ富绾跨▼ parse/鍐欏簱**鈥斺€?
        鍚﹀垯浼氭姠鍚庡彴 IndexWorker 鐨?SQLite 鍐欓攣锛堟渶闀跨瓑 8s锛夋妸 UI 椤舵垚銆屾湭鍝嶅簲銆嶃€?        鏃?live 绾跨▼锛坉o_index=False 娴嬭瘯锛夛細鍚屾鍏滃簳锛屼繚鎸佸彲娴嬨€?        """
        if self._indexer is not None and self._indexer.isRunning():
            self._live_deferred_paths.add(path)
            return
        self._submit_live_index(path)

    def _submit_live_index(self, path: str) -> None:
        if self._live is not None:
            self._live.submit(path)  # 鍚庡彴绾跨▼ parse+鍐欏簱锛屽畬鎴愮粡 indexed 淇″彿鍥炰富绾跨▼
            return
        from .. import indexer
        try:
            ok = indexer.index_single(self._conn, path)
        except Exception:  # noqa: BLE001
            _log.warning("live index failed %s", path, exc_info=True)
            return
        if ok:
            self._after_live_index()

    def _flush_deferred_live_index(self) -> None:
        if self._closing or not self._live_deferred_paths:
            return
        batch_size = min(self._LIVE_FLUSH_BATCH, len(self._live_deferred_paths))
        for _ in range(batch_size):
            path = self._live_deferred_paths.pop()
            self._submit_live_index(path)
        if self._live_deferred_paths and not self._closing:
            QTimer.singleShot(self._LIVE_FLUSH_YIELD_MS, self._flush_deferred_live_index)

    def _on_live_indexed(self, path: str) -> None:
        """鍚庡彴 live 绾跨▼绱㈠紩瀹屼竴涓枃浠讹紙淇″彿宸插垏鍥炰富绾跨▼锛夆啋 鍒锋柊鐘舵€?缁撴灉銆?"""
        self._after_live_index()

    def _after_live_index(self) -> None:
        if self._closing:
            return
        self._index_status_cache = None
        self._live_status_refresh.start()       # 鍚堝苟 watcher 椋庢毚涓嬬殑鐘舵€?DB 鏌ヨ
        self._live_refresh.start()

    def _do_live_refresh(self) -> None:
        """live 绱㈠紩鍘绘姈鍒扮偣锛氭妸椋庢毚鏈熼棿绱Н鐨勬柊鏂囦欢涓€娆℃€у苟鍏ュ綋鍓嶈鍥俱€?"""
        if self._closing:
            return
        if self._search_pending_req is not None and self.search_box.text().strip():
            self._live_refresh_after_search = True
            return
        if self.search_box.text().strip():
            self._do_search()
        elif getattr(self, "dashboard", None) is not None and self._showing_recent:
            self._show_recent(dashboard_force_refresh=True, recent_force_refresh=True)  # 绌烘悳绱㈠仠鍦ㄤ华琛ㄧ洏 鈫?鏂版枃浠跺苟鍏ュ悗鍒锋柊缁熻

    def _start_indexing(self, roots: list[str] | None, workers: int | None) -> bool:
        if hasattr(self, "_coverage_scan_timer"):
            self._coverage_scan_timer.stop()
            self._coverage_scan_roots = None
            self._coverage_scan_reason = ""
        if self._indexer is not None and self._indexer.isRunning():
            self._toast("正在扫描中，请稍候…")
            return False
        from ..scanner import fixed_drives
        if not roots:
            env = os.environ.get("PPTX_FINDER_ROOTS", "").strip()
            if env:
                roots = [r for r in env.split(os.pathsep) if r]
        roots = roots or fixed_drives()
        # 两个低优先级隔离 worker 是“速度/前台流畅”折中上限；显式传入更大值
        # 也不允许应用把八个解析进程同时压到用户的 PowerPoint 上。
        worker_count = max(1, min(2, int(workers))) if workers is not None else 2
        self._indexer = IndexWorker(
            self._db_path,
            roots,
            workers=worker_count,
            background_priority=True,
            supported_exts=self._enabled_index_exts(),
            compute_groups=self._smart_grouping_enabled,
            feature_signature=self._current_index_feature_signature(),
        )
        self._indexer.progress.connect(self._on_index_progress)
        self._indexer.finished_index.connect(self._on_index_done)
        self._indexer.finished.connect(self._flush_deferred_live_index)
        self._index_started_at = time.monotonic()
        self._index_search_ready = False
        self._index_last_done = 0
        self._index_last_total = 0
        self._index_last_current = ""
        self._index_last_summary = None
        self._index_progress_last_ui_at = 0.0
        self._index_progress_last_phase = None
        self._index_rate_ema = 0.0
        self._index_rate_last_done = 0
        self._index_rate_last_at = self._index_started_at
        self._index_status_cache = None
        self.type_rail.hide()  # 避免建库写锁期间在 GUI 线程反复查 type_counts
        self.index_phase_label.setText("升级" if self._index_rebuild_reason else "扫描")
        self.index_phase_label.show()
        self.index_count_label.setText("准备中")
        self.index_count_label.show()
        self.pct_label.clear()
        self.pct_label.hide()
        self.index_bar.setRange(0, 0)
        self.index_bar.show()
        self.status_dot.hide()
        if self._index_rebuild_reason:
            self.status_label.setText("正在升级索引：需要重新整理一次，期间可边扫边搜")
        else:
            self.status_label.setText(f"开始索引：{', '.join(roots)}")
        self._indexer.start()
        return True

    @staticmethod
    def _format_remaining(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        if seconds < 60:
            return f"约 {seconds} 秒"
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"约 {minutes} 分 {sec:02d} 秒"
        hours, minutes = divmod(minutes, 60)
        return f"约 {hours} 小时 {minutes:02d} 分"

    def _update_index_rate(self, done: int, total: int, now: float) -> tuple[float, float | None]:
        if total <= 0:
            return 0.0, None
        if self._index_progress_last_phase != "index":
            self._index_rate_last_done = done
            self._index_rate_last_at = now
            self._index_rate_ema = 0.0
            return 0.0, None
        delta_done = done - self._index_rate_last_done
        delta_t = now - self._index_rate_last_at
        if delta_done > 0 and delta_t > 0.05:
            instant = delta_done / delta_t
            self._index_rate_ema = (
                instant if self._index_rate_ema <= 0
                else self._index_rate_ema * 0.72 + instant * 0.28
            )
            self._index_rate_last_done = done
            self._index_rate_last_at = now
        eta = (
            max(0, total - done) / self._index_rate_ema
            if self._index_rate_ema > 0 and done < total else 0.0
        )
        return self._index_rate_ema, eta

    def _on_index_progress(self, done: int, total: int, cur: str) -> None:
        if self._closing:
            return
        self._status_refresh_token += 1
        self._index_last_done = done
        self._index_last_total = total
        self._index_last_current = cur
        phase = "scan" if total < 0 else "index"
        now = time.monotonic()
        rate, eta = self._update_index_rate(done, total, now)
        force_ui = done >= total > 0 or phase != self._index_progress_last_phase
        if (
            not force_ui
            and self._index_progress_last_ui_at
            and (now - self._index_progress_last_ui_at) * 1000 < self._INDEX_PROGRESS_UI_MS
        ):
            return
        self._index_progress_last_ui_at = now
        self._index_progress_last_phase = phase
        self.status_dot.hide()
        self.type_rail.hide()
        self.index_phase_label.show()
        self.index_count_label.show()
        self.index_bar.show()
        if getattr(self, "_welcome", None) is not None and done > 0:
            self._welcome.update_progress(done)
        preserve_search_status = self._search_pending_req is not None
        if total < 0:
            self.index_bar.setRange(0, 0)
            self.index_phase_label.setText("升级" if self._index_rebuild_reason else "扫描")
            found = re.search(r"发现\s*([\d,]+)", str(cur or ""))
            if found:
                self.index_count_label.setText(f"发现 {found.group(1)}")
            elif done > 0:
                self.index_count_label.setText(f"已整理 {done:,}")
            else:
                self.index_count_label.setText("盘点中")
            self.pct_label.clear()
            self.pct_label.hide()
            self.status_label.setToolTip(str(cur or ""))
            if not preserve_search_status:
                prefix = "升级索引中" if self._index_rebuild_reason else "扫描磁盘中"
                self.status_label.setText(f"{prefix} · 可边扫边搜")
        else:
            self.index_bar.setRange(0, max(1, total))
            self.index_bar.setValue(done)
            self.index_phase_label.setText("升级" if self._index_rebuild_reason else "建库")
            self.index_count_label.setText(f"{done:,} / {total:,}")
            self.pct_label.setText(f"{int(done / max(1, total) * 100)}%")
            self.pct_label.show()
            self.status_label.setToolTip(str(cur or ""))
            if not preserve_search_status:
                speed = f" · {rate:.1f} 个/秒" if rate > 0 else ""
                remaining = (
                    f" · 预计剩余 {self._format_remaining(eta)}"
                    if eta is not None and done < total else ""
                )
                self.status_label.setText(
                    f"正在建库{speed}{remaining} · 前台操作优先"
                )

    def _on_index_done(self, summary: dict) -> None:
        if self._closing:
            return
        self._index_search_ready = True
        self._index_last_summary = dict(summary or {})
        self._index_status_cache = None
        error = str((summary or {}).get("error") or "")
        cancelled = bool(int((summary or {}).get("cancelled", 0) or 0))
        if error or cancelled:
            self.index_bar.hide()
            self.index_phase_label.hide()
            self.index_count_label.hide()
            self.type_rail.hide()
            self._close_type_conn()
            self.pct_label.clear()
            self.pct_label.hide()
            self.status_dot.hide()
            self.status_label.setToolTip("")
            if error:
                self.status_label.setText(
                    f"建库遇到问题，已有结果仍可搜索 · {error}"
                )
            else:
                self.status_label.setText("建库已暂停，已完成的结果仍可搜索")
            return
        completed_signature = str((summary or {}).get("feature_signature") or "")
        if (
            completed_signature
            and completed_signature == self._current_index_feature_signature()
        ):
            set_completed_index_feature_signature(completed_signature)
        if self._index_rebuild_reason:
            try:
                db.delete_meta(self._conn, db.META_INDEX_REBUILD_REASON)
                self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
            self._index_rebuild_reason = ""
        self.index_bar.hide()
        self.index_phase_label.hide()
        self.index_count_label.hide()
        self.type_rail.hide()
        self._close_type_conn()
        self.pct_label.clear()
        self.pct_label.hide()
        self.status_label.setToolTip("")
        celebrate = not getattr(self, "_index_celebrated", False)
        if celebrate:
            self._index_celebrated = True
        self._refresh_status(summary, celebrate=celebrate)
        if not self.search_box.text().strip():
            self._show_recent(dashboard_force_refresh=True, recent_force_refresh=True)  # 绱㈠紩瀹屾垚鍚庡埛鏂版渶杩戯紙鐢ㄦ埛杩樻病寮€濮嬫悳鏃讹紝绾冲叆鏂扮储寮曠殑鏂囦欢锛?
    def _load_status_stats(self, conn_path: str | None) -> dict:
        exts = self._enabled_index_exts()
        if conn_path:
            own = db.connect(conn_path)
            try:
                stats = dict(db.stats(own, exts=exts))
                stats["index_rebuild_reason"] = db.meta_value(own, db.META_INDEX_REBUILD_REASON)
                stats["last_completed_scan_at"] = db.meta_value(own, db.META_LAST_COMPLETED_SCAN_AT, "0")
                stats["last_known_reconcile_at"] = db.meta_value(
                    own, db.META_LAST_KNOWN_RECONCILE_AT, "0")
                stats["scan_policy_version"] = db.meta_value(
                    own, db.META_SCAN_POLICY_VERSION, "")
                stats["last_scan_unreadable_dirs"] = db.meta_value(
                    own, db.META_LAST_SCAN_UNREADABLE_DIRS, "0")
                stats["last_scan_error_examples"] = db.meta_value(
                    own, db.META_LAST_SCAN_ERROR_EXAMPLES, "")
                stats["type_counts"] = db.type_counts(own)
                stats["completed_feature_signature"] = (
                    get_completed_index_feature_signature()
                )
                return stats
            finally:
                own.close()
        stats = dict(db.stats(self._conn, exts=exts))
        stats["index_rebuild_reason"] = db.meta_value(self._conn, db.META_INDEX_REBUILD_REASON)
        stats["last_completed_scan_at"] = db.meta_value(self._conn, db.META_LAST_COMPLETED_SCAN_AT, "0")
        stats["last_known_reconcile_at"] = db.meta_value(
            self._conn, db.META_LAST_KNOWN_RECONCILE_AT, "0")
        stats["scan_policy_version"] = db.meta_value(
            self._conn, db.META_SCAN_POLICY_VERSION, "")
        stats["last_scan_unreadable_dirs"] = db.meta_value(
            self._conn, db.META_LAST_SCAN_UNREADABLE_DIRS, "0")
        stats["last_scan_error_examples"] = db.meta_value(
            self._conn, db.META_LAST_SCAN_ERROR_EXAMPLES, "")
        stats["type_counts"] = db.type_counts(self._conn)
        stats["completed_feature_signature"] = get_completed_index_feature_signature()
        return stats

    def _refresh_status(self, summary: dict | None = None, *, celebrate: bool = False) -> None:
        payload_summary = dict(summary or {}) if summary else None
        if (
            payload_summary is None
            and not celebrate
            and self._status_refresh_inflight_token is not None
            and self._status_refresh_inflight_token == self._status_refresh_token
        ):
            return
        self._status_refresh_token += 1
        token = self._status_refresh_token
        conn_path = _sqlite_file_path(self._conn)
        if not conn_path:
            try:
                stats = self._load_status_stats(conn_path)
            except Exception as e:  # noqa: BLE001
                self._apply_status_error(token, e)
                return
            self._on_status_stats_loaded(token, payload_summary, celebrate, {"stats": stats})
            return
        task = BackgroundTask(
            lambda conn_path=conn_path: {"stats": self._load_status_stats(conn_path)},
            "index-status-refresh",
        )
        self._status_refresh_inflight_token = token
        self._bg_tasks.append(task)
        task.done.connect(
            lambda payload, token=token, summary=payload_summary, celebrate=celebrate:
                self._on_status_stats_loaded(token, summary, celebrate, payload)
        )
        task.finished.connect(
            lambda task=task, token=token: self._finish_status_refresh(task, token))
        task.start()

    def _finish_status_refresh(self, task, token: int) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.remove(task)
        if self._status_refresh_inflight_token == token:
            self._status_refresh_inflight_token = None

    def _on_status_stats_loaded(self, token: int, summary: dict | None, celebrate: bool, payload: object) -> None:
        if self._closing or token != self._status_refresh_token:
            return
        if not isinstance(payload, dict) or "stats" not in payload:
            self._apply_status_error(token, RuntimeError("stats unavailable"))
            return
        stats = dict(payload.get("stats") or {})
        self._apply_status_stats(summary, stats)
        if celebrate:
            try:
                n = int(stats.get("file_count", 0))
            except (TypeError, ValueError):
                n = 0
            if (
                n > 0
                and self._search_pending_req is None
                and self.isVisible()
                and not self.isMinimized()
            ):
                self._toast(f"✓ 已整理好 {n} 个 PPT，搜搜看你写过的字吧")

    def _apply_status_stats(self, summary: dict | None, stats: dict) -> None:
        self._index_status_cache = dict(stats)
        self._index_status_cache_at = time.monotonic()
        if self._search_pending_req is not None:
            return
        # \u5c31\u7eea\u6001\uff08\u8bbe\u8ba1 F\uff09\uff1a\u300c\u7d22\u5f15\u5c31\u7eea\uff1aN \u4e2a\u6587\u4ef6 \u00b7 M \u9875\u300d+ \u5404\u7c7b\u578b\u5206\u5e03\uff0c\u66ff\u6389\u65e7\u7684\u300c\u5f85\u8865\u5efa/\u66f4\u65b0/\u79fb\u9664\u300d\u9ed1\u8bdd
        per_ext = stats.get("type_counts") or {}
        parts = []
        for label, bucket_exts, _color in self._enabled_type_buckets():
            total = sum(per_ext.get(e.lower(), (0, 0))[1] for e in bucket_exts)
            if total > 0:
                parts.append(f"{label} {total:,}")
        dist = (" \u00b7 " + " \u00b7 ".join(parts)) if parts else ""
        try:
            unreadable = int(
                (summary or {}).get(
                    "unreadable_dirs",
                    stats.get("last_scan_unreadable_dirs", 0),
                )
                or 0
            )
        except (TypeError, ValueError):
            unreadable = 0
        coverage_warning = (
            f" · ⚠ {unreadable} 个文件夹无权限，可能有遗漏"
            if unreadable else ""
        )
        self.status_dot.show()
        self.status_label.setText(
            f"\u7d22\u5f15\u5c31\u7eea\uff1a{stats.get('file_count', 0)} \u4e2a\u6587\u4ef6 \u00b7 {stats.get('page_count', 0)} \u9875{dist}{coverage_warning}"
       )
        examples = (summary or {}).get(
            "scan_error_examples",
            stats.get("last_scan_error_examples", ""),
        )
        if isinstance(examples, (list, tuple)):
            examples = "\n".join(str(path) for path in examples)
        else:
            examples = str(examples or "")
        self.status_label.setToolTip(
            "以下目录本轮无法读取：\n" + examples
            if unreadable and examples else ""
        )
        # 常驻「上次索引 HH:mm」：上次全量索引完成时间，提示数据新鲜度
        try:
            last_scan_at = float(stats.get("last_completed_scan_at", 0) or 0)
        except (TypeError, ValueError):
            last_scan_at = 0.0
        if last_scan_at > 0:
            last_dt = datetime.datetime.fromtimestamp(last_scan_at)
            fmt = "%H:%M" if last_dt.date() == datetime.datetime.now().date() else "%m-%d %H:%M"
            self.last_index_label.setText(f"上次索引 {last_dt.strftime(fmt)}")
            self.last_index_label.show()
        else:
            self.last_index_label.hide()

    def _apply_status_error(self, token: int, error: Exception) -> None:
        if self._closing or token != self._status_refresh_token:
            return
        if self._search_pending_req is not None:
            return
        self.status_dot.hide()
        self.status_label.setText(f"数据库读取异常：{error}")

    # ---------- 鐢熷懡鍛ㄦ湡 ----------
    def force_quit(self) -> None:
        """鐪熸閫€鍑猴紙缁曡繃鎵樼洏鏈€灏忓寲锛夛紝璁╂洿鏂?helper 鎺ョ鏂囦欢鏇挎崲 + 閲嶅惎銆?

        close() 瑙﹀彂 _shutdown() 姝ｅ父鏀跺熬绾跨▼骞堕噴鏀?PowerPoint COM/鏂囦欢鍙ユ焺锛?
        闅忓悗 QApplication.quit() 纭繚杩涚▼閫€鍑猴紙鍗充娇鎵樼洏鍥炬爣椹荤暀锛夛紝helper 鐨?
        Wait-Process 鎵嶄細杩斿洖銆佺户缁浛鎹€?
        """
        self._to_tray_on_close = False
        self.close()
        QApplication.quit()

    def closeEvent(self, e):  # noqa: N802
        if self._to_tray_on_close:
            e.ignore()
            self.hide()
            return
        self._shutdown()
        e.accept()

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        # The updater exits through ``force_quit`` instead of the tray action.
        # Keep optional watchers/version reconciliation tied to the window's
        # single shutdown path so an update can never leave background threads
        # alive while the helper waits for this process to disappear.
        feature_runtime = getattr(self, "_feature_runtime", None)
        if feature_runtime is not None:
            try:
                feature_runtime.stop()
            except Exception:  # noqa: BLE001 best-effort process cleanup
                _log.exception("feature runtime did not stop cleanly")
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except RuntimeError:
                pass
        self._render_gen += 1
        self._close_type_conn()  # 关分类型计数用的只读连接
        if self._search_worker is not None:
            self._search_worker.stop()
            self._search_worker.wait(self._SEARCH_SHUTDOWN_WAIT_MS)
        # 增量更新的检查/下载线程：显式 stop+wait+terminate，防「QThread 运行中被析构」崩溃
        # （它们也在 _bg_tasks 里走 light 预算，但那只 wait 不 terminate，下载>1s 关窗会崩）
        if getattr(self, "_updater", None) is not None:
            sd = getattr(self._updater, "shutdown", None)
            if callable(sd):
                sd()
        light_elapsed_ms = 0.0
        # Cancel tasks that have not acquired a background slot yet. Running
        # file operations remain untouched and still receive their normal wait.
        for t in list(self._bg_tasks):
            stop = getattr(t, "stop", None)
            if callable(stop):
                try:
                    stop()
                except RuntimeError:
                    pass
        for t in list(self._bg_tasks):
            wait = getattr(t, "wait", None)
            if callable(wait):
                timeout_ms = self._bg_task_shutdown_wait_ms(t)
                if not self._is_heavy_bg_task(t):
                    remaining_ms = max(0, self._BG_LIGHT_SHUTDOWN_TOTAL_WAIT_MS - int(light_elapsed_ms))
                    timeout_ms = min(timeout_ms, remaining_ms)
                    before = time.monotonic()
                    wait(timeout_ms)
                    light_elapsed_ms += max(0.0, (time.monotonic() - before) * 1000.0)
                else:
                    wait(timeout_ms)
        if self._live is not None:
            self._live.stop()
            self._live.wait(3000)
        if self._indexer is not None:
            self._indexer.stop()
            self._indexer.wait(5000)
        if self._owns_render:
            self._render.stop()
        if self._owns_render:
            if not self._render.wait(3000):
                abort = getattr(self._render, "abort_inflight", None)
                if callable(abort):
                    try:
                        abort()
                    except Exception:  # noqa: BLE001 best-effort exit cleanup
                        pass
                if not self._render.wait(5000):
                    _log.error("render worker did not stop after isolated renderer abort")

    def _bg_task_shutdown_wait_ms(self, task) -> int:
        if self._is_heavy_bg_task(task):
            return self._BG_HEAVY_SHUTDOWN_WAIT_MS
        return self._BG_LIGHT_SHUTDOWN_WAIT_MS

    def _is_heavy_bg_task(self, task) -> bool:
        label = getattr(task, "_label", "")
        return label in self._BG_HEAVY_LABELS
