import json
import sys
import time
from threading import Event, Timer

import pytest

from pptx_finder.ocr_session import OcrSession
from pptx_finder.imgtext_ocr import OcrUnavailable
from pptx_finder.screenshots.encoding import CaptureCancelled


@pytest.fixture
def server(tmp_path):
    source = tmp_path/'server.py'
    source.write_text('''import json, sys, time, os
for line in sys.stdin:
    r=json.loads(line)
    if r['images']==['wait']:time.sleep(5)
    if r['images']==['exit']:sys.exit(7)
    p={'id':r['id'],'ok':r['images']!=['error'],'error':'bad image',
       'results':{path:[{'text':str(os.getpid()),'box':[0,0,1,1],'score':1}] for path in r['images']}}
    print('PPTDOCTOR_OCR:'+json.dumps(p),flush=True)
''', encoding='utf-8')
    return [sys.executable, str(source)]


def test_reuse_idle_shutdown_and_restart(server):
    session = OcrSession(idle_seconds=.2)
    try:
        first = session.recognize(server, ['a'])['a'][0]['text']
        assert session.recognize(server, ['a'])['a'][0]['text'] == first
        process = session._process
        deadline = time.monotonic()+4
        while process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert process.poll() is not None
        assert session.recognize(server, ['a'])['a'][0]['text'] != first
    finally:session.close()


@pytest.mark.parametrize('path', ['error', 'exit', 'wait'])
def test_failure_or_timeout_surfaces_and_reaps(server, path):
    session = OcrSession(timeout=.4)
    try:
        with pytest.raises(OcrUnavailable):session.recognize(server, [path])
        assert session._process is None
        assert session.recognize(server, ['a'])['a']
    finally:session.close()


def test_cancel_running_model_reaps_child(server):
    session = OcrSession()
    cancel = Event()
    timer = Timer(.3, cancel.set);timer.start()
    try:
        with pytest.raises(CaptureCancelled):session.recognize(server, ['wait'], cancelled=cancel)
        assert session._process is None
    finally:timer.cancel();session.close()
