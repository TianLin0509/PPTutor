"""窗口销毁时后台任务还在跑：进程不能崩，任务要跑完。"""
import gc
import threading

from PySide6.QtWidgets import QWidget

from pptx_finder.ui.bg_task import BackgroundTask


def test_task_outlives_destroyed_owner(qtbot):
    release = threading.Event()
    finished = threading.Event()

    def work():
        release.wait(5)
        finished.set()

    owner = QWidget()
    owner.task = BackgroundTask(work, "owner-lifetime", owner)
    owner.task.start()
    owner.deleteLater()
    del owner
    gc.collect()
    qtbot.wait(100)
    release.set()
    qtbot.waitUntil(finished.is_set, timeout=5000)
    qtbot.wait(100)
