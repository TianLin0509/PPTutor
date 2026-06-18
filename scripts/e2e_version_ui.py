"""UI E2E：版本管理窗口 显示 / 选择 / 跨版本搜 + 截图。offscreen，隔离 data dir。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
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


def main() -> int:
    work = Path(_tmp) / "work"
    work.mkdir(parents=True)
    p = work / "算力方案.pptx"
    fx.make_pptx(p, [{"body": "封面"}, {"body": "量子计算 章节"}])

    mgr = VersionManager()
    mgr.add_root(str(work))  # v1
    fx.make_pptx(p, [{"body": "封面 v2"}, {"body": "经典计算"}])
    mgr.snapshot_now(str(p))  # v2
    fx.make_pptx(p, [{"body": "终稿"}, {"body": "终稿内容"}])
    mgr.snapshot_now(str(p))  # v3

    app = QApplication(sys.argv)
    app.setStyleSheet(theme.build_qss("cloud"))

    w = VersionWindow(mgr)
    w.resize(940, 580)
    check("版本窗口列出受管文档", w.doc_list.count() >= 1)

    w.doc_list.setCurrentRow(0)
    check("选中文档后列出版本时间线(3 版)", w.ver_list.count() == 3)

    w.search.setText("量子计算")
    w._do_search()
    check("跨版本搜在 UI 列出历史命中", w.ver_list.count() >= 1)

    w.show()
    loop = QEventLoop()
    QTimer.singleShot(400, loop.quit)
    loop.exec()
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    w.grab().save(str(out / "version_window.png"))
    check("版本窗口截图成功", (out / "version_window.png").exists())

    dlg = SettingsDialog(mgr)
    check("设置面板列出受管目录", dlg.root_list.count() >= 1)
    dlg.grab().save(str(out / "settings_dialog.png"))

    ok = sum(1 for _, c in results if c)
    print(f"\n=== UI E2E: {ok}/{len(results)} 通过 ===")
    app.quit()
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
