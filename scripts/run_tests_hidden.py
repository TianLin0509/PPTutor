"""Run real Windows/Qt tests on a private desktop; watch the user's desktop.

    uv run --frozen python scripts/run_tests_hidden.py -- -q
    uv run --frozen python scripts/run_tests_hidden.py --self-check-seconds 60

No desktop switching, offscreen platform, pythonw, or global interpreter hooks.
The supervising process stays outside the test job so a leak can stop only its
own process tree. A venv/uv redirector may lose STARTUPINFO.lpDesktop, therefore
the final interpreter also attaches its thread before importing pytest or Qt.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
WINDOW_LEAK_EXIT = 86
DESKTOP_PREFIX = 'PptDoctorTests-'


class VisibleTestWindow(RuntimeError):
    def __init__(self, windows):
        self.windows = windows
        super().__init__('测试窗口泄漏到工作桌面：' + json.dumps(windows, ensure_ascii=False))


class WindowGuard:
    """Enumerate top-level windows, then inspect only PIDs owned by this job."""
    def __init__(self, desktop_names=('Default',)):
        import pywintypes
        import win32service
        self.desktops = [win32service.OpenDesktop(name, 0, False, 0x0041)
                         for name in desktop_names]
        # Also cover the active input desktop when it is not Default.
        # A locked secure desktop is inaccessible; Default remains monitored.
        self.input_unavailable = False
        try:
            active = win32service.OpenInputDesktop(0, False, 0x0041)
            names = [win32service.GetUserObjectInformation(d, 2) for d in self.desktops]
            if win32service.GetUserObjectInformation(active, 2) not in names:
                self.desktops.append(active)
            else:
                active.CloseDesktop()
        except pywintypes.error as error:
            if error.winerror != 5:
                raise
            self.input_unavailable = True
        self.names = [win32service.GetUserObjectInformation(d, 2) for d in self.desktops]
        self.scans = 0

    def check(self, pids):
        import pywintypes
        import win32gui
        import win32process
        found = []
        for name, desktop in zip(self.names, self.desktops):
            try:
                windows = desktop.EnumDesktopWindows()
            except pywintypes.error as error:
                if error.winerror != 0:  # Win32 returns false/no-error for an empty desktop.
                    raise
                windows = []
            for hwnd in windows:
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid in pids and win32gui.IsWindowVisible(hwnd):
                        found.append(dict(pid=pid, hwnd=int(hwnd), desktop=name,
                                          title=win32gui.GetWindowText(hwnd)))
                except pywintypes.error as error:
                    if error.winerror != 1400:  # A window can disappear during enumeration.
                        raise
        self.scans += 1
        if found:
            raise VisibleTestWindow(found)

    def close(self):
        for desktop in self.desktops:
            desktop.CloseDesktop()


def enter_test_desktop(name):
    if not name.startswith(DESKTOP_PREFIX):
        raise ValueError('子进程只允许进入本运行器创建的私有桌面')
    import win32api
    import win32service
    desktop = win32service.OpenDesktop(name, 0, False, 0x01ff)
    desktop.SetThreadDesktop()
    actual = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
    if win32service.GetUserObjectInformation(actual, 2) != name:
        raise RuntimeError('测试桌面绑定失败；拒绝导入Qt')
    return desktop  # Keep its handle alive throughout pytest.


def run_windows(pytest_args, report_path, probe_seconds=None):
    import msvcrt
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process
    import win32service

    name = DESKTOP_PREFIX + str(os.getpid()) + '-' + uuid.uuid4().hex
    started = time.monotonic()
    report = dict(desktop=name, pytestArgs=pytest_args, passed=False, scans=0,
                  visibleWindows=[], qtPlatform='windows', pollIntervalMs=50)
    desktop = job = process = thread = guard = None
    inherited = []
    exit_code = 2
    try:
        desktop = win32service.CreateDesktop(name, 0, 0x01ff, None)
        guard = WindowGuard()
        guard.check(set())  # Fail closed on enumeration errors before tests start.
        report.update(monitoredDesktops=guard.names, inputDesktopUnavailable=guard.input_unavailable)
        job = win32job.CreateJobObject(None, name + '-job')
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = name
        startup.dwFlags = win32con.STARTF_USESHOWWINDOW | win32con.STARTF_USESTDHANDLES
        startup.wShowWindow = win32con.SW_HIDE
        with open(os.devnull, 'rb') as null:
            for stream in (null, sys.stdout, sys.stderr):
                handle = win32api.DuplicateHandle(
                    win32api.GetCurrentProcess(), msvcrt.get_osfhandle(stream.fileno()),
                    win32api.GetCurrentProcess(), 0, True, win32con.DUPLICATE_SAME_ACCESS)
                inherited.append(handle)
            startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited
            env = dict(os.environ, PPTDOCTOR_TEST_DESKTOP=name, QT_QPA_PLATFORM='windows',
                       PYTHONIOENCODING='utf-8')
            if probe_seconds is not None:
                env['PPTDOCTOR_TEST_PROBE_SECONDS'] = str(probe_seconds)
            command = [sys.executable, '-u', str(Path(__file__).resolve()),
                       '--_child-desktop', name, '--', *pytest_args]
            # Assign the suspended launcher before it can spawn a venv redirector
            # or pytest child; all descendants then belong to the cleanup job.
            flags = (subprocess.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED
                     | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
            process, thread, pid, _ = win32process.CreateProcess(
                None, subprocess.list2cmdline(command), None, None, True,
                flags, env, str(ROOT), startup)
            report['launcherPid'] = pid
            win32job.AssignProcessToJobObject(job, process)
            win32process.ResumeThread(thread)
        seen_pids = set()
        print(f'[隐藏测试] 私有桌面 {name}；监测 {guard.names}', flush=True)
        while True:
            pids = set(win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList))
            seen_pids.update(pids)
            guard.check(pids)
            if win32event.WaitForSingleObject(process, 50) == win32event.WAIT_OBJECT_0:
                guard.check(set(win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList)))
                break
        exit_code = win32process.GetExitCodeProcess(process)
        report['processIds'] = sorted(seen_pids)
        report['passed'] = exit_code == 0
    except VisibleTestWindow as error:
        report['visibleWindows'] = error.windows
        report['error'] = str(error)
        exit_code = WINDOW_LEAK_EXIT
        print(str(error), file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        exit_code = 130
        report['error'] = '用户中断，清理本次测试进程树'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        print(report['error'], file=sys.stderr, flush=True)
    finally:
        # A failed job assignment leaves a suspended process outside the job.
        if process is not None and win32event.WaitForSingleObject(process, 0) != win32event.WAIT_OBJECT_0:
            win32api.TerminateProcess(process, exit_code)
        if job is not None:
            win32job.TerminateJobObject(job, exit_code)
            job.Close()
        if process is not None:
            win32event.WaitForSingleObject(process, 5000)
            process.Close()
        if thread is not None:
            thread.Close()
        for handle in inherited:
            handle.Close()
        if guard is not None:
            report['scans'] = guard.scans
            guard.close()
        if desktop is not None:
            desktop.CloseDesktop()
        report.update(exitCode=exit_code, seconds=round(time.monotonic() - started, 3))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        print(f'[隐藏测试] 退出 {exit_code}；扫描 {report["scans"]} 次；可见测试窗口 '
              f'{len(report["visibleWindows"])}；报告 {report_path}', flush=True)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--self-check-seconds', type=float)
    parser.add_argument('--_child-desktop', help=argparse.SUPPRESS)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
    pytest_args = pytest_args or ['-q']
    if args._child_desktop:
        desktop = enter_test_desktop(args._child_desktop)
        import pytest
        return pytest.main(pytest_args)
    if args.self_check_seconds is not None:
        if os.name != 'nt':
            parser.error('可见窗口自检仅支持Windows；不可用跳过冒充通过')
        if not 0 < args.self_check_seconds <= 300:
            parser.error('--self-check-seconds须为0至300秒之间')
        pytest_args = ['-q', '-s', 'tests/test_hidden_test_runner.py::test_real_qt_window_stays_on_private_desktop']
    if os.name != 'nt':
        import pytest
        return pytest.main(pytest_args)
    report_path = args.report or ROOT / 'artifacts' / f'hidden-tests-{time.time_ns()}.json'
    return run_windows(pytest_args, report_path, args.self_check_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
