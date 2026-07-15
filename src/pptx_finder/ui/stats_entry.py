"""非侵入式入口注入：给主窗口挂「我的胶片报告」入口。

只通过 mw 的稳定 self 属性（theme_btn / status_label / _conn / _theme）附加入口，
不改 main_window 既有逻辑。main_window 仅需在 _build_ui 末尾调用 install_stats_entry(self)。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
try:
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # noqa: BLE001
    def _qt_is_valid(_obj) -> bool:
        return True

from .. import db
from .. import stats
from . import theme
from .bg_task import BackgroundTask
from .report_overlay import ReportOverlay


def _conn_path(conn) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None
        return row["file"] if hasattr(row, "keys") else row[2]
    except Exception:  # noqa: BLE001
        return None


def _build_report_off_ui(conn, *, year=None, version_db_path=None):
    kwargs = {"year": year}
    if version_db_path is not None:
        kwargs["version_db_path"] = version_db_path
    path = _conn_path(conn)
    if path:
        own = db.connect(path)
        try:
            return stats.build_report(own, **kwargs)
        finally:
            own.close()
    return stats.build_report(conn, **kwargs)


def _version_db_path(mw):
    manager = getattr(mw, "_version_mgr", None)
    return getattr(manager, "_db_path", None) if manager is not None else None


def _main_window_alive(mw) -> bool:
    try:
        return _qt_is_valid(mw) and not getattr(mw, "_closing", False)
    except RuntimeError:
        return False


def _clear_report_loading_if_possible(mw) -> None:
    try:
        if _qt_is_valid(mw):
            mw._stats_report_loading = False
    except RuntimeError:
        pass


def _raise_existing_report_overlay(mw) -> bool:
    try:
        ov = getattr(mw, "_stats_overlay", None)
        if ov is None or not _qt_is_valid(ov) or getattr(ov, "_closing", False):
            return False
        set_geometry = getattr(ov, "setGeometry", None)
        if callable(set_geometry):
            set_geometry(mw.rect())
        show = getattr(ov, "show", None)
        if callable(show):
            show()
        raise_ = getattr(ov, "raise_", None)
        if callable(raise_):
            raise_()
        activate = getattr(ov, "activateWindow", None)
        if callable(activate):
            activate()
        return True
    except (RuntimeError, TypeError):
        try:
            mw._stats_overlay = None
        except Exception:  # noqa: BLE001
            pass
        return False


def _open_report(mw) -> None:
    if not _main_window_alive(mw):
        return
    if _raise_existing_report_overlay(mw):
        return
    if getattr(mw, "_stats_report_loading", False):
        if hasattr(mw, "_toast"):
            mw._toast("胶片报告正在生成…")
        return
    mw._stats_report_loading = True
    if hasattr(mw, "_toast"):
        mw._toast("正在生成胶片报告…")
    version_db_path = _version_db_path(mw)
    task = BackgroundTask(
        lambda: _build_report_off_ui(
            mw._conn,
            year=None,
            version_db_path=version_db_path,
        ),
        "stats-report-build",
        mw,
    )
    if not hasattr(mw, "_stats_report_tasks"):
        mw._stats_report_tasks = []
    mw._stats_report_tasks.append(task)
    bg_tasks = getattr(mw, "_bg_tasks", None)
    if isinstance(bg_tasks, list) and task not in bg_tasks:
        bg_tasks.append(task)

    def _done(report):
        if not _main_window_alive(mw):
            _clear_report_loading_if_possible(mw)
            return
        _clear_report_loading_if_possible(mw)
        if report is None:
            if hasattr(mw, "_toast"):
                mw._toast("胶片报告生成失败，请稍后重试")
            return
        ov = ReportOverlay(
            report,
            theme.tok(mw._theme),
            parent=mw,
            conn=mw._conn,
            version_db_path=version_db_path,
        )
        ov.setGeometry(mw.rect())
        ov.show()
        ov.raise_()
        mw._stats_overlay = ov  # 持引用，避免被 GC 回收

    def _cleanup():
        try:
            if not _qt_is_valid(mw):
                return
            tasks = getattr(mw, "_stats_report_tasks", [])
            if task in tasks:
                tasks.remove(task)
            bg_tasks = getattr(mw, "_bg_tasks", [])
            if task in bg_tasks:
                bg_tasks.remove(task)
        except RuntimeError:
            return

    task.done.connect(_done)
    task.finished.connect(_cleanup)
    task.start()


def _open_report_sync(mw) -> None:
    version_db_path = _version_db_path(mw)
    report = _build_report_off_ui(mw._conn, year=None, version_db_path=version_db_path)
    ov = ReportOverlay(
        report,
        theme.tok(mw._theme),
        parent=mw,
        conn=mw._conn,
        version_db_path=version_db_path,
    )
    ov.setGeometry(mw.rect())
    ov.show()
    ov.raise_()
    mw._stats_overlay = ov  # 持引用，避免被 GC 回收


def install_stats_entry(mw) -> None:
    """给主窗口挂两个入口：顶栏 🎞️ 图标 + 状态栏数字可点击。"""
    # 入口 1：顶栏 🎞️ 图标（插到主题切换键旁，ghost 风格不抢眼）
    btn = QPushButton("🎞️")
    btn.setObjectName("ghost")
    btn.setMinimumHeight(42)
    btn.setToolTip("我的胶片报告")
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(lambda: _open_report(mw))
    try:
        bar = mw.theme_btn.parentWidget().layout().itemAt(0).layout()
        bar.addWidget(btn)
    except Exception:  # noqa: BLE001 顶栏结构变了就降级，不挂顶栏入口
        pass

    # 入口 2：状态栏数字可点击（彩蛋）
    try:
        mw.status_label.setCursor(Qt.PointingHandCursor)
        mw.status_label.setToolTip("点我看胶片报告")
        mw.status_label.mousePressEvent = lambda e: _open_report(mw)
    except Exception:  # noqa: BLE001
        pass
