"""Exercise the shipped file protocol without importing Doctor in the runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import zipfile


def read_json(path):
    for attempt in range(20):
        try:
            return json.loads(path.read_text('utf-8'))
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.015)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('work', type=Path)
    parser.add_argument('--assist-image', type=Path)
    args = parser.parse_args()
    exe, work = args.exe.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    marker = read_json(exe.with_name('hub-api.json'))
    assert marker['apiVersion'] == 1
    assert marker['executable'] == exe.name
    assert marker['exeSha256'] == hashlib.sha256(exe.read_bytes()).hexdigest()
    env = dict(os.environ, PPTX_FINDER_DATA_DIR=str(work / 'data'), APPDATA=str(work / 'appdata'))
    for key in ('PPTUTOR_OCR_CMD', 'PPTUTOR_FORMULA_CMD', 'QT_QPA_PLATFORM'):
        env.pop(key, None)
    checks = []

    def call(action, data=None, expected=0):
        number = len(checks)
        request = {'apiVersion': 1, 'requestId': f'frozen-{number}', 'db': str(work/'index.db'), **(data or {})}
        source, target = work/f'{number}-request.json', work/f'{number}-response.json'
        source.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        start = time.perf_counter()
        result = subprocess.run([str(exe), '--hub', action, '--in', str(source), '--out', str(target)],
                                env=env, timeout=240, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        response = read_json(target)
        checks.append({'action': action, 'exit': result.returncode, 'seconds': time.perf_counter()-start,
                       'elapsedMs': response['elapsedMs']})
        (work/'checks.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
        assert result.returncode == expected, response
        assert response['requestId'] == request['requestId'] and response['action'] == action
        assert response['apiVersion'] == 1 and response['ok'] == (expected == 0), response
        return response

    def report_request(evidence):
        page = evidence['pages'][0]
        return {'decision': {'task': {'purpose': '冻结验证', 'conditions': []}, 'results': [
            {'id': page['id'], 'quote': 'FrozenHubUnique', 'relevance': '直接适用',
             'use': '检索验证', 'reason': '原文匹配', 'conditions': '无额外条件', 'assessments': []}]},
                'evidence': evidence}

    assert not call('status')['available']
    assert not (work/'index.db').exists()
    # Fixture generation uses the test runner's dependencies, never the exe's.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tests'))
    from fixtures_gen import make_pptx
    decks = work/'decks'
    decks.mkdir()
    make_pptx(decks/'fixture.pptx', [{'body': '无线调度 FrozenHubUnique'}, {'body': '第二页 波束赋形'}])
    call('index', {'folders': [str(decks)]})
    assert call('status')['available']
    found = call('search', {'queries': ['FrozenHubUnique']})['candidates']
    assert found and found[0]['stableId'] and found[0]['ref']['source_id']
    evidence = call('read', {'refs': [found[0]['ref']]})
    page = evidence['pages'][0]
    assert page['ref']['page_hash'] == found[0]['ref']['page_hash']
    report = call('report', report_request(evidence))
    call('unknown-action', expected=2)
    if args.assist_image:
        seed = call('imgtext-seed', {'image': str(args.assist_image.resolve()), 'workdir': str(work/'seed')})
        built = call('imgtext-build', {'workdir': str(work/'seed'), 'seedHash': seed['seedHash'], 'ops': {}})
        with zipfile.ZipFile(built['pptx']) as archive:
            assert b'<a:t>' in archive.read('ppt/slides/slide1.xml')
    if 'serve' in marker['capabilities']:
        spool = work/'spool'
        started = time.perf_counter()
        log = (work/'serve-process.log').open('wb')
        server = subprocess.Popen([str(exe), '--hub', 'serve', '--spool', str(spool)], env=env,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        def wait(path):
            deadline = time.monotonic()+240
            while time.monotonic()<deadline:
                if path.exists():
                    return read_json(path)
                assert server.poll() is None, '常驻进程提前退出'
                time.sleep(.05)
            raise TimeoutError(str(path))

        def queued(action, data):
            job = uuid.uuid4().hex
            request = {'apiVersion': 1, 'requestId': job, 'action': action,
                       'db': str(work/'index.db'), **data}
            source = spool/'requests'/(job+'.json')
            temporary = source.with_suffix('.tmp')
            temporary.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
            start = time.perf_counter()
            temporary.replace(source)
            result = wait(spool/'responses'/(job+'.json'))
            checks.append(dict(action='serve:'+action,seconds=time.perf_counter()-start,
                               elapsedMs=result['elapsedMs']))
            (work/'checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
            assert result['ok'] and result['requestId']==job and result['action']==action, result
            return result

        try:
            info = wait(spool/'server.json')
            assert info['pid'] == server.pid and info['idleTimeoutSec'] == 600
            checks.append(dict(action='serve:startup',seconds=time.perf_counter()-started))
            warm_found = queued('search',{'queries':['FrozenHubUnique']})['candidates']
            assert warm_found == found
            warm_evidence = queued('read',{'refs':[warm_found[0]['ref']]})
            warm_page = warm_evidence['pages'][0]
            assert warm_page == page
            warm_report = queued('report',report_request(warm_evidence))
            for field in ('report','recommendations','verified_rows'):
                assert warm_report[field] == report[field]
        finally:
            if server.poll() is None and (spool/'server.json').exists():
                temporary = spool/'stop.tmp'
                temporary.write_text(json.dumps({'instanceId':read_instance(spool)}),encoding='utf-8')
                temporary.replace(spool/'stop.json')
                try:
                    server.wait(30)
                except subprocess.TimeoutExpired:
                    server.kill()  # exact child owned by this isolated probe
                    server.wait()
                    raise
            elif server.poll() is None:
                server.kill()
                server.wait()
            log.close()
        assert server.returncode == 0
    print(json.dumps({'passed': True, 'checks': checks}, ensure_ascii=False))


def read_instance(spool):
    return read_json(spool/'server.json')['instanceId']


if __name__ == '__main__':
    main()
