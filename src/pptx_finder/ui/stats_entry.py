"""胶片报告：报告生成 + 浮层弹出逻辑。

入口已收编到主窗左侧导航轨「报告」按钮（main_window 直接调 ``_open_report(mw)``）；
顶栏图标按钮与状态栏数字点击彩蛋已随导航轨改造退役。
"""
from __future__ import annotations

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

