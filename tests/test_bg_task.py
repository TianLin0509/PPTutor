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


def test_background_task_limits_concurrent_work(qtbot):
    lock = threading.Lock()
    release = threading.Event()
    entered = 0
    active = 0
    max_active = 0

    def fn():
        nonlocal active, entered, max_active
        with lock:
            entered += 1
            active += 1
            max_active = max(max_active, active)
        release.wait(timeout=3)
        with lock:
            active -= 1
        return "ok"

    limit = bg_task._REGULAR_CONCURRENT
    tasks = [BackgroundTask(fn, label=f"limit-{i}") for i in range(limit + 3)]
    for task in tasks:
        task.start()

    qtbot.waitUntil(lambda: entered >= limit, timeout=3000)
    with lock:
        assert max_active <= limit
        assert entered == limit
    assert "waiting=" in "\n".join(bg_task.diagnostic_lines())

    release.set()
    for task in tasks:
        assert task.wait(5000)


def test_interactive_open_keeps_one_reserved_background_slot(qtbot):
    if bg_task._MAX_CONCURRENT < 2:
        return
    release = threading.Event()
    entered = []
    lock = threading.Lock()

    def work(label):
        def _run():
            with lock:
                entered.append(label)
            release.wait(3)
            return label
        return _run

    regular = [
        BackgroundTask(work(f"regular-{i}"), label=f"regular-{i}")
        for i in range(bg_task._MAX_CONCURRENT)
    ]
    urgent = BackgroundTask(work("open"), label="open")
    for task in regular:
        task.start()
    try:
        qtbot.waitUntil(
            lambda: len(entered) >= bg_task._MAX_CONCURRENT - 1,
            timeout=2000,
        )
        urgent.start()
        qtbot.waitUntil(lambda: "open" in entered, timeout=800)
        with lock:
            assert len([x for x in entered if x.startswith("regular-")]) == bg_task._MAX_CONCURRENT - 1
    finally:
        release.set()
        for task in [*regular, urgent]:
            task.wait(5000)


def test_waiting_background_task_can_be_cancelled_before_it_runs(qtbot):
    if bg_task._MAX_CONCURRENT < 2:
        return
    release = threading.Event()
    blockers = [
        BackgroundTask(lambda: release.wait(3), label=f"blocker-{i}")
        for i in range(bg_task._MAX_CONCURRENT - 1)
    ]
    ran = []
    queued = BackgroundTask(lambda: ran.append(True), label="queued-low-priority")
    for task in blockers:
        task.start()
    try:
        qtbot.waitUntil(
            lambda: f"active={bg_task._MAX_CONCURRENT - 1}" in bg_task.diagnostic_lines()[0],
            timeout=2000,
        )
        queued.start()
        qtbot.waitUntil(lambda: "waiting=1" in bg_task.diagnostic_lines()[0], timeout=1000)
        queued.stop()
        assert queued.wait(1000)
        assert ran == []
    finally:
        release.set()
        for task in blockers:
            task.wait(5000)
