"""Regression coverage for the merge gate, without opening the user's desktop."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_gate_uses_hidden_entry():
    config = json.loads((ROOT / '.agents/project.json').read_text('utf-8'))
    assert config['test'] == ['uv run --frozen python scripts/run_tests_hidden.py -- -q']


def test_merge_reads_test_commands_after_candidate_config_changes(tmp_path, monkeypatch):
    merge = load_script('merge_task')
    config = tmp_path / 'project.json'
    config.write_text(json.dumps({'test': ['old-command']}), 'utf-8')
    monkeypatch.setattr(merge, 'CONFIG', config)
    old = merge.load_config()
    config.write_text(json.dumps({'test': ['new-hidden-command']}), 'utf-8')
    calls = []
    monkeypatch.setattr(merge, 'run', lambda command, **kw: calls.append(command)
                        or type('Result', (), {'returncode': 0})())
    merge.run_test_gate()
    assert old['test'] == ['old-command'] and calls == ['new-hidden-command']


def test_failed_test_gate_stops_instead_of_continuing(monkeypatch):
    merge = load_script('merge_task')
    monkeypatch.setattr(merge, 'load_config', lambda: {'test': ['first', 'second']})
    calls = []
    monkeypatch.setattr(merge, 'run', lambda command, **kw: calls.append(command)
                        or type('Result', (), {'returncode': 86})())
    with pytest.raises(RuntimeError, match='first'):
        merge.run_test_gate()
    assert calls == ['first']


def test_child_rejects_user_desktop_before_importing_pytest():
    runner = load_script('run_tests_hidden')
    with pytest.raises(ValueError, match='私有桌面'):
        runner.enter_test_desktop('Default')


def test_alternate_merge_script_cannot_change_repo_without_chdir(tmp_path, monkeypatch):
    merge = load_script('merge_task')
    monkeypatch.setattr(merge.sys, 'argv', ['merge_task.py', 'candidate', '--repo', str(tmp_path)])
    monkeypatch.setattr(merge, 'load_config', lambda: pytest.fail('must reject before reading config'))
    assert merge.main() == 2


def test_first_merge_bootstrap_uses_new_gate_and_keeps_rollback(tmp_path):
    """Real throwaway repository: no origin, no production checkout or push."""
    def run(args):
        return subprocess.run(args, cwd=tmp_path, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=30,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    def git(*args):
        result = run(['git', *args])
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    git('init', '-b', 'master')
    git('config', 'user.name', 'Hidden gate test')
    git('config', 'user.email', 'hidden-gate@example.invalid')
    git('config', 'core.hooksPath', str(tmp_path / 'no-hooks'))
    (tmp_path / '.agents').mkdir()
    config = tmp_path / '.agents/project.json'
    config.write_text(json.dumps({'trunk': 'master', 'test': [
        subprocess.list2cmdline([sys.executable, '-c', 'raise SystemExit(17)'])]}), 'utf-8')
    git('add', '.')
    git('commit', '-m', 'base with obsolete gate')
    original = git('rev-parse', 'HEAD')
    git('checkout', '-b', 'fix/new-gate')
    config.write_text(json.dumps({'trunk': 'master', 'test': [
        subprocess.list2cmdline([sys.executable, 'gate_probe.py'])]}), 'utf-8')
    (tmp_path / 'gate_probe.py').write_text(
        "from pathlib import Path; Path('new-gate-ran').write_text('ok')", 'utf-8')
    git('add', '.')
    git('commit', '-m', 'candidate gate')
    candidate = git('rev-parse', 'HEAD')
    git('checkout', 'master')
    result = run([sys.executable, str(ROOT / 'scripts/merge_task.py'),
                  'fix/new-gate', '--repo', str(tmp_path), '--dry-run'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'new-gate-ran').read_text() == 'ok'
    assert git('rev-parse', 'HEAD') == original
    assert git('rev-parse', 'fix/new-gate') == candidate
    assert not (tmp_path / '.git/MERGE_HEAD').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows desktop regression')
def test_merge_subprocesses_never_create_console(monkeypatch):
    merge = load_script('merge_task')
    seen = {}
    def capture(*args, **kwargs):
        seen.update(kwargs)
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(merge.subprocess, 'run', capture)
    merge.run(['git', 'status'])
    assert seen['creationflags'] & merge.subprocess.CREATE_NO_WINDOW


@pytest.mark.skipif(os.name != 'nt', reason='Windows desktop regression')
def test_real_qt_window_stays_on_private_desktop(qtbot):
    name = os.environ.get('PPTDOCTOR_TEST_DESKTOP')
    if not name:
        pytest.skip('Use scripts/run_tests_hidden.py for the real desktop probe')
    from PySide6.QtWidgets import QApplication, QWidget
    import win32api
    import win32con
    import win32service
    desktop = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
    assert win32service.GetUserObjectInformation(desktop, win32con.UOI_NAME) == name
    assert QApplication.platformName() == 'windows'
    window = QWidget()
    qtbot.addWidget(window)
    window.setWindowTitle('pytest-qt-qapp-hidden-gate-probe')
    window.show()
    assert window.isVisible()
    import pptx_finder
    assert Path(pptx_finder.__file__).resolve().is_relative_to(ROOT / 'src')
    print(json.dumps({'probe': 'real-qt-window', 'desktop': name,
                      'pid': os.getpid(), 'qtPlatform': QApplication.platformName(),
                      'fontFamily': QApplication.font().family(),
                      'windowShownOnPrivateDesktop': window.isVisible(),
                      'source': pptx_finder.__file__}), flush=True)
    qtbot.wait(int(float(os.environ.get('PPTDOCTOR_TEST_PROBE_SECONDS', '0.1')) * 1000))


@pytest.mark.skipif(os.name != 'nt', reason='Windows desktop regression')
def test_window_guard_detects_real_owned_window_without_touching_user_desktop(qtbot):
    name = os.environ.get('PPTDOCTOR_TEST_DESKTOP')
    if not name:
        pytest.skip('Needs the private desktop test entry')
    from PySide6.QtWidgets import QWidget
    runner = load_script('run_tests_hidden')
    window = QWidget()
    qtbot.addWidget(window)
    window.setWindowTitle('hidden-gate-negative-test')
    window.show()
    qtbot.wait(50)
    # Treat this private test desktop as the monitored desktop to exercise the
    # real detection path. Never deliberately leak a window onto Default.
    guard = runner.WindowGuard((name,))
    try:
        guard.check({0})  # Unrelated visible windows do not fail another run.
        with pytest.raises(runner.VisibleTestWindow) as error:
            guard.check({os.getpid()})
        assert any(row['title'] == window.windowTitle() for row in error.value.windows)
        assert all(row['pid'] == os.getpid() for row in error.value.windows)
    finally:
        guard.close()
