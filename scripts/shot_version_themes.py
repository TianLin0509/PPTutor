"""截图验证：版本管理窗口 / 设置面板 在多套主题下的视觉，确认与主体配色一致。

offscreen + 隔离 data dir，造 1 个受管文档(3 版) + 1 个受管目录，
在 cloud(浅) / raycast(深) 两套主题下 grab 两个窗口，保存到 artifacts/。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="pptxver_shot_")
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


def _settle(ms: int = 250) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    work = Path(_tmp) / "我的方案"
    work.mkdir(parents=True)
    p = work / "算力方案.pptx"
    fx.make_pptx(p, [{"body": "封面 算力方案"}, {"body": "第二页 量子计算 章节"}])

    mgr = VersionManager()
    mgr.add_root(str(work))  # v1（catch-up）
    fx.make_pptx(p, [{"body": "封面 算力方案 v2"}, {"body": "第二页 改成 经典计算"}])
    mgr.snapshot_now(str(p))  # v2
    fx.make_pptx(p, [{"body": "终稿封面"}, {"body": "终稿正文"}, {"body": "新增第三页"}])
    mgr.snapshot_now(str(p))  # v3

    app = QApplication(sys.argv)
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)

    for tname in ("cloud", "raycast"):
        app.setStyleSheet(theme.build_qss(tname))

        vw = VersionWindow(mgr)
        vw.resize(940, 560)
        vw.doc_list.setCurrentRow(0)  # 触发版本时间线填充
        vw.show()
        _settle()
        vw.grab().save(str(out / f"ver_{tname}.png"))
        print(f"saved ver_{tname}.png  docs={vw.doc_list.count()} versions={vw.ver_list.count()}")

        sd = SettingsDialog(mgr)
        sd.resize(540, 420)
        sd.show()
        _settle()
        sd.grab().save(str(out / f"settings_{tname}.png"))
        print(f"saved settings_{tname}.png roots={sd.root_list.count()}")

    app.quit()
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
