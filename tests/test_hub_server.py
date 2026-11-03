import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

from pptx_finder import hub_cli, hub_server


def read(path):
    return json.loads(hub_cli.retry_file_change(path.read_text, 'utf-8'))


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('condition did not become ready')


def request(spool, action='status', **data):
    job = uuid.uuid4().hex
    payload = dict(apiVersion=1, requestId=job, action=action,
                   db=str(spool / 'absent.db'), **data)
    path = spool / 'requests' / (job + '.json')
    hub_cli.write_json(path, payload)
    return path, payload


def launch(spool, **kwargs):
    results, errors = [], []

    def worker():
        try:
            results.append(hub_server.serve(spool, heartbeat_interval=.02,
                                           poll_interval=.01, **kwargs))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    wait_for(lambda: (spool / 'server.json').exists() or errors)
    assert not errors
    return thread, results, errors


def stop(spool, running):
    thread, results, errors = running
    hub_cli.write_json(spool / 'stop.json', {'instanceId': read(spool/'server.json')['instanceId']})
    thread.join(10)
    assert not thread.is_alive()
    assert not errors
    assert results == [0]


def test_multiple_requests_own_responses_and_duplicate_is_not_replayed(tmp_path):
    calls = []

    def execute(argv):
        calls.append(argv)
        return hub_cli.main(argv, allow_server=False)

    running = launch(tmp_path, run_request=execute)
    try:
        for _ in range(2):
            path, payload = request(tmp_path)
            out = tmp_path/'responses'/path.name
            wait_for(out.exists)
            result = read(out)
            assert result['requestId'] == payload['requestId'] and result['ok']
            assert 'serve' in result['capabilities']
            wait_for(lambda: not (tmp_path/'working'/path.name).exists())
        assert len(calls) == 2
        hub_cli.write_json(path, payload)
        wait_for(lambda: not path.exists())
        assert len(calls) == 2
        assert not (tmp_path/'absent.db').exists()
    finally:
        stop(tmp_path, running)


def test_busy_heartbeat_and_stop_finishes_current_request_without_claiming_next(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def blocked(argv):
        entered.set()
        assert release.wait(5)
        return hub_cli.main(argv, allow_server=False)

    first, _ = request(tmp_path)
    running = launch(tmp_path, run_request=blocked, idle_timeout=.15)
    try:
        assert entered.wait(5)
        before = read(tmp_path/'server.json')
        assert before['state'] == 'busy'
        # Busy work must outlive the idle deadline and keep refreshing heartbeat.
        wait_for(lambda: read(tmp_path/'server.json')['heartbeatAt'] != before['heartbeatAt'])
        time.sleep(.18)
        assert running[0].is_alive()
        second, _ = request(tmp_path)
        hub_cli.write_json(tmp_path/'stop.json', {'instanceId': before['instanceId']})
        release.set()
        running[0].join(5)
        assert not running[0].is_alive() and not running[2]
        assert read(tmp_path/'responses'/first.name)['ok']
        assert second.exists() and not (tmp_path/'responses'/second.name).exists()
    finally:
        release.set()
        running[0].join(5)


def test_stale_stop_is_ignored_then_idle_exits(tmp_path):
    hub_cli.write_json(tmp_path/'stop.json', {'instanceId': 'old-instance'})
    path, _ = request(tmp_path)
    running = launch(tmp_path, idle_timeout=.25)
    wait_for(lambda: (tmp_path/'responses'/path.name).exists())
    running[0].join(5)
    assert running[1] == [0] and not running[2]
    assert read(tmp_path/'server.json')['state'] == 'stopping'


@pytest.mark.parametrize('action,retryable', [('search',True),('report',True),('index',False),('imgtext-build',False)])
def test_recovery_marks_interrupted_without_executing(tmp_path, action, retryable):
    path, payload = request(tmp_path, action)
    (tmp_path/'working').mkdir()
    path.rename(tmp_path/'working'/path.name)

    def never(argv):
        pytest.fail('interrupted work was replayed')

    hub_server.serve(tmp_path, run_request=never, idle_timeout=.02, poll_interval=.005)
    result = read(tmp_path/'responses'/path.name)
    assert result['code'] == 'REQUEST_INTERRUPTED'
    assert result['requestId'] == payload['requestId']
    assert result['retryable'] is retryable and result['resultMayBePartial'] is not retryable
    assert not (tmp_path/'working'/path.name).exists()


def test_recovery_preserves_completed_result_and_job_conflict_does_not_execute(tmp_path):
    path, payload = request(tmp_path)
    out = tmp_path/'responses'/path.name
    hub_cli.main(['--hub','status','--in',str(path),'--out',str(out)])
    old = out.read_bytes()
    (tmp_path/'working').mkdir()
    path.rename(tmp_path/'working'/path.name)
    hub_server.serve(tmp_path, idle_timeout=.02, poll_interval=.005)
    assert out.read_bytes() == old
    hub_cli.write_json(path, {**payload,'requestId':'different-job'})
    with pytest.raises(hub_cli.HubError, match='jobId'):
        hub_server.serve(tmp_path, idle_timeout=.1, poll_interval=.005)
    assert path.exists() and out.read_bytes() == old


@pytest.mark.parametrize('data,code', [('broken-json','INVALID_REQUEST'),
    ('[]','INVALID_REQUEST'), ('{"apiVersion":1,"requestId":"bad","action":"serve"}','UNKNOWN_ACTION')])
def test_malformed_request_gets_failure_and_next_request_succeeds(tmp_path, data, code):
    running = launch(tmp_path)
    try:
        path = tmp_path/'requests'/(uuid.uuid4().hex+'.json')
        temporary=path.with_suffix('.tmp')
        temporary.write_text(data,encoding='utf-8')
        temporary.replace(path)
        out = tmp_path/'responses'/path.name
        wait_for(out.exists)
        assert read(out)['code'] == code
        good, _ = request(tmp_path)
        wait_for(lambda: (tmp_path/'responses'/good.name).exists())
        assert read(tmp_path/'responses'/good.name)['ok']
    finally:
        stop(tmp_path, running)


def test_lock_contender_in_another_process_cannot_change_instance(tmp_path):
    spool=tmp_path/'spool'
    spool.mkdir()
    info=spool/'server.json'
    info.write_text('{"existing":"owner"}',encoding='utf-8')
    with hub_server.SpoolLock(spool/'server.lock') as owned:
        assert owned
        result = subprocess.run([sys.executable,'-m','pptx_finder','--hub','serve','--spool',str(spool)],
            timeout=30, capture_output=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode == 0, result.stderr
        assert read(info) == {'existing':'owner'}
        assert not (spool/'requests').exists()


def test_internal_error_is_logged_and_work_survives_for_recovery(tmp_path, monkeypatch):
    path, _ = request(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError('injected internal failure')
    real_serve = hub_server.serve
    monkeypatch.setattr(hub_server,'serve',lambda spool: real_serve(spool,run_request=fail))
    monkeypatch.setattr(hub_server,'_background_priority',lambda:None)
    assert hub_server.main(['--hub','serve','--spool',str(tmp_path)]) == 4
    assert 'injected internal failure' in (tmp_path/'server-error.log').read_text('utf-8')
    assert (tmp_path/'working'/path.name).exists()


def test_abrupt_process_exit_releases_lock_and_write_is_not_replayed(tmp_path):
    spool=tmp_path/'queue'
    path, _ = request(spool,'index')
    side_effect=tmp_path/'written-once.txt'
    program = '''
import os, sys
from pathlib import Path
from pptx_finder.hub_server import serve
def crash(argv):
    Path(sys.argv[2]).write_text('once',encoding='utf-8')
    os._exit(7)
serve(Path(sys.argv[1]),run_request=crash)
'''
    result = subprocess.run([sys.executable,'-c',program,str(spool),str(side_effect)],
        capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode == 7,result.stderr
    assert (spool/'working'/path.name).exists()
    hub_server.serve(spool,idle_timeout=.02,poll_interval=.005)
    response=read(spool/'responses'/path.name)
    assert response['code']=='REQUEST_INTERRUPTED' and not response['retryable']
    assert side_effect.read_text('utf-8')=='once'


def test_invalid_directory_and_job_names_preserve_inputs(tmp_path):
    unrelated=tmp_path/'keep.txt'
    unrelated.write_text('user data')
    with pytest.raises(hub_cli.HubError,match='专用目录'):
        hub_server.serve(tmp_path,idle_timeout=.01)
    assert unrelated.read_text() == 'user data'
    spool=tmp_path/'queue'
    (spool/'requests').mkdir(parents=True)
    bad=spool/'requests'/'bad-job.json'
    bad.write_text('{}')
    with pytest.raises(hub_cli.HubError,match='jobId'):
        hub_server.serve(spool,idle_timeout=.05)
    assert bad.read_text() == '{}'


def test_request_reparse_point_is_rejected_without_touching_target(tmp_path):
    target=tmp_path/'original.json'
    target.write_text('{}')
    spool=tmp_path/'queue'
    (spool/'requests').mkdir(parents=True)
    link=spool/'requests'/(uuid.uuid4().hex+'.json')
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    with pytest.raises(hub_cli.HubError,match='链接'):
        hub_server.serve(spool,idle_timeout=.02)
    assert target.read_text() == '{}'


def test_busy_heartbeat_retries_brief_windows_reader_lock(tmp_path, monkeypatch):
    real_replace=hub_cli.os.replace
    attempts=[]
    def occupied(source,target):
        if Path(target).name=='server.json' and read(Path(source))['state']=='busy':
            attempts.append(1)
            if len(attempts)<3:
                raise PermissionError('brief reader sharing violation')
        return real_replace(source,target)
    monkeypatch.setattr(hub_cli.os,'replace',occupied)
    running=launch(tmp_path)
    try:
        path,_=request(tmp_path)
        wait_for(lambda: (tmp_path/'responses'/path.name).exists() or running[2])
        assert not running[2]
        assert read(tmp_path/'responses'/path.name)['ok']
        assert len(attempts)>=3
    finally:
        if running[0].is_alive():
            stop(tmp_path,running)


def test_permanent_reader_lock_fails_without_replacing_previous_json(tmp_path,monkeypatch):
    target=tmp_path/'keep.json'
    hub_cli.write_json(target,{'old':True})
    attempts=[]
    def occupied(*_):
        attempts.append(1)
        raise PermissionError('permanent reader lock')
    monkeypatch.setattr(hub_cli.os,'replace',occupied)
    with pytest.raises(PermissionError):
        hub_cli.write_json(target,{'new':True})
    assert read(target)=={'old':True}
    assert len(attempts)==20
    assert not list(tmp_path.glob('*.tmp'))


def test_client_can_remove_published_response_immediately(tmp_path,monkeypatch):
    real_write=hub_cli.write_json
    consumed=[]
    def immediate_reader(path,payload):
        real_write(path,payload)
        if path.parent.name=='responses':
            consumed.append(read(path))
            path.unlink()
    monkeypatch.setattr(hub_cli,'write_json',immediate_reader)
    # Atomic publish may use replace after validating a private staging result.
    real_replace=hub_cli.os.replace
    def immediate_publish(source,target):
        result=real_replace(source,target)
        if Path(target).parent.name=='responses':
            consumed.append(read(Path(target)))
            Path(target).unlink()
        return result
    monkeypatch.setattr(hub_cli.os,'replace',immediate_publish)
    running=launch(tmp_path)
    try:
        path,payload=request(tmp_path)
        wait_for(lambda: consumed or running[2])
        wait_for(lambda: not (tmp_path/'working'/path.name).exists() or running[2])
        assert not running[2]
        assert consumed[0]['requestId']==payload['requestId'] and consumed[0]['ok']
    finally:
        if running[0].is_alive():
            stop(tmp_path,running)


def test_request_reader_retries_temporary_windows_lock(tmp_path,monkeypatch):
    request=tmp_path/'request.json'
    request.write_text('{"action":"status","requestId":"read-busy"}',encoding='utf-8')
    original=Path.read_text
    attempts=[]
    def occupied(path,*args,**kwargs):
        if path==request:
            attempts.append(1)
            if len(attempts)<3:
                raise PermissionError('brief file scanner lock')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',occupied)
    assert hub_server._request(request)['requestId']=='read-busy'
    assert len(attempts)==3
