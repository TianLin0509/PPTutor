"""库概览仪表盘（零搜索默认首屏）。

极光玻璃布局下的"驾驶舱"：KPI 大数字 + 主题分布环形图 + 最近活跃列表 + 库构成条形 +
本周改动趋势。数据优先取真实 db / version_mgr 统计，取不到则合理占位。图表自绘并跟随
主窗 `_tok` 变色。集成进主窗：无搜索词时显示本视图，有结果时隐藏（QStackedWidget 切换）。

仅 UI 层：不触碰搜索 / 版本 / 后台线程逻辑，只读 db 统计。
"""
from __future__ import annotations

import datetime
import logging
import os
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .bg_task import BackgroundTask

_log = logging.getLogger(__name__)

_WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"]


def _seg_colors(tok: dict) -> list[QColor]:
    base = QColor(tok.get("acc", "#0A84FF"))
    acc2 = QColor(tok.get("accd", base.name()))
    return [base, base.lighter(135), base.darker(122), acc2, base.lighter(165)]


def _rgb_of(tok: dict) -> tuple[int, int, int]:
    return (int(tok.get("hl_r", 10)), int(tok.get("hl_g", 132)), int(tok.get("hl_b", 255)))


def _ink(tok: dict, i: int) -> str:
    return tok.get(f"ink{i + 1}", ("#F0EEFC", "#C8C2E8", "#948CC0", "#6A6298")[i])


def _conn_path(conn) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None
        path = row["file"] if hasattr(row, "keys") else row[2]
        return path or None
    except Exception:  # noqa: BLE001
        return None


class _Donut(QWidget):
    """主题/构成占比环形图：data=[(label, value), ...]。"""

    def __init__(self, view: "DashboardView"):
        super().__init__()
        self._view = view
        self.setMinimumHeight(150)

    def paintEvent(self, e):  # noqa: N802
        data = self._view._topics
        if not data:
            return
        tok = self._view._tok()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rct = QRectF(self.rect())
        size = min(rct.width(), rct.height()) - 12
        if size <= 0:
            p.end()
            return
        cx, cy = rct.center().x(), rct.center().y()
        thick = size * 0.20
        arc = QRectF(cx - size / 2 + thick / 2, cy - size / 2 + thick / 2, size - thick, size - thick)
        total = sum(v for _, v in data) or 1
        cols = _seg_colors(tok)
        start = 90 * 16
        for i, (_, v) in enumerate(data):
            span = -int(360 * 16 * v / total)
            p.setPen(QPen(cols[i % len(cols)], thick, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(arc, start, span)
            start += span
        p.setPen(QColor(_ink(tok, 0)))
        p.setFont(QFont("Consolas", max(8, int(size * 0.14)), QFont.Bold))
        p.drawText(QRectF(cx - size / 2, cy - size * 0.16, size, size * 0.2),
                   Qt.AlignCenter, str(len(data)))
        p.setPen(QColor(_ink(tok, 2)))
        p.setFont(QFont("Microsoft YaHei", max(8, int(size * 0.052))))
        p.drawText(QRectF(cx - size / 2, cy + size * 0.02, size, size * 0.16),
                   Qt.AlignCenter, "热门分布")
        p.end()


class _HBars(QWidget):
    """库构成水平条形：data=[(label, value), ...]。"""

    def __init__(self, view: "DashboardView"):
        super().__init__()
        self._view = view
        self.setMinimumHeight(140)

    def paintEvent(self, e):  # noqa: N802
        data = self._view._folders
        if not data:
            return
        tok = self._view._tok()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rct = QRectF(self.rect())
        mx = max((v for _, v in data), default=1) or 1
        ar, ag, ab = _rgb_of(tok)
        n = len(data)
        rowh = rct.height() / n
        bx = 96
        bw = rct.width() - bx - 44
        if bw <= 0:
            p.end()
            return
        for i, (lab, v) in enumerate(data):
            y = i * rowh + rowh * 0.22
            h = rowh * 0.4
            p.setPen(QColor(_ink(tok, 1)))
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(QRectF(0, i * rowh, bx - 8, rowh), Qt.AlignRight | Qt.AlignVCenter, str(lab))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ar, ag, ab, 38))
            p.drawRoundedRect(QRectF(bx, y, bw, h), h / 2, h / 2)
            p.setBrush(QColor(ar, ag, ab, 235))
            p.drawRoundedRect(QRectF(bx, y, bw * v / mx, h), h / 2, h / 2)
            p.setPen(QColor(_ink(tok, 2)))
            p.setFont(QFont("Consolas", 9))
            p.drawText(QRectF(bx + bw + 4, i * rowh, 40, rowh),
                       Qt.AlignLeft | Qt.AlignVCenter, str(v))
        p.end()


class _Spark(QWidget):
    """本周改动趋势柱：7 个值（周一→周日）。"""

    def __init__(self, view: "DashboardView"):
        super().__init__()
        self._view = view
        self.setMinimumHeight(70)

    def paintEvent(self, e):  # noqa: N802
        week = self._view._week
        if not week:
            return
        tok = self._view._tok()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rct = QRectF(self.rect())
        mx = max(week) or 1
        n = len(week)
        ar, ag, ab = _rgb_of(tok)
        gap = 8
        bw = (rct.width() - gap * (n - 1)) / n
        if bw <= 0:
            p.end()
            return
        today_wd = datetime.date.today().weekday()
        for i, v in enumerate(week):
            bh = (rct.height() - 16) * v / mx
            x = i * (bw + gap)
            top = rct.height() - 14 - bh
            cur = i == today_wd
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ar, ag, ab, 245 if cur else 130))
            p.drawRoundedRect(QRectF(x, top, bw, bh), 3, 3)
            p.setPen(QColor(_ink(tok, 3)))
            p.setFont(QFont("Microsoft YaHei", 7))
            p.drawText(QRectF(x, rct.height() - 13, bw, 12), Qt.AlignCenter, _WEEK_LABELS[i])
        p.end()


class DashboardView(QWidget):
    """库概览首屏。`win` 提供 `_tok`（主题）/ `_conn`（db）/ `_version_mgr`（可 None）。

    refresh()：重算真实统计 + 重绘（主窗在切到首屏 / 切主题 / 索引完成时调）。
    """
    _REFRESH_MIN_INTERVAL_MS = 1000

    def __init__(self, win):
        super().__init__()
        self.setObjectName("dashView")
        self._win = win
        # 数据缓存（图表 paintEvent 读取；refresh 重算）
        self._topics: list[tuple[str, int]] = []
        self._folders: list[tuple[str, int]] = []
        self._week: list[int] = [0] * 7
        self._recent: list[tuple[str, str, str]] = []
        self._last_refresh_at = 0.0
        self._refresh_token = 0
        self._refresh_apply_token = 0
        self._refresh_inflight_token: int | None = None
        self._refresh_inflight_force = False
        self._pending_refresh_force = False
        self._refresh_tasks: list[BackgroundTask] = []
        self._parent_bg_tasks = getattr(win, "_bg_tasks", None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self._build()
        self._apply_data()
        self._restyle()

    def _tok(self) -> dict:
        return getattr(self._win, "_tok", {}) or {}

    # ---------- 布局 ----------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(14)

        head = QHBoxLayout()
        h1 = QLabel("库概览")
        h1.setObjectName("dashTitle")
        head.addWidget(h1)
        self._sub = QLabel("你的 PPT 资产全景")
        self._sub.setObjectName("dashSub")
        head.addWidget(self._sub)
        head.addStretch(1)
        root.addLayout(head)

        # KPI 行
        kpi = QHBoxLayout()
        kpi.setSpacing(14)
        self._kpi_nums: list[QLabel] = []
        self._kpi_labs: list[QLabel] = []
        self._kpi_subs: list[QLabel] = []
        for _ in range(4):
            card = QFrame()
            card.setObjectName("dashCard")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(18, 14, 18, 14)
            cl.setSpacing(2)
            num = QLabel("—")
            num.setObjectName("kpiNum")
            lab = QLabel("")
            lab.setObjectName("kpiLab")
            sub = QLabel("")
            sub.setObjectName("kpiSub")
            cl.addWidget(num)
            cl.addWidget(lab)
            cl.addWidget(sub)
            self._kpi_nums.append(num)
            self._kpi_labs.append(lab)
            self._kpi_subs.append(sub)
            kpi.addWidget(card)
        root.addLayout(kpi)

        # 2x2 图表网格
        grid = QGridLayout()
        grid.setSpacing(14)
        grid.addWidget(self._topic_card(), 0, 0)
        grid.addWidget(self._recent_card(), 0, 1)
        grid.addWidget(self._folder_card(), 1, 0)
        grid.addWidget(self._week_card(), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        root.addLayout(grid, 1)

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        w = QFrame()
        w.setObjectName("dashCard")
        l = QVBoxLayout(w)
        l.setContentsMargins(18, 15, 18, 16)
        l.setSpacing(9)
        t = QLabel(title)
        t.setObjectName("dashCardT")
        l.addWidget(t)
        return w, l

    def _topic_card(self) -> QFrame:
        w, l = self._card("主题分布")
        row = QHBoxLayout()
        self._donut = _Donut(self)
        row.addWidget(self._donut, 1)
        self._leg_box = QVBoxLayout()
        self._leg_box.setSpacing(7)
        self._leg_rows: list[tuple[QLabel, QLabel, QLabel]] = []
        for _ in range(5):
            r = QHBoxLayout()
            r.setSpacing(8)
            dot = QLabel("●")
            nm = QLabel("")
            nm.setObjectName("legName")
            pc = QLabel("")
            pc.setObjectName("legPc")
            r.addWidget(dot)
            r.addWidget(nm, 1)
            r.addWidget(pc)
            self._leg_box.addLayout(r)
            self._leg_rows.append((dot, nm, pc))
        row.addLayout(self._leg_box, 1)
        l.addLayout(row, 1)
        return w

    def _recent_card(self) -> QFrame:
        w, l = self._card("最近活跃")
        self._rec_box = QVBoxLayout()
        self._rec_box.setSpacing(7)
        l.addLayout(self._rec_box)
        l.addStretch(1)
        return w

    def _folder_card(self) -> QFrame:
        w, l = self._card("库构成 · 按文件夹")
        self._hbars = _HBars(self)
        l.addWidget(self._hbars, 1)
        return w

    def _week_card(self) -> QFrame:
        w, l = self._card("本周活跃 · 改动趋势")
        self._spark = _Spark(self)
        l.addWidget(self._spark, 1)
        self._week_shield = QLabel("")
        self._week_shield.setObjectName("dashSub")
        l.addWidget(self._week_shield)
        return w

    # ---------- 数据 ----------
    def schedule_refresh(self, *, force: bool = False, delay_ms: int = 0) -> None:
        """把统计重算推迟到事件循环空档，并合并短时间内的重复请求。"""
        if not self._ui_alive():
            return
        self._refresh_token += 1
        token = self._refresh_token
        self._pending_refresh_force = self._pending_refresh_force or force
        QTimer.singleShot(delay_ms, lambda token=token: self._run_scheduled_refresh(token))

    def _run_scheduled_refresh(self, token: int) -> None:
        if not self._ui_alive():
            return
        if token != self._refresh_token:
            return
        force = self._pending_refresh_force
        self._pending_refresh_force = False
        self.refresh(force=force)

    def refresh(self, *, force: bool = False) -> None:
        """重算真实统计并刷新 KPI/列表/图表（取不到则占位，绝不抛异常打断 UI）。"""
        if not self._ui_alive():
            return
        now = time.monotonic()
        if (
            not force
            and self._last_refresh_at
            and (now - self._last_refresh_at) * 1000 < self._REFRESH_MIN_INTERVAL_MS
        ):
            self._restyle()
            return
        if self._refresh_inflight_token is not None and (self._refresh_inflight_force or not force):
            self._restyle()
            return
        self._last_refresh_at = now
        self._refresh_apply_token += 1
        token = self._refresh_apply_token
        conn = self._conn()
        conn_path = _conn_path(conn) if conn is not None else None
        fallback_conn = conn if conn is not None and conn_path is None else None
        vm = getattr(self._win, "_version_mgr", None)
        task = BackgroundTask(
            lambda conn_path=conn_path, fallback_conn=fallback_conn, vm=vm: self._build_payload(
                conn_path=conn_path,
                fallback_conn=fallback_conn,
                version_mgr=vm,
            ),
            "dashboard-refresh",
            None,
        )
        self._refresh_inflight_token = token
        self._refresh_inflight_force = force
        self._track_refresh_task(task)
        task.done.connect(lambda payload, token=token: self._on_refresh_payload(token, payload))
        task.finished.connect(lambda task=task, token=token: self._forget_refresh_task(task, token))
        task.start()

    def _conn(self):
        return getattr(self._win, "_conn", None)

    def _ui_alive(self) -> bool:
        try:
            if not _qt_is_valid(self):
                return False
            return not getattr(self._win, "_closing", False)
        except RuntimeError:
            return False

    def _on_refresh_payload(self, token: int, payload: object) -> None:
        if not self._ui_alive() or token != self._refresh_apply_token:
            return
        if isinstance(payload, dict):
            self._apply_payload(payload)
            self._apply_data()
        self._restyle()

    def _track_refresh_task(self, task) -> None:
        self._refresh_tasks.append(task)
        if self._parent_bg_tasks is not None and task not in self._parent_bg_tasks:
            self._parent_bg_tasks.append(task)

    def _forget_refresh_task(self, task, token: int | None = None) -> None:
        parent_tasks = self._parent_bg_tasks
        try:
            if _qt_is_valid(self):
                if task in self._refresh_tasks:
                    self._refresh_tasks.remove(task)
                if token is not None and self._refresh_inflight_token == token:
                    self._refresh_inflight_token = None
                    self._refresh_inflight_force = False
        except RuntimeError:
            pass
        if parent_tasks is not None and task in parent_tasks:
            parent_tasks.remove(task)

    def _build_payload(self, *, conn_path: str | None = None, fallback_conn=None,
                       version_mgr=None) -> dict:
        conn = None
        own_conn = False
        if conn_path:
            from .. import db
            conn = db.connect(conn_path)
            own_conn = True
        else:
            conn = fallback_conn

        file_count = page_count = 0
        rows: list = []
        if conn is not None:
            from .. import db
            try:
                s = db.stats(conn)
                file_count, page_count = s["file_count"], s["page_count"]
            except Exception:  # noqa: BLE001
                _log.warning("db.stats failed in dashboard", exc_info=True)
            try:
                rows = conn.execute(
                    "SELECT path, name, mtime FROM files ORDER BY mtime DESC LIMIT 400"
                ).fetchall()
            except Exception:  # noqa: BLE001
                _log.warning("db files query failed in dashboard", exc_info=True)
                rows = []

        guarded = 0
        if version_mgr is not None:
            try:
                if hasattr(version_mgr, "list_docs_details"):
                    guarded = len(version_mgr.list_docs_details())
                else:
                    guarded = len(version_mgr.list_docs())
            except Exception:  # noqa: BLE001
                guarded = 0

        folder_counts: dict[str, int] = {}
        week = [0] * 7
        now = datetime.datetime.now()
        week_start = (now - datetime.timedelta(days=now.weekday())).date()
        recent_week = 0
        for r in rows:
            folder = os.path.basename(os.path.dirname(r["path"])) or "根目录"
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
            try:
                dt = datetime.datetime.fromtimestamp(r["mtime"])
            except (OSError, OverflowError, ValueError):
                continue
            if dt.date() >= week_start:
                week[dt.weekday()] += 1
                recent_week += 1

        folders_sorted = sorted(folder_counts.items(), key=lambda kv: kv[1], reverse=True)
        top_folders = folders_sorted[:5]
        if len(folders_sorted) > 5:
            rest = sum(v for _, v in folders_sorted[5:])
            top_folders = folders_sorted[:4] + [("其他", rest)]

        recents: list[tuple[str, str, str]] = []
        if conn is not None:
            from .. import db
            try:
                for fr in db.recent_files(conn, limit=5):
                    recents.append((fr.name, _rel_time(fr.mtime),
                                    f"{fr.page_count}页" if fr.page_count else ""))
            except Exception:  # noqa: BLE001
                _log.warning("recent_files failed in dashboard", exc_info=True)

        if own_conn and conn is not None:
            conn.close()

        return {
            "kpi_vals": [
                (f"{file_count:,}", "已索引文档", "全文可搜"),
                (f"{guarded:,}" if guarded else "—", "版本快照", "PPT 版 git"),
                (f"{page_count:,}", "已索引页", "命中即定位"),
                (f"{recent_week:,}", "本周改动", "近 7 日活跃"),
            ],
            "folders": top_folders,
            "topics": top_folders,
            "week": week if any(week) else [0] * 7,
            "week_shield_text": (
                f"🛡 版本保护 {guarded} 份 · 改存即自动留版" if guarded
                else "🛡 改存即自动留版，随时回到旧版本"),
            "recent": recents,
        }

    def _apply_payload(self, payload: dict) -> None:
        self._kpi_vals = list(payload.get("kpi_vals") or [("—", "", "")] * 4)
        self._folders = list(payload.get("folders") or [])
        self._topics = list(payload.get("topics") or [])
        self._week = list(payload.get("week") or [0] * 7)
        self._week_shield_text = str(payload.get("week_shield_text") or "")
        self._recent = list(payload.get("recent") or [])

    def _recompute(self) -> None:
        conn = self._conn()
        file_count = page_count = 0
        rows: list = []
        if conn is not None:
            from .. import db
            try:
                s = db.stats(conn)
                file_count, page_count = s["file_count"], s["page_count"]
            except Exception:  # noqa: BLE001
                _log.warning("db.stats failed in dashboard", exc_info=True)
            try:
                rows = conn.execute(
                    "SELECT path, name, mtime FROM files ORDER BY mtime DESC LIMIT 400"
                ).fetchall()
            except Exception:  # noqa: BLE001
                _log.warning("db files query failed in dashboard", exc_info=True)
                rows = []

        # 守护文档数（版本快照）：version_mgr 可 None（测试 / 未注入）
        guarded = 0
        vm = getattr(self._win, "_version_mgr", None)
        if vm is not None:
            try:
                guarded = len(vm.list_docs())
            except Exception:  # noqa: BLE001
                guarded = 0

        # 文件夹构成（top 5，其余并入"其他"）
        folder_counts: dict[str, int] = {}
        week = [0] * 7
        now = datetime.datetime.now()
        week_start = (now - datetime.timedelta(days=now.weekday())).date()
        recent_week = 0
        for r in rows:
            folder = os.path.basename(os.path.dirname(r["path"])) or "根目录"
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
            try:
                dt = datetime.datetime.fromtimestamp(r["mtime"])
            except (OSError, OverflowError, ValueError):
                continue
            if dt.date() >= week_start:
                week[dt.weekday()] += 1
                recent_week += 1

        folders_sorted = sorted(folder_counts.items(), key=lambda kv: kv[1], reverse=True)
        top_folders = folders_sorted[:5]
        if len(folders_sorted) > 5:
            rest = sum(v for _, v in folders_sorted[5:])
            top_folders = folders_sorted[:4] + [("其他", rest)]

        # KPI 数值
        self._kpi_vals = [
            (f"{file_count:,}", "已索引文档", "全文可搜"),
            (f"{guarded:,}" if guarded else "—", "版本快照", "PPT 版 git"),
            (f"{page_count:,}", "已索引页", "命中即定位"),
            (f"{recent_week:,}", "本周改动", "近 7 日活跃"),
        ]
        self._folders = top_folders
        # 主题分布：复用文件夹分布当"分布"语义（真实可得；无则占位）
        self._topics = top_folders
        self._week = week if any(week) else [0] * 7
        self._week_shield_text = (
            f"🛡 版本保护 {guarded} 份 · 改存即自动留版" if guarded
            else "🛡 改存即自动留版，随时回到旧版本")

        # 最近活跃列表（取真实最近文件名 + 相对时间）
        recents: list[tuple[str, str, str]] = []
        if conn is not None:
            from .. import db
            try:
                for fr in db.recent_files(conn, limit=5):
                    recents.append((fr.name, _rel_time(fr.mtime),
                                    f"{fr.page_count}页" if fr.page_count else ""))
            except Exception:  # noqa: BLE001
                _log.warning("recent_files failed in dashboard", exc_info=True)
        self._recent = recents

    def _apply_data(self) -> None:
        for i, (num, lab, sub) in enumerate(getattr(self, "_kpi_vals",
                                                     [("—", "", "")] * 4)[:4]):
            self._kpi_nums[i].setText(num)
            self._kpi_labs[i].setText(lab)
            self._kpi_subs[i].setText(sub)

        total = sum(v for _, v in self._topics) or 1
        for i, (dot, nm, pc) in enumerate(self._leg_rows):
            if i < len(self._topics):
                lab, v = self._topics[i]
                dot.show()
                nm.show()
                pc.show()
                nm.setText(str(lab))
                pc.setText(f"{round(v / total * 100)}%")
            else:
                dot.hide()
                nm.hide()
                pc.hide()

        # 最近活跃列表：清旧建新
        while self._rec_box.count():
            it = self._rec_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if self._recent:
            for name, tm, ver in self._recent:
                it = QFrame()
                it.setObjectName("dashRec")
                il = QHBoxLayout(it)
                il.setContentsMargins(11, 8, 12, 8)
                il.setSpacing(8)
                disp = name if len(name) < 18 else name[:16] + "…"
                nm = QLabel(disp)
                nm.setObjectName("recName")
                nm.setToolTip(name)
                tml = QLabel(tm)
                tml.setObjectName("recTime")
                vr = QLabel(ver)
                vr.setObjectName("recVer")
                il.addWidget(nm, 1)
                il.addWidget(tml)
                il.addWidget(vr)
                self._rec_box.addWidget(it)
        else:
            empty = QLabel("还没有最近活跃的文件 · 索引好后这里会列出")
            empty.setObjectName("dashSub")
            empty.setWordWrap(True)
            self._rec_box.addWidget(empty)

        if hasattr(self, "_week_shield"):
            self._week_shield.setText(getattr(self, "_week_shield_text", ""))

        for ch in (getattr(self, "_donut", None), getattr(self, "_hbars", None),
                   getattr(self, "_spark", None)):
            if ch is not None:
                ch.update()

    def _restyle(self) -> None:
        """图例圆点颜色跟随主题（QSS 管不到自绘色，这里直接 setStyleSheet）。"""
        tok = self._tok()
        cols = _seg_colors(tok)
        for i, (dot, _nm, _pc) in enumerate(getattr(self, "_leg_rows", [])):
            dot.setStyleSheet(
                f"color:{cols[i % len(cols)].name()};background:transparent;font-size:11px;")

    def set_theme(self) -> None:
        """主题切换：重绘图表 + 刷新图例色（数据不变，无需重算 db）。"""
        for ch in (getattr(self, "_donut", None), getattr(self, "_hbars", None),
                   getattr(self, "_spark", None)):
            if ch is not None:
                ch.update()
        self._restyle()


def _rel_time(ts: float) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return ""
    now = datetime.datetime.now()
    d = (now.date() - dt.date()).days
    if d <= 0:
        return dt.strftime("今天 %H:%M")
    if d == 1:
        return dt.strftime("昨天 %H:%M")
    if dt.year == now.year:
        return dt.strftime("%m-%d")
    return dt.strftime("%Y-%m-%d")
