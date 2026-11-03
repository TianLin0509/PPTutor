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
    merge.run_test_gate(old['test'], accept_test_change=True)
    assert old['test'] == ['old-command'] and calls == ['new-hidden-command']


@pytest.mark.parametrize(('previous', 'updated'), [
    (['full-tests'], ['smoke-tests']),
    ([], ['full-tests']),
    (['full-tests'], []),
    (['first', 'second'], ['second', 'first']),
    (['pytest -q'], ['pytest -q -k smoke']),
], ids=['replace', 'add', 'remove', 'reorder', 'arguments'])
def test_changed_test_commands_require_explicit_acceptance(previous, updated, monkeypatch, capsys):
    merge = load_script('merge_task')
    monkeypatch.setattr(merge, 'load_config', lambda: {'test': updated})
    monkeypatch.setattr(merge, 'run', lambda *a, **kw: pytest.fail('must not execute unaccepted commands'))
    with pytest.raises(RuntimeError, match='--accept-test-change'):
        merge.run_test_gate(previous)
    output = capsys.readouterr().out
    assert '合并前测试命令' in output and '合并后测试命令' in output
    assert all(command in output for command in previous + updated)
    if not previous or not updated:
        assert '未配置' in output


def test_unchanged_test_commands_do_not_need_acceptance(monkeypatch):
    merge = load_script('merge_task')
    monkeypatch.setattr(merge, 'load_config', lambda: {'test': ['first', 'second']})
    calls = []
    monkeypatch.setattr(merge, 'run', lambda command, **kw: calls.append(command)
                        or type('Result', (), {'returncode': 0})())
    merge.run_test_gate(['first', 'second'])
    assert calls == ['first', 'second']


@pytest.mark.parametrize('accept_change', [False, True], ids=['unchanged', 'accepted-change'])
def test_failed_test_gate_stops_instead_of_continuing(monkeypatch, accept_change):
    merge = load_script('merge_task')
    monkeypatch.setattr(merge, 'load_config', lambda: {'test': ['first', 'second']})
    calls = []
    monkeypatch.setattr(merge, 'run', lambda command, **kw: calls.append(command)
                        or type('Result', (), {'returncode': 86})())
    with pytest.raises(RuntimeError, match='first'):
        merge.run_test_gate(['old-command'] if accept_change else ['first', 'second'],
                            accept_test_change=accept_change)
    assert calls == ['first']


def test_child_rejects_user_desktop_before_importing_pytest():
    runner = load_script('run_tests_hidden')
    with pytest.raises(ValueError, match='私有桌面'):
        runner.enter_test_desktop('Default')


def test_merge_relays_child_output_when_parent_stdout_is_redirected(tmp_path):
    child = tmp_path / 'printing_child.py'
    child.write_text("import sys\nprint('CHILD_STDOUT', flush=True)\n"
                     "print('CHILD_STDERR', file=sys.stderr, flush=True)\n"
                     "raise SystemExit(7)\n", 'utf-8')
    driver = tmp_path / 'gate_driver.py'
    driver.write_text(
        "import runpy\n"
        f"gate = runpy.run_path({str(ROOT / 'scripts/merge_task.py')!r})\n"
        f"result = gate['run']({subprocess.list2cmdline([sys.executable, str(child)])!r}, "
        "capture=False, check=False)\nraise SystemExit(result.returncode)\n", 'utf-8')
    result = subprocess.run([sys.executable, str(driver)], cwd=ROOT,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 7
    assert 'CHILD_STDOUT' in result.stdout and 'CHILD_STDERR' in result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows hidden-runner integration')
def test_hidden_runner_preserves_failed_pytest_output_and_progress(tmp_path):
    failing = tmp_path / 'test_failure_probe.py'
    failing.write_text('def test_intentional_failure():\n    assert False, "OUTPUT_PROBE_中文"\n', 'utf-8')
    report = tmp_path / 'hidden-report.json'
    command = subprocess.list2cmdline([
        sys.executable, str(ROOT / 'scripts/run_tests_hidden.py'), '--report', str(report),
        '--', '-q', str(failing)])
    driver = tmp_path / 'real_gate_driver.py'
    driver.write_text(
        "import runpy\n"
        f"gate = runpy.run_path({str(ROOT / 'scripts/merge_task.py')!r})\n"
        f"result = gate['run']({command!r}, capture=False, check=False)\n"
        "raise SystemExit(result.returncode)\n", 'utf-8')
    env = dict(os.environ)
    env.pop('PYTHONIOENCODING', None)
    result = subprocess.run(
        [sys.executable, str(driver)], cwd=ROOT, stdin=subprocess.DEVNULL, env=env,
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED ' in result.stdout and '1 failed' in result.stdout and 'OUTPUT_PROBE_中文' in result.stdout
    data = json.loads(report.read_text('utf-8'))
    output = Path(data['outputLog']).read_text('utf-8')
    assert 'FAILED ' in output and '1 failed' in output and 'OUTPUT_PROBE' in output
    assert data['pytest']['failures'][0]['nodeid'].endswith('::test_intentional_failure')
    assert data['pytest']['exitStatus'] == 1
    assert data['visibleWindows'] == []


def test_alternate_merge_script_cannot_change_repo_without_chdir(tmp_path, monkeypatch):
    merge = load_script('merge_task')
    monkeypatch.setattr(merge.sys, 'argv', ['merge_task.py', 'candidate', '--repo', str(tmp_path)])
    monkeypatch.setattr(merge, 'load_config', lambda: pytest.fail('must reject before reading config'))
    assert merge.main() == 2


@pytest.mark.parametrize('accept_change', [False, True], ids=['reject', 'accept'])
def test_first_merge_bootstrap_uses_new_gate_and_keeps_rollback(tmp_path, accept_change):
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
    old_command = subprocess.list2cmdline([sys.executable, '-c', 'raise SystemExit(17)'])
    config.write_text(json.dumps({'trunk': 'master', 'test': [old_command]}), 'utf-8')
    git('add', '.')
    git('commit', '-m', 'base with obsolete gate')
    original = git('rev-parse', 'HEAD')
    git('checkout', '-b', 'fix/new-gate')
    new_command = subprocess.list2cmdline([sys.executable, 'gate_probe.py'])
    config.write_text(json.dumps({'trunk': 'master', 'test': [new_command]}), 'utf-8')
    (tmp_path / 'gate_probe.py').write_text(
        "from pathlib import Path; Path('new-gate-ran').write_text('ok')", 'utf-8')
    git('add', '.')
    git('commit', '-m', 'candidate gate')
    candidate = git('rev-parse', 'HEAD')
    git('checkout', 'master')
    (tmp_path / 'unrelated.txt').write_text('keep unrelated work', 'utf-8')
    command = [sys.executable, str(ROOT / 'scripts/merge_task.py'),
               'fix/new-gate', '--repo', str(tmp_path), '--dry-run']
    if accept_change:
        command.append('--accept-test-change')
    result = run(command)
    assert result.returncode == (0 if accept_change else 1), result.stdout + result.stderr
    assert old_command in result.stdout and new_command in result.stdout
    assert (tmp_path / 'new-gate-ran').exists() is accept_change
    if accept_change:
        assert (tmp_path / 'new-gate-ran').read_text() == 'ok'
    else:
        assert '--accept-test-change' in result.stdout
    assert git('rev-parse', 'HEAD') == original
    assert git('rev-parse', 'fix/new-gate') == candidate
    assert not (tmp_path / '.git/MERGE_HEAD').exists()
    assert json.loads(config.read_text('utf-8'))['test'] == [old_command]
    assert not (tmp_path / 'gate_probe.py').exists()
    assert (tmp_path / 'unrelated.txt').read_text('utf-8') == 'keep unrelated work'


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
        assert all(row['windowClass'] and Path(row['processImage']).name.lower() == 'python.exe'
                   for row in error.value.windows)
    finally:
        guard.close()
