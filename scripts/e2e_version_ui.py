"""UI E2E：版本管理窗口 显示 / 选择 / 跨版本搜 + 截图。offscreen，隔离 data dir。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="pptxver_ui_")
os.environ["PPTX_FINDER_DATA_DIR"] = str(Path(_tmp) / "appdata")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import fixtures_gen as fx  # noqa: E402

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pptx_finder.ui import theme  # noqa: E402
from pptx_finder.ui.settings_dialog import SettingsDialog  # noqa: E402
from pptx_finder.ui.version_window import VersionWindow  # noqa: E402
from pptx_finder.versioning.manager import VersionManager  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} | {name}")


def wait_until(app: QApplication, predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def main() -> int:
    work = Path(_tmp) / "work"
    work.mkdir(parents=True)
    p = work / "算力方案.pptx"
    fx.make_pptx(p, [{"body": "封面"}, {"body": "量子计算 章节"}])

    mgr = VersionManager()
    mgr.snapshot_now(str(p))  # v1
    fx.make_pptx(p, [{"body": "封面 v2"}, {"body": "经典计算"}])
    mgr.snapshot_now(str(p))  # v2
    fx.make_pptx(p, [{"body": "终稿"}, {"body": "终稿内容"}])
    mgr.snapshot_now(str(p))  # v3

    app = QApplication(sys.argv)
    app.setStyleSheet(theme.build_qss("cloud"))

    w = VersionWindow(mgr)
    w.resize(940, 580)
    w.show()
    check(
        "版本窗口列出受管文档",
        wait_until(app, lambda: w.doc_list.count() >= 1 and "算力方案" in w.doc_list.item(0).text()),
    )

    w.doc_list.setCurrentRow(0)
    check(
        "选中文档后列出版本时间线(3 版)",
        wait_until(app, lambda: w.ver_list.count() == 3),
    )
    w.doc_filter.setText("算力")
    check("文件名筛选保留目标文档", w.doc_list.count() == 1)
    check("现存/已删除范围选择可用", w.doc_scope.count() == 3)

    w.search.setText("量子计算")
    w._do_search()
    check(
        "跨版本搜在 UI 列出历史命中",
        wait_until(app, lambda: "命中" in w.right_title.text() and w.ver_list.count() >= 1),
    )
    w.search.clear()
    w._populate_docs(mgr.list_docs_details())
    check(
        "截图前恢复完整版本时间线",
        wait_until(app, lambda: w.ver_list.count() == 3),
    )

    loop = QEventLoop()
    QTimer.singleShot(400, loop.quit)
    loop.exec()
    out = Path.home() / "artifacts"
    out.mkdir(parents=True, exist_ok=True)
    version_shot = out / "ppt-doctor-v105-version-window.png"
    w.grab().save(str(version_shot))
    check("版本窗口截图成功", version_shot.exists())

    dlg = SettingsDialog(mgr)
    dlg.show()
    check("设置面板默认保留 100 版", dlg.retention.currentData() == 100)
    check("设置面板提供版本库深检", dlg.vault_audit_btn.text() == "深度检查版本库")
    check(
        "设置面板守护统计异步加载完成",
        wait_until(app, lambda: "正在读取" not in dlg.stat.text()),
    )
    settings_shot = out / "ppt-doctor-v105-settings.png"
    dlg.grab().save(str(settings_shot))
    check("设置面板截图成功", settings_shot.exists())

    ok = sum(1 for _, c in results if c)
    print(f"\n=== UI E2E: {ok}/{len(results)} 通过 ===")
    app.quit()
    mgr.stop()
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
