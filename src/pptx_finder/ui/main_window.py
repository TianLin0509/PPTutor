"""涓荤獥鍙ｏ細鍙屼富棰?+ 绮捐嚧缁撴灉椤癸紙鐒︾偣鍙屾€?鍗婇€忔槑楂樹寒/瀛楅噸灞傜骇锛? P0 浜や簰
锛堝嵆鏃舵悳绱?/ 鍏ㄩ敭鐩樺鑸?/ 鍛戒腑椤电缉鐣ュ浘鏉?/ 绱㈠紩杩涘害鏉★級銆?

鍙祴璇曟€э細conn 涓?render_worker 鍙敞鍏ワ紱do_index=False 鏃朵笉鍚姩纾佺洏绱㈠紩銆?
"""
from __future__ import annotations

import datetime
import html
import json
import logging
import os

import sys
import time

from PySide6.QtCore import QEvent, QMimeData, QPropertyAnimation, Qt, QStringListModel, QTimer, QUrl
from PySide6.QtGui import QColor, QCursor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
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

from .. import actions, db, history, search as search_mod, updater, __version__
from ..config import (
    data_dir, db_path as cfg_db_path, get_hotkey, get_theme, is_first_run,
    mark_welcomed, set_theme as cfg_set_theme, update_base_url,
)
from ..models import FileResult
from ..query_explain import explain_query, mode_label, suggestion_keys
from . import theme
from .bg_task import BackgroundTask
from .index_worker import IndexWorker
from .live_indexer import LiveIndexer
from .path_helpers import ensure_pptx_suffix
from .render_worker import RenderWorker
from .search_worker import SearchWorker
from .thumb_worker import ThumbWorker
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
    pen = QPen(QColor(color), 1.7)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    draw(p)
    p.end()
    return QIcon(pm)


def _icon_search() -> QIcon:
    return _make_icon(lambda p: (p.drawEllipse(3, 3, 8, 8), p.drawLine(10, 10, 15, 15)))


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


def _app_logo() -> QPixmap:
    """鍝佺墝 logo锛歅PTutor 鍚夌ゥ鐗╋紙瀛﹀＋甯?+ 鎼滅储/PPT锛夛紝鍔犺浇鎵撳寘鍐?assets/logo.png銆?"""
    pm = QPixmap(_asset_path("logo.png"))
    if not pm.isNull():
        return pm.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    fb = QPixmap(28, 28)
    fb.fill(Qt.transparent)
    return fb


def _load_theme() -> str:
    # 主题持久化集中在 config.ui.json（与热键等共用一个文件，合并写不互相清键）
    return get_theme("cloud")


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


def _mode_key_from_text(mode: str) -> str:
    if mode in {"filename", "仅文件名"} or "文件名" in mode:
        return "filename"
    if mode in {"content", "仅内容"} or "内容" in mode:
        return "content"
    return "all"


def _empty_suggestions(query: str, mode: str) -> list[str]:
    """闆剁粨鏋滄椂閫傜敤鐨勮ˉ鏁戝缓璁?key锛涗繚鐣欑粰娴嬭瘯鍜屾棫璋冪敤锛屽疄闄呴€昏緫璧?query_explain銆?"""
    return suggestion_keys(query, _mode_key_from_text(mode))


def _sort_results(results: list, key: str) -> list:
    """缁撴灉鎺掑簭锛歳elevance 淇濇寔鍘熷簭 / recent 鎸?mtime 闄嶅簭 / name 鎸夋枃浠跺悕鍗囧簭銆?"""
    if key == "recent":
        return sorted(results, key=lambda r: r.mtime, reverse=True)
    if key == "name":
        return sorted(results, key=lambda r: r.name.lower())
    return list(results)


def _time_bucket(mtime: float, now_ts: float) -> str:
    """鎸?mtime 褰掑叆鏃堕棿妗讹細浠婂ぉ / 鏄ㄥぉ / 鏈懆 / 鏈湀 / 鏇存棭銆?"""
    now = datetime.datetime.fromtimestamp(now_ts)
    try:
        dt = datetime.datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return "更早"
    d = (now.date() - dt.date()).days
    if d <= 0:
        return "今天"
    if d == 1:
        return "昨天"
    if d < 7:
        return "本周"
    if d < 30:
        return "本月"
    return "更早"


def group_by_time(results: list, now_ts: float) -> list:
    """鎸?mtime 鍒嗘椂闂存《锛屼繚鎸佽緭鍏ラ『搴忋€傝繑鍥?[(label, [items]), ...]銆?"""
    buckets: dict[str, list] = {}
    order: list[str] = []
    for r in results:
        label = _time_bucket(r.mtime, now_ts)
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(r)
    return [(label, buckets[label]) for label in order]


def _page_bucket(pc: int) -> str:
    if pc <= 10:
        return "1-10"
    if pc <= 30:
        return "10-30"
    return "30+"


def _folder_of(path: str) -> str:
    d = os.path.basename(os.path.dirname(path))
    return d or path


def _facet_type(r) -> str:
    return "pptx" if (r.ext or "").lower() == ".pptx" else "ppt"


def facet_counts(results: list, now_ts: float) -> dict:
    """鎸夌淮搴﹁仛鍚堟暟閲忥細time/type/page/folder 鈫?[(bucket, count)]锛堜繚鎸佸嚭鐜伴『搴忥級銆?"""
    dims: dict[str, dict] = {"time": {}, "type": {}, "page": {}, "folder": {}}

    def bump(d, k):
        d[k] = d.get(k, 0) + 1

    for r in results:
        bump(dims["time"], _time_bucket(r.mtime, now_ts))
        bump(dims["type"], _facet_type(r))
        bump(dims["page"], _page_bucket(r.page_count or 0))
        bump(dims["folder"], _folder_of(r.path))
    return {k: list(v.items()) for k, v in dims.items()}


def facet_filter(results: list, filters: dict, now_ts: float) -> list:
    """澶氱淮 AND 杩囨护锛涙煇缁村害鏈€?涓嶉檺璇ョ淮搴︺€?"""
    def ok(r):
        if filters.get("time") and _time_bucket(r.mtime, now_ts) not in filters["time"]:
            return False
        if filters.get("type") and _facet_type(r) not in filters["type"]:
            return False
        if filters.get("page") and _page_bucket(r.page_count or 0) not in filters["page"]:
            return False
        if filters.get("folder") and _folder_of(r.path) not in filters["folder"]:
            return False
        return True
    return [r for r in results if ok(r)]


def _thumb_placeholder(tok: dict) -> QPixmap:
    """缂╃暐鍥惧崰浣嶏細骞荤伅鐗囨牱瀛愶紙椤堕儴鑹叉潯 + 鍐呭绾匡級锛岀湡瀹炲浘娓叉煋濂藉悗鏇挎崲銆?"""
    pm = QPixmap(74, 55)
    pm.fill(QColor(tok["field"]))
    p = QPainter(pm)
    p.fillRect(0, 0, 74, 13, QColor(tok["bd2"]))
    p.setPen(QPen(QColor(tok["ink4"]), 2))
    p.drawLine(9, 27, 52, 27)
    p.drawLine(9, 37, 40, 37)
    p.end()
    return pm


class ResultItem(QWidget):
    """鍗曟潯缁撴灉鍗＄墖锛氬乏缂╃暐鍥?棣栭〉/鍛戒腑椤? + 鍙?鏂囦欢鍚?+ 鍛戒腑椤佃兌鍥?+ 楂樹寒鐗囨 + mtime)銆?"""

    def __init__(self, r: FileResult, tok: dict, hlcss: str, ginfo: dict | None = None,
                 on_toggle_group=None):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._tok = tok
        self._sel = False
        self.path = r.path
        self.thumb_page = r.hits[0].page_no if r.hits else 1
        # 版本组：ginfo 为组主卡时带 count（折叠起来的历史版本数）；为成员行时 member=True
        self._gid = ginfo.get("gid") if ginfo else None
        self._exp_btn = None  # 版本组展开器按钮（仅组主卡有），供就地切换文字 ▾/▴
        is_member = bool(ginfo and ginfo.get("member"))
        vcount = int(ginfo.get("count", 0)) if ginfo else 0

        outer = QHBoxLayout(self)
        outer.setContentsMargins(11 + (24 if is_member else 0), 9, 12, 9)  # 历史版本行左缩进，视觉归属上方组主卡
        outer.setSpacing(11)
        self._thumb = QLabel()
        self._thumb.setObjectName("cardThumb")
        self._thumb.setFixedSize(74, 55)
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setPixmap(_thumb_placeholder(tok))
        outer.addWidget(self._thumb, 0, Qt.AlignTop)
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
        row.addWidget(fn, 1)

        if r.hits:
            for h in r.hits[:3]:
                pg = QLabel(f"P{h.page_no}")
                pg.setStyleSheet(
                    f"font-size:11px;font-weight:700;color:{tok['acc']};"
                    f"background:rgba({tok['hl_r']},{tok['hl_g']},{tok['hl_b']},0.15);"
                    "border-radius:6px;padding:1px 7px;")
                row.addWidget(pg, 0)
        elif r.name_hit:
            nh = QLabel("\u6587\u4ef6\u540d\u547d\u4e2d")
            nh.setStyleSheet(
                f"font-size:10.5px;font-weight:700;color:{tok['grn']};"
                f"border:1px solid {tok['bd2']};border-radius:6px;padding:1px 7px;background:transparent;")
            row.addWidget(nh, 0)
        if r.status == "filename_only":
            ext = QLabel(".ppt")
            ext.setStyleSheet(f"font-size:10px;color:{tok['ink4']};border:1px solid {tok['bd2']};border-radius:5px;padding:1px 6px;background:transparent;")
            row.addWidget(ext, 0)
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
            t = QLabel(tm)
            t.setStyleSheet(f"font-size:11px;color:{tok['ink3']};background:transparent;")
            lay.addWidget(t)
        outer.addLayout(lay, 1)
        self._apply("normal", True)

    def set_thumbnail(self, pm: QPixmap) -> None:
        if pm is not None and not pm.isNull():
            self._thumb.setPixmap(pm.scaled(74, 55, Qt.KeepAspectRatio, Qt.SmoothTransformation))

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
    _AUTO_PREVIEW_DELAY_MS = 180
    _UI_LOOP_INTERVAL_MS = 250
    _UI_LOOP_SLOW_GAP_MS = 250
    _RECENT_CACHE_MS = 1000
    _LIVE_FLUSH_BATCH = 64
    _LIVE_FLUSH_YIELD_MS = 1
    _DEFERRED_LIVE_SEARCH_YIELD_MS = 1
    _LIVE_STATUS_REFRESH_MS = 250
    _DETAIL_UPDATE_DELAY_MS = 80
    _DETAIL_DOT_DELAY_MS = 80
    _VERSION_SHIELD_REFRESH_MS = 250
    _INDEX_PROGRESS_UI_MS = 100
    _INDEX_STATUS_CACHE_MS = 1000
    _BG_LIGHT_SHUTDOWN_WAIT_MS = 250
    _BG_LIGHT_SHUTDOWN_TOTAL_WAIT_MS = 1000
    _SEARCH_SHUTDOWN_WAIT_MS = 500
    _BG_HEAVY_SHUTDOWN_WAIT_MS = 3000
    _BG_HEAVY_LABELS = {"open", "restore", "export", "version-restore", "version-export", "version-recover"}

    def __init__(self, conn=None, render_worker=None, thumb_worker=None, version_mgr=None,
                 do_index=True, roots: list[str] | None = None, workers: int | None = None):
        super().__init__()
        self.setWindowTitle(f"PPTutor · PPT 查询助手   v{__version__}")
        self.setWindowIcon(QIcon(_asset_path("logo.png")))  # 绐楀彛鏍囬/浠诲姟鏍忓浘鏍?
        self.resize(1180, 760)
        self._title_h = 40  # 鑷粯鐜荤拑鏍囬鏍忛珮搴︼紙nativeEvent 鎷栧姩鍖?缂╂斁杈瑰垽瀹氱敤锛?
        self.setWindowFlag(Qt.FramelessWindowHint, True)  # 鏃犺竟妗?鈫?鑷粯鐜荤拑鏍囬鏍?

        self._theme = _load_theme()
        self._tok = theme.tok(self._theme)
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
        self._detail_update_token = 0
        self._detail_update_force = False
        self._detail_load_inflight_token: int | None = None
        self._detail_load_inflight_path: str | None = None
        self._detail_load_inflight_file_id: int | None = None
        self._detail_dot_token = 0
        self._detail_dot_inflight_token: int | None = None
        self._detail_dot_inflight_path: str | None = None
        self._detail_hint_token = 0
        self._detail_hint_inflight_token: int | None = None
        self._detail_hint_inflight_path: str | None = None
        self._recent_load_token = 0
        self._status_refresh_token = 0
        self._empty_status_token = 0
        self._empty_status_inflight_token: int | None = None
        self._empty_status_inflight_mode: str | None = None
        self._startup_index_token = 0
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
        self._hit_idx = 0
        self._view_page = 1  # 褰撳墠棰勮椤碉紙鍘熷椤靛簭锛屾粴杞彲鑴辩鍛戒腑椤佃嚜鐢辩炕锛?
        self._req_id = 0
        self._cur_pixmap: QPixmap | None = None
        self._preview_provisional = False  # 褰撳墠棰勮鏄惁涓虹缉鐣ュ浘鍗犱綅锛堥珮娓呮湭鍒帮級
        self._zoom = 1.0  # 棰勮缂╂斁锛?.0=閫傞厤绐楀彛锛?1 鏀惧ぇ鐪嬬粏鑺?
        self._to_tray_on_close = False
        self._thumb_btns: list[QToolButton] = []
        # 版本组折叠（#1）：默认折叠同一 MinHash 组的历史副本为一条，点「N 个历史版本」就地展开
        self._expanded_groups: set[int] = set()              # 当前已展开的 group_id
        self._group_others: dict[int, list] = {}             # group_id -> [(idx, FileResult), ...] 隐藏的历史版本
        self._group_primary_item: dict[int, QListWidgetItem] = {}   # group_id -> 组主卡列表项
        self._group_member_items: dict[int, list] = {}       # group_id -> 已插入的成员列表项（折叠时移除）
        self._open_settings_cb = None                        # app.py 注入：状态栏热键标签点击 → 打开设置（#2）

        self._render = render_worker or RenderWorker(self)
        self._render.rendered.connect(self._on_rendered)
        self._owns_render = render_worker is None
        if self._owns_render:
            self._render.start()
            # 鍚姩鍚?1.5s 鍚庡彴闈欓粯棰勭儹 PowerPoint COM锛堢敤鎴蜂笉鎰熺煡锛夛紝棣栨棰勮鍏嶅喎鍚姩 ~1.5s銆?
            # 浠呯湡瀹炶繍琛屾€侊紙do_index锛夐鐑紱娴嬭瘯 do_index=False 涓嶆棤璋撴媺璧?PowerPoint銆?
            if do_index:
                QTimer.singleShot(1500, self._maybe_prewarm_render)

        self._thumb = thumb_worker or ThumbWorker(self)
        self._thumb.thumb_rendered.connect(self._on_thumb)
        self._owns_thumb = thumb_worker is None
        if self._owns_thumb and do_index:
            self._thumb.start()  # 娴嬭瘯 do_index=False 涓嶈捣鐪熸覆鏌撶嚎绋?
        self._thumb_cache: dict[tuple[str, int], QPixmap] = {}
        self._thumb_items: dict[tuple[str, int], ResultItem] = {}
        self._version_mgr = version_mgr  # 鐗堟湰绠＄悊锛坅pp.py 娉ㄥ叆锛岃鎯呴潰鏉跨敤锛涘彲 None锛?
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
        self._live_refresh.setInterval(600)
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

        self._build_ui()
        self._apply_theme(self._theme, persist=False)
        if self._async_search:
            self._search_worker = SearchWorker(self._db_path, self)
            self._search_worker.searched.connect(self._on_search_done)
            self._search_worker.start()

        self._indexer: IndexWorker | None = None
        # 瀹炴椂绱㈠紩鍚庡彴绾跨▼锛氫繚瀛樹簨浠朵笉鍦ㄤ富绾跨▼ parse/鍐欏簱锛堥槻 UI 鍐荤粨锛夈€?
        # do_index=False 鐨勬祴璇曟棤姝ょ嚎绋嬶紝璧?_index_file_live 鐨勫悓姝ュ厹搴曘€?
        self._live: LiveIndexer | None = None
        if do_index:
            self._live = LiveIndexer(self._db_path)
            self._live.indexed.connect(self._on_live_indexed)
            self._live.start()
        if do_index:
            self._schedule_startup_index_check(roots, workers)
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
        return lines

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = AuroraCentral(self)  # 鑷粯鏋佸厜搴曪紙璇?self._tok锛夛紝objectName 浠?"central"
        self._central = central
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_glass_title())  # 鏃犺竟妗嗙獥鍙ｇ殑鑷粯鐜荤拑鏍囬鏍?

        top = QWidget()
        top.setObjectName("topBar")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(13, 12, 13, 11)
        tl.setSpacing(0)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        logo = QLabel()
        logo.setObjectName("appLogo")
        logo.setPixmap(_app_logo())
        logo.setToolTip("PPTutor")
        bar.addWidget(logo)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText('输入你记得的文字 / 文件名…（多词空格=同时含，"引号"=精确短语）')
        self.search_box.setMinimumHeight(42)
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
        bar.addWidget(self.search_box, 1)
        self.mode = QComboBox()
        self.mode.addItems(["全部", "仅文件名", "仅内容"])
        self.mode.setMinimumHeight(42)
        self.mode.currentIndexChanged.connect(self._do_search)
        bar.addWidget(self.mode)
        self.facet_btn = QPushButton("筛选")
        self.facet_btn.setObjectName("ghost")
        self.facet_btn.setMinimumHeight(42)
        self.facet_btn.setCheckable(True)
        self.facet_btn.setToolTip("按时间 / 类型 / 页数 / 文件夹筛选")
        self.facet_btn.clicked.connect(self._toggle_facet)
        bar.addWidget(self.facet_btn)
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("ghost")
        self.theme_btn.setMinimumHeight(42)
        self.theme_btn.clicked.connect(self._show_theme_menu)
        bar.addWidget(self.theme_btn)
        self.detail_btn = QPushButton("详情")
        self.detail_btn.setObjectName("ghost")
        self.detail_btn.setMinimumHeight(42)
        self.detail_btn.setCheckable(True)
        self.detail_btn.setToolTip("显示/隐藏 版本时间线 · 大纲 · 文件信息")
        self.detail_btn.clicked.connect(self._toggle_detail)
        bar.addWidget(self.detail_btn)
        self._detail_dot = QLabel("●", self.detail_btn)
        self._detail_dot.setObjectName("navDot")
        self._detail_dot.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._detail_dot.hide()
        tl.addLayout(bar)
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
        self.result_count = QLabel("")
        self.result_count.setObjectName("listHead")
        hr.addWidget(self.result_count, 1)
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("sortCombo")
        self.sort_combo.addItems(["相关度", "最近修改", "文件名"])
        self.sort_combo.setToolTip("结果排序方式")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        hr.addWidget(self.sort_combo, 0)
        self.list_head.hide()
        ll.addWidget(self.list_head)
        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.currentItemChanged.connect(self._on_select)
        self.result_list.itemActivated.connect(self._on_activate)
        self.result_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._context_menu)
        ll.addWidget(self.result_list, 1)
        self._build_empty_hint(ll)
        # 鍒楄〃鍖虹敤 QStackedWidget 鍖呫€岀粨鏋滃垪琛?left銆嶄笌銆屼华琛ㄧ洏棣栧睆銆嶄簩閫変竴鍒囨崲锛?
        # left锛堝惈 result_list 鍙婂叏閮ㄤ俊鍙风粦瀹氾級鍘熸牱淇濈暀锛屼粎澶氫竴灞?stack 瀹瑰櫒銆?
        self._list_stack = QStackedWidget()
        self._list_stack.setObjectName("listStack")
        self._list_stack.addWidget(left)                  # index 0锛氱粨鏋滃垪琛ㄥ尯
        self.dashboard = DashboardView(self)
        self._list_stack.addWidget(self.dashboard)        # index 1锛氫华琛ㄧ洏棣栧睆
        self.facet_panel = FacetPanel(self._tok)
        self.facet_panel.filters_changed.connect(self._apply_facet)
        self.facet_panel.hide()
        split.addWidget(self.facet_panel)
        split.addWidget(self._list_stack)
        split.addWidget(self._build_preview())
        # 璇︽儏鏀逛负娴姩寮圭獥锛堜笉鍐嶅崰绗洓鍒楋紝鑺傜害妯悜绌洪棿锛夛細闈炴ā鎬?Tool 绐楋紝娴湪涓荤獥涔嬩笂銆?
        # 璺熼殢閫変腑瀹炴椂鍒锋柊锛屽叧鎺変笉褰卞搷鎼滅储/棰勮涓诲尯锛涗俊鍙蜂笌 _update_detail 閫昏緫鍧囦笉鍙樸€?
        self.detail_panel = DetailPanel(self._tok, parent=self)
        # 鏃犺竟妗嗙幓鐠冨脊绐楋紙鑷粯鐜荤拑鏍囬鏍忓彲鎷栧姩 + 鍏抽棴锛夛紝show 鍚庡姞 Win11 DWM 鍦嗚锛屽拰涓荤獥缁熶竴
        self.detail_panel.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.detail_panel.restore_requested.connect(self._act_restore_version)
        self.detail_panel.export_requested.connect(self._act_export_version)
        self.detail_panel.page_requested.connect(self._act_goto_page)
        self.detail_panel.hide()
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 5)
        split.setStretchFactor(2, 6)
        split.setSizes([0, 520, 660])
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
        self.index_bar = QProgressBar()
        self.index_bar.setObjectName("indexBar")
        self.index_bar.setTextVisible(False)
        self.index_bar.setFixedWidth(200)
        self.index_bar.hide()
        self.status.addWidget(self.index_bar)
        self.pct_label = QLabel("")
        self.pct_label.setObjectName("pctLabel")
        self.status.addWidget(self.pct_label)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_dot.hide()
        self.status.addWidget(self.status_dot)
        self.status_label = QLabel("准备中…")
        self.status.addWidget(self.status_label)
        self.version_shield = QLabel("")
        self.version_shield.setObjectName("verShield")
        self.version_shield.hide()  # 鏈夌増鏈悗鎵嶆樉绀?
        self.status.addPermanentWidget(self.version_shield)
        kb = QLabel('<span id="kbd"> ↑↓ </span> 选择　<span id="kbd"> ↵</span> 打开　<span id="kbd"> Esc </span> 收起')
        kb.setTextFormat(Qt.RichText)
        self.hotkey_label = QLabel(f"全局热键 {get_hotkey()}")
        self.hotkey_label.setObjectName("hotkeyLabel")
        self.hotkey_label.setCursor(Qt.PointingHandCursor)
        self.hotkey_label.setToolTip("点击修改全局唤起热键")
        self.hotkey_label.installEventFilter(self)  # 点击 → 打开设置（#2 热键可改）
        self.status.addPermanentWidget(kb)
        self.status.addPermanentWidget(self.hotkey_label)

        # 瓒ｅ懗缁熻銆屾垜鐨勮兌鐗囨姤鍛娿€嶅叆鍙ｏ紙闈炰镜鍏ユ敞鍏ワ紝閫昏緫鍏ㄥ湪 stats_entry锛?
        from .stats_entry import install_stats_entry
        install_stats_entry(self)

        self._init_toast()
        self._init_spinner()
        self._install_shortcuts()

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
        pr.addWidget(self.copy_text_btn, 0)
        self.copy_path_btn = QPushButton("复制路径")
        self.copy_path_btn.setObjectName("linkBtn")
        self.copy_path_btn.clicked.connect(self._act_copy_path)
        self.copy_path_btn.hide()
        pr.addWidget(self.copy_path_btn, 0)
        hv.addLayout(pr)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("metaLabel")
        hv.addWidget(self.meta_label)
        lay.addWidget(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.image_label = QLabel(
            '<div style="font-size:30px">🔎</div>'
            '<div style="color:#888;font-size:13px;margin-top:12px">选中左侧结果，预览命中页</div>')
        self.image_label.setObjectName("previewImage")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.image_label)
        # 棰勮鍖烘粴杞?= 鎸夊師濮嬮〉搴忕炕椤碉紙鐪嬪墠鍑犻〉鍒ゆ柇鏄笉鏄鎵剧殑 PPT锛?
        self.scroll.viewport().installEventFilter(self)
        self.image_label.installEventFilter(self)
        lay.addWidget(self.scroll, 1)

        # 鍛戒腑椤电缉鐣ュ浘鏉?
        self.thumb_row = QHBoxLayout()
        self.thumb_row.setSpacing(7)
        self.thumb_row.setAlignment(Qt.AlignCenter)
        thumb_wrap = QWidget()
        thumb_wrap.setLayout(self.thumb_row)
        lay.addWidget(thumb_wrap)

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("◀ 上一命中页")
        self.prev_btn.setObjectName("navBtn")
        self.prev_btn.setToolTip("上一处命中页　(Ctrl+↑)")
        self.prev_btn.clicked.connect(lambda: self._step_hit(-1))
        self.page_label = QLabel("—")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_btn = QPushButton("下一命中页 ▶")
        self.next_btn.setObjectName("navBtn")
        self.next_btn.setToolTip("下一处命中页　(Ctrl+↓)")
        self.next_btn.clicked.connect(lambda: self._step_hit(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next_btn)
        lay.addLayout(nav)

        ops = QHBoxLayout()
        self.goto_btn = QPushButton("↵ 打开并跳到此页")
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
        lay.addLayout(ops)
        self._set_ops_enabled(False)
        return panel

    # ---------- 鐜荤拑鏍囬鏍忥紙鏃犺竟妗嗙獥鍙ｈ嚜缁橈級 ----------
    def _build_glass_title(self) -> QWidget:
        tb = QWidget()
        tb.setObjectName("glassTitle")
        tb.setFixedHeight(self._title_h)
        l = QHBoxLayout(tb)
        l.setContentsMargins(16, 0, 6, 0)
        l.setSpacing(9)
        dot = QLabel("◆")
        dot.setObjectName("gtDot")
        name = QLabel("PPTutor")
        name.setObjectName("gtName")
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("gtVer")
        self.gt_theme = QLabel(dict(theme.THEMES).get(self._theme, self._theme))
        self.gt_theme.setObjectName("gtTheme")
        self.update_chip = QPushButton("")  # 澧為噺鏇存柊 chip锛氬彂鐜版柊鐗堟墠鏄剧ず锛岄潪妯℃€併€佷笉鎵撴柇鎼滅储
        self.update_chip.setObjectName("updateChip")
        self.update_chip.setCursor(Qt.PointingHandCursor)
        self.update_chip.hide()
        l.addWidget(dot)
        l.addWidget(name)
        l.addWidget(ver)
        l.addWidget(self.gt_theme)
        l.addWidget(self.update_chip)
        l.addStretch(1)
        for txt, slot, oid, tip in (("—", self.showMinimized, "winMin", "最小化"),
                                    ("□", self._win_toggle_max, "winMax", "最大化 / 还原"),
                                    ("×", self.close, "winClose", "关闭")):
            btn = QPushButton(txt)
            btn.setObjectName(oid)
            btn.setFixedSize(46, self._title_h)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            l.addWidget(btn)
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
        for w in (self.goto_btn, self.open_btn, self.folder_btn, self.clip_btn,
                  self.copy_path_btn,
                  self.prev_btn, self.next_btn):
            w.setEnabled(on)
        for b in getattr(self, "_thumb_btns", []):
            b.setEnabled(on)
        panel = getattr(self, "detail_panel", None)
        set_detail_actions = getattr(panel, "set_file_actions_enabled", None)
        if callable(set_detail_actions):
            set_detail_actions(on)

    def _set_result_refine_enabled(self, enabled: bool) -> None:
        for w in (self.sort_combo, self.facet_btn, self.facet_panel):
            w.setEnabled(enabled)

    def _clear_stale_result_context(self) -> None:
        self._results_raw = []
        self._results = []
        self._cur = None
        self._cur_item_widget = None
        self._preview_deferred_due_to_busy = False
        self._clear_detail_load_inflight()
        self._clear_detail_panel_selection()
        self._invalidate_preview_request()
        self._update_preview_header(None)
        self._clear_preview_empty()
        self.result_list.clear()
        self._thumb.clear()
        self._thumb_items.clear()
        self._set_ops_enabled(False)

    def _update_preview_header(self, r: FileResult | None) -> None:
        """棰勮椤舵爮锛氬畬鏁磋矾寰勶紙鍙鍒讹級+ 澶у皬路椤垫暟路淇敼鏃堕棿銆?"""
        if r is None:
            self.path_label.setText("← 选中左侧结果查看预览")
            self.path_label.setToolTip("")
            self.meta_label.setText("")
            self.copy_path_btn.hide()
            self.copy_text_btn.hide()
            return
        self.path_label.setText(_elide_middle(r.path))
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
        self.theme_btn.setText(f"🎨 {dict(theme.THEMES).get(name, name)}")
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
        self.query_hint.setText(explain_query(query, self._mode_key()).summary)
        self.query_hint.show()

    def _clear_search_now(self) -> None:
        self.search_box.clear()
        self._do_search()

    def _do_search(self) -> None:
        if self._closing:
            return
        self._debounce.stop()
        query = self.search_box.text().strip()
        self._search_seq += 1
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
            self._search_worker.request(self._search_seq, query, self._mode_key())
            return
        started = time.perf_counter()
        results = SearchWorker._apply_mode(search_mod.search(self._conn, query), self._mode_key())
        self._finish_search(query, results, (time.perf_counter() - started) * 1000)

    def _show_search_pending(self, query: str) -> None:
        self._status_refresh_token += 1
        req_id = self._search_seq
        self._search_pending_req = req_id
        self._render_gen += 1
        self._thumb.clear()
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
            self._clear_detail_panel_selection()
            self._invalidate_preview_request()
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

    def _clear_search_pending(self) -> None:
        self._search_pending_req = None
        self._search_slow_hint_timer.stop()

    def _maybe_run_deferred_live_refresh(self, query: str) -> None:
        if not self._live_refresh_after_search:
            return
        self._live_refresh_after_search = False
        if self._closing or self.search_box.text().strip() != query:
            return
        QTimer.singleShot(self._DEFERRED_LIVE_SEARCH_YIELD_MS, self._do_search)

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
            self.status_label.setText(f"\u641c\u7d22\u5931\u8d25\uff1a{error}")
            self.result_count.setText("搜索失败 · 已保留当前结果" if self.result_list.count() else "搜索失败")
            if self.result_list.count():
                self.result_list.setEnabled(True)
                self._set_result_refine_enabled(True)
                self._set_ops_enabled(self._cur is not None)
                self._flush_deferred_preview_if_idle()
            else:
                self.list_head.hide()
                self._invalidate_preview_request()
                self._update_preview_header(None)
                self._set_ops_enabled(False)
                self._show_empty_hint(query)
            self._maybe_run_deferred_live_refresh(query)
            return
        self._finish_search(query, list(results or []), elapsed_ms)
        self._refresh_status()
        self._maybe_run_deferred_live_refresh(query)

    def _finish_search(self, query: str, results: list[FileResult], elapsed_ms: float | None = None) -> None:
        self._clear_search_pending()
        self._results_raw = results
        self._refresh_facets()
        self._apply_sort_render()
        if results:
            suffix = f" \u00b7 {elapsed_ms:.0f} ms" if elapsed_ms is not None else ""
            self.result_count.setText(f"\u547d\u4e2d {len(results)} \u4e2a\u6587\u4ef6{suffix}")
            self.list_head.show()
            self._select_first(delayed_preview=True)
            self._animate_list_in()
        else:
            self.list_head.hide()
            self._cur = None
            self._clear_detail_load_inflight()
            self._clear_detail_panel_selection()
            self._invalidate_preview_request()
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._show_empty_hint(query)

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
        if conn_path:
            own = db.connect(conn_path)
            try:
                return list(db.recent_files(own, limit=20))
            finally:
                own.close()
        return list(db.recent_files(self._conn, limit=20))

    def _apply_recent_results(self, recents: list[FileResult]) -> None:
        self.result_list.setEnabled(True)
        self._set_result_refine_enabled(True)
        self._results_raw = list(recents)
        self._cur = None
        self._clear_detail_load_inflight()
        self._clear_detail_panel_selection()
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
        self._empty_icon = QLabel("🔍")
        self._empty_icon.setObjectName("emptyIcon")
        self._empty_icon.setAlignment(Qt.AlignCenter)
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
        self.empty_hint.hide()
        parent_layout.addWidget(self.empty_hint, 1)

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
        s = dict(db.stats(self._conn))
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
        self._empty_icon.setText("🔍")
        self._empty_tip.setText("换个说法试试")
        self._empty_query_label.setText(f"\u6ca1\u627e\u5230\u300c{query}\u300d")
        self._set_empty_index_status_async()
        sugg = suggestion_keys(query, self._mode_key())
        for key, btn in self._sugg_btns.items():
            btn.setVisible(key in sugg)
        self.empty_hint.show()

    def _show_start_hint(self) -> None:
        """鏃犳渶杩戞枃浠讹紙鍒氳 / 杩樺湪绱㈠紩锛夋椂鐨勮捣姝ュ紩瀵硷紝澶嶇敤 emptyHint 瀹瑰櫒锛堥殣钘忓缓璁寜閽級銆?"""
        self.result_list.hide()
        self._empty_icon.setText("📂")
        self._empty_query_label.setText("\u8fd8\u5728\u6574\u7406\u4f60\u7684 PPT...")
        self._empty_tip.setText("索引好后这里会列出最近文件；现在就能在上方搜索框直接搜你写过的字")
        self._set_empty_index_status_async()
        for btn in self._sugg_btns.values():
            btn.hide()
        self.empty_hint.show()

    def _hide_empty_hint(self, *, invalidate_status: bool = True) -> None:
        if getattr(self, "empty_hint", None) is not None:
            if invalidate_status:
                self._empty_status_token += 1
                self._empty_status_inflight_token = None
                self._empty_status_inflight_mode = None
            self.empty_hint.hide()
            self.result_list.show()

    def _apply_suggestion(self, key: str) -> None:
        q = self.search_box.text()
        search_started = False
        if key == "unquote":
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
        dlg = SettingsDialog(self._version_mgr, self, on_rescan=self._request_full_rescan)
        dlg.tabs.setCurrentIndex(1)
        self._settings_dialogs.append(dlg)
        dlg.destroyed.connect(lambda _=None, d=dlg: self._settings_dialogs.remove(d) if d in self._settings_dialogs else None)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _request_full_rescan(self) -> bool:
        """鍋ュ悍璇婃柇閲岀殑涓€閿噸鎵叆鍙ｏ細鍙彂璧峰悗鍙扮储寮曪紝涓嶉樆濉炶缃璇濇銆?"""
        return self._start_indexing(None, None)

    def _sort_key(self) -> str:
        return {"相关度": "relevance", "最近修改": "recent", "文件名": "name"}.get(
            self.sort_combo.currentText(), "relevance")

    def _apply_sort_render(self) -> None:
        base = self._results_raw
        if self._facet_filters:
            base = facet_filter(base, self._facet_filters, datetime.datetime.now().timestamp())
        self._results = _sort_results(base, self._sort_key())
        self._render_results(self._results)

    def _on_sort_changed(self) -> None:
        if self._search_pending_req is not None:
            return
        if self._results_raw:
            self._cancel_auto_preview()
            self._apply_sort_render()
            if self._results:
                self._select_first(delayed_preview=True)

    # 棣栧睆鍚屾娓叉煋鏉℃暟锛氱粨鏋?鈮?姝ゆ暟鍒欏叏閮ㄥ悓姝ラ摵锛堝皬缁撴灉闆嗛浂寤惰繜锛孶I 娴嬭瘯涓嶅彈寮傛褰卞搷锛夛紱
    # 瓒呭嚭閮ㄥ垎鐢ㄤ簨浠跺惊鐜垎鎵?娴佸紡"琛ュ叆鈥斺€旈涓粨鏋滄渶蹇嚭鐜帮紝鏁翠綋浠?< 3s銆?
    _RENDER_FIRST = 16
    _RENDER_CHUNK = 16
    _THUMB_FIRST = 16
    _THUMB_CACHE_MAX = 256
    _HIT_NAV_MAX = 12
    _RENDER_YIELD_MS = 1

    def _render_results(self, results: list[FileResult]) -> None:
        self.result_list.setEnabled(True)
        self._set_result_refine_enabled(True)
        if not self._showing_recent:
            self._recent_load_token += 1
        self._hide_empty_hint(invalidate_status=bool(results))
        self.result_list.clear()
        self._thumb.clear()        # 涓㈠純涓婁竴鎵规湭娓插畬鐨勭缉鐣ュ浘璇锋眰
        self._thumb_items.clear()
        # 版本组折叠状态随每次重渲染复位（新搜索/排序/筛选/换肤都回折叠态）；展开是就地插入，不走这里
        self._group_primary_item = {}
        self._group_member_items = {}
        self._group_others = {}
        self._expanded_groups = set()
        self._render_gen += 1      # 浣滃簾涓婁竴鎵逛粛鍦ㄦ祦鍏ョ殑鍒嗘壒娓叉煋
        hlcss = theme.highlight_css(self._theme)
        plan = self._build_render_plan(results)
        n = self._flush_plan(plan, 0, self._RENDER_FIRST, hlcss)  # 棣栧睆绔嬪嵆閾?
        if n < len(plan):
            self._stream_plan_rest(plan, n, hlcss, self._render_gen)

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
        w = ResultItem(r, self._tok, hlcss, ginfo=ginfo, on_toggle_group=self._toggle_version_group)
        item.setSizeHint(w.sizeHint())
        self.result_list.addItem(item)
        self.result_list.setItemWidget(item, w)
        if ginfo and ginfo.get("count"):
            self._group_primary_item[ginfo["gid"]] = item  # 记录组主卡列表项，供就地展开定位
        self._request_thumb(w, idx)

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
            w = ResultItem(orr, self._tok, hlcss, ginfo={"member": True, "gid": gid})
            item.setSizeHint(w.sizeHint())
            self.result_list.insertItem(base + 1 + k, item)
            self.result_list.setItemWidget(item, w)
            # 成员是用户主动展开看的，直接请求缩略图（不受首屏 THUMB_FIRST 门限限制）
            key = (w.path, w.thumb_page)
            self._thumb_items[key] = w
            cached = self._thumb_cache.get(key)
            if cached is not None and not cached.isNull():
                w.set_thumbnail(cached)
            else:
                self._thumb.request(w.path, w.thumb_page)
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

    def _request_thumb(self, w: ResultItem, idx: int) -> None:
        """缁撴灉鍗＄墖缂╃暐鍥撅細鍛戒腑缂撳瓨鐩存帴鐢紝鍚﹀垯浠呬负棣栧睆鍓嶈嫢骞蹭釜鍙戣捣娓叉煋銆?"""
        key = (w.path, w.thumb_page)
        self._thumb_items[key] = w
        cached = self._thumb_cache.pop(key, None)
        if cached is not None:
            self._thumb_cache[key] = cached
            w.set_thumbnail(cached)
        elif idx < self._THUMB_FIRST:
            self._thumb.request(w.path, w.thumb_page)

    def _remember_thumb(self, key: tuple[str, int], pm: QPixmap) -> None:
        self._thumb_cache.pop(key, None)
        self._thumb_cache[key] = pm
        while len(self._thumb_cache) > self._THUMB_CACHE_MAX:
            self._thumb_cache.pop(next(iter(self._thumb_cache)))

    def _on_thumb(self, path: str, page: int, png: str) -> None:
        if self._closing:
            return
        key = (path, page)
        if key not in self._thumb_items:
            return
        w = self._thumb_items.pop(key, None)
        if not png or not os.path.exists(png):
            return
        pm = QPixmap(png)
        if pm.isNull():
            return
        self._remember_thumb(key, pm)
        if w is not None:
            w.set_thumbnail(pm)

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
            self._clear_detail_panel_selection()
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
        f = 180 if not self.facet_panel.isHidden() else 0
        avail = max(560, self.width() - f - 24)
        self._split.setSizes([f, int(avail * 0.44), int(avail * 0.56)])

    def _toggle_facet(self) -> None:
        if self._search_pending_req is not None:
            return
        self.facet_panel.setHidden(not self.facet_panel.isHidden())
        self.facet_btn.setChecked(not self.facet_panel.isHidden())
        self._relayout_split()

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
            self._clear_detail_panel_selection()
            self._invalidate_preview_request()
            self._clear_preview_empty("筛选后没有可预览结果")
            self._update_preview_header(None)
            self._set_ops_enabled(False)
            self._show_facet_empty()

    def _show_facet_empty(self) -> None:
        """facet 鎶婄粨鏋滅瓫绌烘椂鐨勬彁绀衡€斺€旀槸绛涢€夊お绐勶紝涓嶆槸娌℃悳鍒般€?"""
        self.result_list.hide()
        self._empty_icon.setText("🔎")
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

    def _toggle_detail(self) -> None:
        if self.detail_panel.isHidden():
            self._position_detail_popup()
            self.detail_panel.show()
            self._round_detail_corners()   # 鏃犺竟妗嗙獥 show 鍚庢墠鑳芥嬁 winId 鍔?Win11 鍦嗚
            self.detail_panel.raise_()
            self._detail_update_token += 1
            self._update_detail()
            self._maybe_hint_detail_versions()
        else:
            self.detail_panel.hide()
            self._detail_update_token += 1
            self._detail_update_force = False
            self._detail_update_timer.stop()
            self._clear_detail_load_inflight()
        self.detail_btn.setChecked(not self.detail_panel.isHidden())
        self._detail_dot_token += 1
        self._detail_dot_timer.stop()
        self._refresh_detail_dot()

    def _position_detail_popup(self) -> None:
        """璇︽儏寮圭獥娴湪涓荤獥鍙充晶鍐呬晶锛岃窡闅忎富绐楀綋鍓嶄綅缃?澶у皬銆?"""
        g = self.frameGeometry()
        w = 360
        h = min(640, max(420, g.height() - 120))
        self.detail_panel.resize(w, h)
        self.detail_panel.move(max(0, g.right() - w - 30), g.top() + self._title_h + 60)

    def _round_detail_corners(self) -> None:
        """缁欐棤杈规璇︽儏寮圭獥鍔?Win11 DWM 鍦嗚锛堟棫绯荤粺闈欓粯澶辫触锛岄€€鍖栦负鐩磋锛夈€?"""
        if not _WIN:
            return
        try:
            hwnd = int(self.detail_panel.winId())
            # DWMWA_WINDOW_CORNER_PREFERENCE=33, value 2=ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:  # noqa: BLE001
            pass

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
        if bool(has) and not self.detail_panel.isHidden():
            self._detail_opened_once = True
            self._toast("💡 这里能一键回到任意历史版本")

    def _schedule_detail_update(self, *, force: bool = False) -> None:
        if self.detail_panel.isHidden():
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

    def _clear_detail_panel_selection(self) -> None:
        panel = getattr(self, "detail_panel", None)
        clear = getattr(panel, "clear_selection", None)
        if callable(clear):
            clear()

    def _update_detail(self, *, force: bool = False) -> None:
        if self.detail_panel.isHidden() or self._cur is None:
            self._clear_detail_load_inflight()
            if self._cur is None:
                self._clear_detail_panel_selection()
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
        if self.detail_panel.isHidden() or self._cur is None:
            return
        if self._cur.path != path or self._cur.file_id != file_id:
            return
        if not isinstance(payload, dict):
            return
        r = payload.get("result") or self._cur
        versions = list(payload.get("versions") or [])
        titles = list(payload.get("titles") or [])
        self.detail_panel.update_for(r, versions)
        try:
            self.detail_panel.set_outline(titles)
        except Exception:  # noqa: BLE001
            self.detail_panel.set_outline([])
        self._set_ops_enabled(self._cur is not None)

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

    def _restore_version_off_ui(self, path: str, version_id: str):
        if os.path.exists(path):
            try:
                with open(path, "r+b"):
                    pass
            except OSError:
                return "locked"
        return bool(self._version_mgr.restore_to(path, version_id))

    def _confirm_restore(self) -> bool:
        """鎭㈠鍓嶅弸濂界‘璁わ細寮鸿皟銆屼細鑷姩鐣欏簳銆侀殢鏃跺垏鍥炪€嶏紝闄嶄綆鐮村潖鎬ф搷浣滅殑蹇冪悊璐熸媴銆?"""
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("恢复到这个版本？")
        box.setText("会用这个历史版本覆盖当前文件。")
        box.setInformativeText("别担心：覆盖前会自动把当前内容也留一版，之后随时能再切回来。")
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
        for i, h in enumerate(hits[:self._HIT_NAV_MAX]):
            b = QToolButton()
            b.setObjectName("thumb")
            b.setText(f"\u7b2c{h.page_no}\u9875")
            b.setCheckable(True)
            b.setChecked(i == self._hit_idx)
            b.setEnabled(self._active_heavy_op is None and self._search_pending_req is None)
            b.setFixedSize(64, 34)
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
        self._hit_idx = i
        if self._cur and self._cur.hits:
            self._view_page = self._cur.hits[i].page_no
        self._request_preview()

    def _step_hit(self, delta: int) -> None:
        if self._preview_interaction_blocked():
            return
        if not self._cur or not self._cur.hits:
            return
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
            "\u9996\u6b21\u9884\u89c8\u9700\u8981\u542f\u52a8 PowerPoint\uff0c\u8bf7\u7a0d\u7b49..."
            if not getattr(self, "_preview_hinted", False)
            else "\u6b63\u5728\u6e32\u67d3\u9884\u89c8..."
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

    def _invalidate_preview_request(self) -> None:
        self._preview_deferred_due_to_busy = False
        self._req_id += 1
        if hasattr(self, "_spin_timer"):
            self._stop_spinner()

    def _show_preview_pending(self) -> None:
        self._cur_pixmap = None
        self._preview_provisional = False
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            '<div style="font-size:28px">…</div>'
            '<div style="color:#888;font-size:13px;margin-top:12px">正在准备预览</div>')

    def _clear_preview_empty(self, message: str = "选中左侧结果查看预览") -> None:
        self._cur_pixmap = None
        self._preview_provisional = False
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            f'<div style="color:#888;font-size:13px">{message}</div>')

    def _show_preview_unavailable(self) -> None:
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(
            '<div style="font-size:30px">📫</div>'
            '<div style="color:#888;font-size:13px;margin-top:12px">此页暂时无法预览<br>'
            '点“打开文件”直接查看</div>')
        self._cur_pixmap = None
        self._preview_provisional = False

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
        for i, b in enumerate(self._thumb_btns):
            b.setChecked(i < n and hits[i].page_no == page)
        # 娓愯繘寮忛瑙堬細璇ラ〉缂╃暐鍥惧凡缂撳瓨灏辩珛鍗虫斁澶ф樉绀轰綔鍗犱綅锛堢鍑哄唴瀹广€侀伄浣忔覆鏌撶瓑寰咃級锛岄珮娓呮覆鏌?
        # 濂藉悗鍦?_on_rendered 鏃犵紳鏇挎崲銆傚懡涓〉閫氬父宸叉湁缂╃暐鍥撅紙缁撴灉鍗＄墖宸︿晶閭ｅ紶灏辨槸瀹冿級銆?
        thumb = self._thumb_cache.get((self._cur.path, page))
        if thumb is not None and not thumb.isNull():
            self._cur_pixmap = thumb
            self._preview_provisional = True
            self._update_pixmap()
        else:
            self._preview_provisional = False
            self._start_spinner()
        self._req_id += 1
        self._render.request(self._req_id, self._cur.path, page, cache_key=None)

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
        self._update_pixmap()

    def _toggle_zoom(self) -> None:
        if self._preview_interaction_blocked():
            return
        if self._cur_pixmap is None:
            return
        self._zoom = 1.0 if self._zoom > 1.0 else 2.0
        self._update_pixmap()
        self._toast("原尺寸放大 · 再双击还原" if self._zoom > 1.0 else "已适配窗口")

    def _on_rendered(self, req_id: int, png: str) -> None:
        if self._closing:
            return
        if req_id != self._req_id:
            return
        self._stop_spinner()
        if not png or not os.path.exists(png):
            if self._preview_provisional and self._cur_pixmap is not None:
                return
            self._show_preview_unavailable()
            return
        pm = QPixmap(png)
        if pm.isNull():
            if self._preview_provisional and self._cur_pixmap is not None:
                return
            self._show_preview_unavailable()
            return
        self._cur_pixmap = pm
        self._preview_provisional = False  # 楂樻竻宸插埌锛屼笉鍐嶆槸鍗犱綅
        self._preview_hinted = True  # 棣栨棰勮宸叉垚鍔燂紝涔嬪悗涓嶅啀鎻愩€屽敜璧?PowerPoint銆?
        self._update_pixmap()
        self._prefetch_neighbors()  # 鍚庡彴棰勬覆鏌撶浉閭?鍛戒腑椤碉紝缈昏繃鍘绘椂缂撳瓨鍛戒腑=鐬棿

    def _prefetch_neighbors(self) -> None:
        """鍚庡彴棰勬覆鏌撳綋鍓嶆枃浠躲€屽叾瀹冨懡涓〉 + 鍓嶅悗椤点€嶁啋 缈昏繃鍘绘椂缂撳瓨鍛戒腑銆佺灛闂村嚭鍥俱€?

        鏂囦欢宸叉墦寮€鐫€锛岄鍙栨瘡椤靛彧鏄瀵煎嚭 ~0.07s锛屼綆浼樺厛銆佽鏂伴瑙堥殢鏃舵姠鍗犲苟浣滃簾
        锛坃request_preview鈫抮ender_worker.request 浼氭竻绌哄緟棰勫彇锛夛紝鏁呭彧棰勫彇浣犲綋鍓嶅仠鐣欓〉鐨勯偦灞呫€?
        """
        if self._cur is None or not self._owns_render:
            return
        if not hasattr(self._render, "prefetch"):
            return  # 娴嬭瘯娉ㄥ叆鐨?StubRender 鏃犳鏂规硶
        total = self._cur.page_count or 0
        cur = self._view_page
        order: list[int] = [h.page_no for h in (self._cur.hits or []) if h.page_no != cur]
        order += [cur + 1, cur - 1]  # 鍏跺畠鍛戒腑椤典紭鍏堬紝鍐嶅墠鍚庣浉閭婚〉
        seen = {cur}
        for p in order:
            if p in seen or p < 1 or (total and p > total):
                continue
            seen.add(p)
            if len(seen) > 7:  # 闄愰噺锛屽埆杩囧害棰勬覆鏌?
                break
            self._render.prefetch(self._cur.path, p)

    def _update_pixmap(self) -> None:
        if self._cur_pixmap is None:
            return
        vp = self.scroll.viewport().size()
        fitted = self._cur_pixmap.scaled(
            vp.width() - 6, vp.height() - 6, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setText("")
        if self._zoom <= 1.0:
            self.scroll.setWidgetResizable(True)
            self.image_label.setPixmap(fitted)
        else:
            self.scroll.setWidgetResizable(False)
            scaled = self._cur_pixmap.scaled(
                int(fitted.width() * self._zoom), int(fitted.height() * self._zoom),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
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
        self._update_pixmap()
        if getattr(self, "_toast_label", None) is not None and self._toast_label.isVisible():
            self._reposition_toast()
        if getattr(self, "_welcome", None) is not None:
            self._welcome.resize(self.size())

    def changeEvent(self, e):  # noqa: N802
        if e.type() == QEvent.ActivationChange and self._cur_item_widget is not None:
            self._cur_item_widget.set_selected(True, self.isActiveWindow())
        super().changeEvent(e)

    # ---------- 閿洏 ----------
    def eventFilter(self, obj, ev):  # noqa: N802
        et = ev.type()
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
    def _open_file_path(self, path: str) -> None:
        def _after(ok):
            if ok is None:
                self._toast("打开文件时出错了，请稍后重试")
            elif not ok:
                self._toast("文件已移动或删除")

        if self._run_bg(lambda: actions.open_file(path), _after, "open"):
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
        """复制当前预览页的文字：直接取已索引的 pages_raw 原文，不启动 PowerPoint。
        单行主键查询（WAL 下读不阻塞写），不走重操作后台通道。"""
        if self._block_if_search_pending():
            return
        if not self._cur:
            return
        page = self._view_page
        try:
            text = db.get_page_text(self._conn, self._cur.file_id, page)
        except Exception:  # noqa: BLE001 取文本失败不致命，提示即可
            _log.warning("复制本页文字失败", exc_info=True)
            text = ""
        if text.strip():
            self._copy_text_with_toast(text, f"已复制第 {page} 页文字（{len(text)} 字）")
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
            if res is None:
                self._toast("打开时出错了，请稍后重试")
                return
            opened, jumped = res
            if not opened:
                self._toast("文件已移动或删除")
            elif not jumped:
                self._toast(f"\u5df2\u6253\u5f00\uff0c\u4f46\u672a\u80fd\u81ea\u52a8\u8df3\u5230\u7b2c {page} \u9875")

        if self._run_bg(lambda: actions.open_at_page(path, page), _after, "open"):
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
        self._open_at_page_bg(self._cur.path, self._view_page)

    def _on_activate(self, _item) -> None:
        self._act_goto()

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
        if self._version_mgr is None or cur is None or not self.detail_panel.isHidden():
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
        if has and self.detail_panel.isHidden():  # 璇︽儏宸叉墦寮€灏变笉鐢ㄧ孩鐐瑰啀鎻愮ず
            self._detail_dot.move(self.detail_btn.width() - 14, 5)
            self._detail_dot.show()
            self._detail_dot.raise_()
            # 棣栨鍙戠幇 + 绐楀彛鍙鏃舵墠鍛煎惛寮曞锛堥殣钘忓埌鎵樼洏鏃朵笉娴垂鍔ㄧ敾锛岀暀鍒颁笅娆″啀璇曪級
            if not getattr(self, "_detail_hint_done", False) and self.isVisible():
                self._detail_hint_done = True
                from .spotlight import attention_pulse
                attention_pulse(self.detail_btn,
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
            self.detail_btn,
            "已自动给你改过的 PPT 留了底 🛡️\n"
            "改崩了、想找回旧版，点这里「详情」就能一键回到任意历史版本。")
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
            return db.stats(self._conn)["file_count"] == 0
        except Exception:  # noqa: BLE001
            return True

    def _schedule_startup_index_check(self, roots: list[str] | None, workers: int | None) -> None:
        self._startup_index_token += 1
        token = self._startup_index_token
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
        try:
            file_count = int(stats.get("file_count", 0))
        except (TypeError, ValueError):
            file_count = 0
        if file_count <= 0:
            self._start_indexing(roots, workers)
            return
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
        if self._indexer is not None and self._indexer.isRunning():
            self._toast("正在扫描中，请稍候…")
            return False
        from ..scanner import fixed_drives
        if not roots:
            env = os.environ.get("PPTX_FINDER_ROOTS", "").strip()
            if env:
                roots = [r for r in env.split(os.pathsep) if r]
        roots = roots or fixed_drives()
        self._indexer = IndexWorker(self._db_path, roots, workers=workers)
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
        self._index_status_cache = None
        self.index_bar.setRange(0, 0)
        self.index_bar.show()
        self.status_dot.hide()
        self.status_label.setText(f"开始索引：{', '.join(roots)}")
        self._indexer.start()
        return True

    def _on_index_progress(self, done: int, total: int, cur: str) -> None:
        if self._closing:
            return
        self._status_refresh_token += 1
        self._index_last_done = done
        self._index_last_total = total
        self._index_last_current = cur
        phase = "scan" if total < 0 else "index"
        now = time.monotonic()
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
        self.index_bar.show()
        if getattr(self, "_welcome", None) is not None and done > 0:
            self._welcome.update_progress(done)
        preserve_search_status = self._search_pending_req is not None
        if total < 0:
            self.index_bar.setRange(0, 0)  # busy锛氳繘搴︽潯鏉ュ洖娴佸姩锛堟壂鎻忥紝鎬绘暟鏈煡锛?
            self.pct_label.setText("")
            if not preserve_search_status:
                self.status_label.setText(f"扫描磁盘中…　{cur}（可边扫边搜）")
        else:
            self.index_bar.setRange(0, max(1, total))
            self.index_bar.setValue(done)
            self.pct_label.setText(f"{int(done / max(1, total) * 100)}%")
            if not preserve_search_status:
                self.status_label.setText(f"正在索引内容　{done}/{total}　·　{os.path.basename(cur)}")

    def _on_index_done(self, summary: dict) -> None:
        if self._closing:
            return
        self._index_search_ready = True
        self._index_last_summary = dict(summary or {})
        self._index_status_cache = None
        self.index_bar.hide()
        self.pct_label.setText("")
        celebrate = not getattr(self, "_index_celebrated", False)
        if celebrate:
            self._index_celebrated = True
        self._refresh_status(summary, celebrate=celebrate)
        if not self.search_box.text().strip():
            self._show_recent(dashboard_force_refresh=True, recent_force_refresh=True)  # 绱㈠紩瀹屾垚鍚庡埛鏂版渶杩戯紙鐢ㄦ埛杩樻病寮€濮嬫悳鏃讹紝绾冲叆鏂扮储寮曠殑鏂囦欢锛?
    def _load_status_stats(self, conn_path: str | None) -> dict:
        if conn_path:
            own = db.connect(conn_path)
            try:
                return dict(db.stats(own))
            finally:
                own.close()
        return dict(db.stats(self._conn))

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
        extra = ""
        if summary and "deleted" in summary:
            extra = f"\uff08\u66f4\u65b0 {summary.get('indexed', 0)}\uff0c\u79fb\u9664 {summary.get('deleted', 0)}\uff09"
        self.status_dot.show()
        self.status_label.setText(
            f"\u7d22\u5f15\u5c31\u7eea\uff1a{stats.get('file_count', 0)} \u4e2a\u6587\u4ef6 \u00b7 {stats.get('page_count', 0)} \u9875{extra}"
        )

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
        self._closing = True
        self._render_gen += 1
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
        if self._owns_thumb:
            self._thumb.stop()
        if self._owns_render:
            self._render.stop()
        if self._owns_thumb:
            if not self._thumb.wait(6000):
                self._thumb.terminate()
                self._thumb.wait(1500)
        if self._owns_render:
            if not self._render.wait(8000):
                self._render.terminate()
                self._render.wait(2000)

    def _bg_task_shutdown_wait_ms(self, task) -> int:
        if self._is_heavy_bg_task(task):
            return self._BG_HEAVY_SHUTDOWN_WAIT_MS
        return self._BG_LIGHT_SHUTDOWN_WAIT_MS

    def _is_heavy_bg_task(self, task) -> bool:
        label = getattr(task, "_label", "")
        return label in self._BG_HEAVY_LABELS
