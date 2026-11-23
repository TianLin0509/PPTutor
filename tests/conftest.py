"""测试全局配置：把应用数据目录指向临时目录，避免污染真实 LOCALAPPDATA。"""
import os
import tempfile

os.environ.setdefault(
    "PPTX_FINDER_DATA_DIR", tempfile.mkdtemp(prefix="pptxfinder_test_")
)
# 公司内源更新源只在打包版里开；有的用例把 is_frozen 改成真，别让它们真去连内源
os.environ.setdefault("PPTX_FINDER_OPENX", "0")

import time

import pytest


@pytest.fixture(scope="session", autouse=True)
def _wait_background_tasks_at_exit():
    """整轮测试结束、解释器退出前等后台线程跑完。

    否则还在跑的 QThread 随解释器析构被销毁，Qt 直接 abort（退出码 0xC0000409），
    测试全过也会让闸门判失败（2026-11-22 test_settings_dialog 复现）。
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
        from pptx_finder.ui import bg_task
    except Exception:  # noqa: BLE001
        return
    deadline = time.monotonic() + 15.0
    for task in list(bg_task._running):
        left = int(max(0.0, deadline - time.monotonic()) * 1000)
        try:
            if left and task.isRunning():
                task.stop()
                task.wait(left)
        except RuntimeError:
            pass
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
