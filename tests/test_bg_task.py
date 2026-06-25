"""后台任务：把重活丢后台线程跑、结果信号回主线程（修复版本恢复/导出/打开 COM 冻结 UI）。"""
from __future__ import annotations

import threading

from pptx_finder.ui import bg_task
from pptx_finder.ui.bg_task import BackgroundTask


def test_background_task_runs_off_main_thread(qtbot):
    main = threading.current_thread()
    ran_on = {}

    def fn():
        ran_on["t"] = threading.current_thread()
        return 42

    task = BackgroundTask(fn)
    with qtbot.waitSignal(task.done, timeout=3000) as blocker:
        task.start()
    assert blocker.args == [42]            # 返回值经信号回主线程
    assert ran_on["t"] is not main         # 重活确实在后台线程跑
    task.wait(2000)
    assert "background_tasks:" in "\n".join(bg_task.diagnostic_lines())


def test_background_task_swallows_exception(qtbot):
    def boom():
        raise ValueError("炸了")

    task = BackgroundTask(boom)
    with qtbot.waitSignal(task.done, timeout=3000) as blocker:
        task.start()
    assert blocker.args == [None]          # 异常 → 结果 None，线程不崩
    task.wait(2000)
    assert "failed=" in "\n".join(bg_task.diagnostic_lines())
