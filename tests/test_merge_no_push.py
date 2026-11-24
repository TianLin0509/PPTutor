"""Exercise the real merge lifecycle in disposable local repositories; never push."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def command(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', check=True,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    command(repo, 'git', 'init', '-b', 'main')
    command(repo, 'git', 'config', 'user.name', 'Isolated test')
    command(repo, 'git', 'config', 'user.email', 'test@example.invalid')
    command(repo, 'git', 'config', 'core.hooksPath', str(tmp_path / 'empty-hooks'))
    (repo / '.agents').mkdir()
    (repo / 'version.txt').write_text('1', encoding='utf-8')
    (repo / 'feature.txt').write_text('base', encoding='utf-8')
    (repo / 'keep.txt').write_text('original', encoding='utf-8')
    (repo / '.gitignore').write_text('gate.log\nafter.log\nfail-gate\n', encoding='utf-8')
    (repo / 'bump.py').write_text("from pathlib import Path\nPath('version.txt').write_text('2')\n", encoding='utf-8')
    (repo / 'gate.py').write_text(
        "from pathlib import Path\nimport subprocess\n"
        "assert Path('version.txt').read_text() == '2'\n"
        "assert Path('feature.txt').read_text() == 'candidate'\n"
        "assert subprocess.run(['git', 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], "
        "capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode == 0\n"
        "Path('gate.log').write_text('gate-before-commit')\n"
        "raise SystemExit(1 if Path('fail-gate').exists() else 0)\n", encoding='utf-8')
    (repo / 'after.py').write_text("from pathlib import Path\nPath('after.log').write_text('after-commit')\n", encoding='utf-8')
    quoted = lambda name: subprocess.list2cmdline([sys.executable, name])
    (repo / '.agents' / 'project.json').write_text(json.dumps({
        'name': 'Isolated merge test', 'trunk': 'main', 'test': [quoted('gate.py')],
        'versionBump': [quoted('bump.py')], 'versionFiles': ['version.txt'],
        'afterMerge': [quoted('after.py')]}), encoding='utf-8')
    command(repo, 'git', 'add', '.')
    command(repo, 'git', 'commit', '-m', 'base')
    command(repo, 'git', 'checkout', '-b', 'candidate')
    (repo / 'feature.txt').write_text('candidate', encoding='utf-8')
    command(repo, 'git', 'commit', '-am', 'candidate')
    command(repo, 'git', 'checkout', 'main')
    origin = tmp_path / 'local-origin.git'
    command(tmp_path, 'git', 'init', '--bare', str(origin))
    command(repo, 'git', 'remote', 'add', 'origin', str(origin))
    return repo


def load_merge(repo, monkeypatch, *, no_push=True, extra=(), allow_push_probe=False):
    spec = importlib.util.spec_from_file_location('isolated_merge_no_push', ROOT / 'scripts' / 'merge_task.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO, module.CONFIG = repo, repo / '.agents' / 'project.json'
    monkeypatch.setattr(sys, 'argv', ['merge_task.py', 'candidate', *(['--no-push'] if no_push else []), *extra])
    calls = []
    real_run = module.run

    def guarded_run(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(cmd, list) and cmd[:2] == ['git', 'push']:
            assert allow_push_probe, 'no-push must never attempt git push'
            # The default path is observed without issuing even a local git push.
            return subprocess.CompletedProcess(cmd, 0, '', '')
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(module, 'run', guarded_run)
    return module, calls


def test_no_push_commits_after_gate_and_preserves_unrelated_changes(repository, monkeypatch):
    repo = repository
    original = command(repo, 'git', 'rev-parse', 'main')
    (repo / 'keep.txt').write_text('unsaved user work', encoding='utf-8')
    module, calls = load_merge(repo, monkeypatch)
    try:
        assert module.main() == 0
    finally:
        module.release_merge_lock()
    assert command(repo, 'git', 'rev-parse', 'HEAD') != original
    assert len(command(repo, 'git', 'show', '-s', '--format=%P', 'HEAD').split()) == 2
    assert (repo / 'version.txt').read_text() == '2'
    assert (repo / 'keep.txt').read_text() == 'unsaved user work'
    assert (repo / 'gate.log').read_text() == 'gate-before-commit'
    assert (repo / 'after.log').read_text() == 'after-commit'
    assert not any(isinstance(c, list) and c[:2] == ['git', 'push'] for c in calls)


def test_default_still_attempts_push_after_local_commit(repository, monkeypatch):
    module, calls = load_merge(repository, monkeypatch, no_push=False, allow_push_probe=True)
    try:
        assert module.main() == 0
    finally:
        module.release_merge_lock()
    push = calls.index(['git', 'push', 'origin', 'main'])
    commit = next(i for i, cmd in enumerate(calls) if isinstance(cmd, list) and cmd[:2] == ['git', 'commit'])
    assert commit < push


@pytest.mark.parametrize('dry_run', [False, True], ids=['failed-gate', 'dry-run'])
def test_no_push_rollback_preserves_user_work(repository, monkeypatch, dry_run):
    repo = repository
    original = command(repo, 'git', 'rev-parse', 'HEAD')
    (repo / 'keep.txt').write_text('keep unsaved', encoding='utf-8')
    if not dry_run:
        (repo / 'fail-gate').touch()
    module, calls = load_merge(repo, monkeypatch, extra=['--dry-run'] if dry_run else [])
    try:
        assert module.main() == (0 if dry_run else 1)
    finally:
        module.release_merge_lock()
    assert command(repo, 'git', 'rev-parse', 'HEAD') == original
    assert (repo / 'feature.txt').read_text() == 'base'
    assert (repo / 'version.txt').read_text() == '1'
    assert (repo / 'keep.txt').read_text() == 'keep unsaved'
    assert (repo / 'gate.log').exists()
    assert not (repo / 'after.log').exists()
    assert not any(isinstance(c, list) and c[:2] == ['git', 'push'] for c in calls)


def test_no_push_does_not_bypass_overlapping_dirty_guard(repository, monkeypatch):
    repo = repository
    original = command(repo, 'git', 'rev-parse', 'HEAD')
    (repo / 'feature.txt').write_text('in-progress edit', encoding='utf-8')
    module, _ = load_merge(repo, monkeypatch)
    try:
        with pytest.raises(SystemExit) as ex:
            module.main()
        assert ex.value.code == 2
    finally:
        module.release_merge_lock()
    assert command(repo, 'git', 'rev-parse', 'HEAD') == original
    assert (repo / 'feature.txt').read_text() == 'in-progress edit'
    assert not (repo / 'gate.log').exists()


def test_no_push_keeps_main_worktree_and_lock_guards(repository, monkeypatch):
    module, _ = load_merge(repository, monkeypatch)
    monkeypatch.setattr(module, 'main_worktree', lambda: repository.parent / 'another-main')
    try:
        with pytest.raises(SystemExit) as ex:
            module.main()
        assert ex.value.code == 2
    finally:
        module.release_merge_lock()
    monkeypatch.setattr(module, 'acquire_merge_lock', lambda: False)
    assert module.main() == 2
    assert not (repository / 'gate.log').exists()
