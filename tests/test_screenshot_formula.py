import json
from threading import Event
import pytest
from PySide6.QtCore import Qt,QRect,QPointF
from PySide6.QtGui import QImage,QColor
from pptx_finder.screenshots.formula_format import clean_latex,to_word,FormulaFormatError
from pptx_finder.screenshots import formula_component as component
from pptx_finder.screenshots.annotations import Mark
from pptx_finder.ui.screenshot_window import ScreenshotWindow
from pptx_finder.ui.screenshot_overlay import ScreenshotOverlay


def image():
    result=QImage(600,200,QImage.Format_RGB32);result.fill(QColor('white'));return result


@pytest.mark.parametrize('source,expected',[
    (r'\frac{a}{b+c}','(a)/(b+c)'),(r'\sqrt[3]{x}','√(3&x)'),
    (r'x_i^2','x_(i)^(2)'),(r'\sum_{i=1}^n x_i','∑_(i=1)^(n) x_(i)'),
    (r'\begin{pmatrix}a&b\\c&d\end{pmatrix}','(■(a&b@c&d))'),
    (r'\mathbb{R}','ℝ'),(r'\mathsf{x}',chr(0x1D5D1)),(r'\alpha+\beta=\gamma','α+β=γ'),
])
def test_word_structure(source,expected):assert to_word(source)==expected


@pytest.mark.parametrize('source',[r'\unknown{x}',r'\overbrace{x+y}',r'\color{red}{x}',r'\mathbf{x}',r'\boldsymbol{x}',r'\operatorname{rank}(A)'])
def test_unsupported_never_silently_drops_structure(source):
    with pytest.raises(FormulaFormatError):to_word(source)


def test_clean_and_bounds():
    assert clean_latex(r'\[ x^2 \]')=='x^2'
    for value in ['',None,123,'x'*4097,'x\0']:
        with pytest.raises(FormulaFormatError):clean_latex(value)


def window(qtbot,tmp_path,writer=None,reader=None,renderer=None):
    win=ScreenshotWindow(output_root=tmp_path,formula_recognizer=reader or (lambda im,**kw:r'\frac{a}{b}'),
                         formula_renderer=renderer or (lambda s,**kw:image()),formula_writer=writer or (lambda s:None))
    win.setAttribute(Qt.WA_DontShowOnScreen,True);win._formula.dialog.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(win);return win


def test_formula_uses_original_without_annotations(qtbot):
    overlay=ScreenshotOverlay(image(),QRect(0,0,600,200),QRect(20,20,300,100))
    overlay.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(overlay)
    overlay.marks=[Mark('arrow',QPointF(40,40),QPointF(140,40))]
    result=[];normal=[];overlay.formula_selected.connect(result.append);overlay.selected.connect(normal.append)
    overlay._confirm('formula');overlay._confirm('normal')
    assert result[0]!=normal[0] and result[0].pixelColor(20,20)==QColor('white')


def test_auto_latex_word_copy_edit_invalidates(qtbot,tmp_path):
    written=[];win=window(qtbot,tmp_path,written.append);win._formula_selected(image())
    qtbot.waitUntil(lambda:not win._busy,timeout=5000)
    dlg=win._formula.dialog
    assert written==[r'\frac{a}{b}'] and dlg.word.toPlainText()=='(a)/(b)'
    win._formula.copy('word');qtbot.waitUntil(lambda:not win._busy)
    assert written[-1]=='(a)/(b)'
    dlg.editor.setPlainText(r'\sqrt{x}')
    assert not dlg.word.toPlainText() and not dlg.word_btn.isEnabled()
    win._formula.refresh();qtbot.waitUntil(lambda:not win._busy)
    assert dlg.word.toPlainText()=='√(x)' and len(written)==2


def test_copy_failure_keeps_editable_result(qtbot,tmp_path):
    def fail(text):raise OSError('剪贴板占用')
    win=window(qtbot,tmp_path,fail);win._formula.start(image());qtbot.waitUntil(lambda:not win._busy)
    assert win._formula.dialog.editor.toPlainText()==r'\frac{a}{b}'
    assert '复制失败' in win._formula.dialog.status.text() and win._formula.dialog.word_btn.isEnabled()


def test_cancel_late_result_does_not_copy_or_replace(qtbot,tmp_path,monkeypatch):
    written=[];win=window(qtbot,tmp_path,written.append);jobs=[]
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win._formula.start(image());win._formula.dialog.reject();win._formula.start(image())
    jobs[0][1](jobs[0][0]());assert not written and win._busy
    jobs[1][1](jobs[1][0]());assert len(written)==1 and not win._busy


def test_owner_close_reopen_copy_has_fresh_handle(qtbot,tmp_path,monkeypatch):
    from pptx_finder.ui import screenshot_formula_controller as controller
    win=window(qtbot,tmp_path);calls=[];win._formula.writer=None
    monkeypatch.setattr(controller,'copy_text',lambda text,**kw:calls.append((text,kw)))
    win.close();win.show();win._formula.dialog.editor.setPlainText('x^2');win._formula.copy('latex')
    qtbot.waitUntil(lambda:not win._busy)
    assert calls[0][0]=='x^2' and calls[0][1]['hwnd']==int(win.winId())
    assert win._formula.dialog.latex_btn.isEnabled()


def test_unknown_disables_word_and_preview_failure_keeps_latex(qtbot,tmp_path):
    def fail(*a,**kw):raise ValueError('无法预览')
    written=[];win=window(qtbot,tmp_path,written.append,lambda *a,**kw:r'\unknown{x}',fail)
    win._formula.start(image());qtbot.waitUntil(lambda:not win._busy)
    assert written==[r'\unknown{x}'] and not win._formula.dialog.word_btn.isEnabled()
    assert 'Word' in win._formula.dialog.status.text() and '预览' in win._formula.dialog.status.text()


def test_partial_install_preserves_previous_active(tmp_path,monkeypatch):
    from pptx_finder import imgtext_ocr
    root=tmp_path/'formula';old=root/'runtime-old';old.mkdir(parents=True);(old/'pptdoctor-formula.exe').write_bytes(b'old')
    (root/'active.json').write_text(json.dumps({'runtime':'runtime-old'}))
    monkeypatch.setattr(component,'component_dir',lambda:root)
    def partial(*a,**kw):
        kw['target_dir'].mkdir();(kw['target_dir']/'partial').write_bytes(b'partial');raise OSError('复制失败')
    monkeypatch.setattr(imgtext_ocr,'install',partial)
    with pytest.raises(OSError):component.install(manifest={'files':{'pptdoctor-formula.exe':{}}})
    assert component.command()==[str(old/'pptdoctor-formula.exe')]
    assert list(root.glob('runtime-*'))==[old]


def test_cancel_before_pointer_swap_preserves_old(tmp_path,monkeypatch):
    from pptx_finder import imgtext_ocr
    root=tmp_path/'formula';root.mkdir();pointer=root/'active.json';pointer.write_text('{"runtime":"runtime-old"}')
    monkeypatch.setattr(component,'component_dir',lambda:root);event=Event()
    def late(*a,**kw):
        dest=kw['target_dir'];dest.mkdir();(dest/'pptdoctor-formula.exe').write_bytes(b'new');event.set();return '1'
    monkeypatch.setattr(imgtext_ocr,'install',late)
    from pptx_finder.screenshots.encoding import CaptureCancelled
    with pytest.raises(CaptureCancelled):component.install(cancelled=event,manifest={'files':{'pptdoctor-formula.exe':{}}})
    assert json.loads(pointer.read_text())['runtime']=='runtime-old'
    assert not list(root.glob('runtime-*'))


@pytest.mark.parametrize('name',['../runtime-bad','runtime-../bad','runtime-..\\bad',123])
def test_component_path_never_escapes(tmp_path,monkeypatch,name):
    monkeypatch.setattr(component,'component_dir',lambda:tmp_path)
    (tmp_path/'active.json').write_text(json.dumps({'runtime':name}))
    assert not component.is_installed()


def test_preview_script_injection_is_data():
    from pptx_finder.screenshots.formula_preview import preview_html
    html=preview_html(r'\text{</script><script>alert(1)</script>}')
    assert '\\u003c/script\\u003e' in html and 'trust:false' in html and 'maxExpand:100' in html
    assert '<script>alert(1)</script>' not in html and "default-src 'none'" in html


def test_install_bundle_never_downloads(tmp_path,monkeypatch):
    from pptx_finder import imgtext_ocr
    bundle=tmp_path/'bundled';bundle.mkdir();(bundle/'component.zip').write_bytes(b'archive')
    (bundle/'component.json').write_text('{"files":{"pptdoctor-formula.exe":{}},"version":"1"}')
    root=tmp_path/'installed';calls=[]
    monkeypatch.setattr(component,'component_dir',lambda:root);monkeypatch.setattr(component,'bundled_dir',lambda:bundle)
    def install(payload,**kw):
        calls.append(kw['archive_path']);kw['target_dir'].mkdir();(kw['target_dir']/'pptdoctor-formula.exe').write_bytes(b'exe');return '1'
    monkeypatch.setattr(imgtext_ocr,'install',install)
    monkeypatch.setattr(component,'fetch_manifest',lambda:pytest.fail('离线组件不得下载'))
    assert component.install()=='1' and calls==[bundle/'component.zip'] and component.command()


def test_progress_from_cancelled_install_does_not_override_new(qtbot,tmp_path,monkeypatch):
    win=window(qtbot,tmp_path);jobs=[]
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win._formula.start(image());old=win._serial;win._formula.dialog.reject();win._formula.start(image())
    current=win._formula.dialog.status.text();win._formula._progress(old,'旧进度')
    assert win._formula.dialog.status.text()==current
    win.close();jobs[-1][1]((None,''));assert not win._busy


def test_missing_component_after_owner_close_can_retry_install(qtbot,tmp_path,monkeypatch):
    win=window(qtbot,tmp_path);win._formula.recognizer=component.recognize
    monkeypatch.setattr(component,'is_installed',lambda:False);jobs=[]
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win._formula.start(image());win._formula.install();assert win._busy
    win.close();win._formula.start(image())
    assert win._formula.dialog.install_btn.isEnabled() and not win._busy
    jobs[0][1]('');assert len(jobs)==1 and win._formula.dialog.install_btn.isEnabled()


def test_private_helper_timeout_only_kills_own_process(tmp_path):
    import os,sys,time,subprocess
    from pptx_finder.screenshots.formula_process import run
    # An independent control child must survive cleanup of the timed-out job.
    opts={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    control=subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)'],**opts)
    try:
        with pytest.raises(TimeoutError):run([sys.executable,'-c','import time;time.sleep(15)'],timeout=.1)
        assert control.poll() is None
    finally:control.terminate();control.wait(timeout=5)


@pytest.mark.parametrize('source',[r'\mathchoice{A}{B}{C}{D}',r'\begin{gathered}a=b\\c=d\end{gathered}'])
def test_unsupported_cross_format_meaning_is_rejected(source):
    with pytest.raises(FormulaFormatError):to_word(source)


def test_cancel_during_pointer_write_cannot_replace_active(tmp_path,monkeypatch):
    from pathlib import Path
    from pptx_finder import imgtext_ocr
    from pptx_finder.screenshots.encoding import CaptureCancelled
    root=tmp_path/'formula';root.mkdir();pointer=root/'active.json';pointer.write_text('{"runtime":"runtime-old"}')
    monkeypatch.setattr(component,'component_dir',lambda:root);event=Event();original=Path.write_text
    def write(path,*a,**kw):
        value=original(path,*a,**kw)
        if path.name.startswith('active-'):event.set()
        return value
    def installed(*a,**kw):
        dest=kw['target_dir'];dest.mkdir();(dest/'pptdoctor-formula.exe').write_bytes(b'new');return '1'
    monkeypatch.setattr(Path,'write_text',write);monkeypatch.setattr(imgtext_ocr,'install',installed)
    with pytest.raises(CaptureCancelled):component.install(cancelled=event,manifest={'files':{'pptdoctor-formula.exe':{}}})
    assert json.loads(pointer.read_text())['runtime']=='runtime-old'


@pytest.mark.parametrize('width,height',[(4000,1),(1,4000),(6000,4000),(1,1),(96,32)])
def test_helper_extreme_aspect_ratio_has_bounded_resize(width,height):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('formula_worker',Path(__file__).parents[1]/'tools/formula_sidecar/worker.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    fitted=module.fitted_size(width,height)
    assert min(fitted)>=1 and max(fitted)<=384 and fitted[0]*fitted[1]<=384*384


def test_formula_source_resets_previous_partial_long_capture(qtbot,tmp_path):
    win=window(qtbot,tmp_path);win._original_scroll_message='旧长图已停止';win._original_complete=False
    win._formula.start(image());qtbot.waitUntil(lambda:not win._busy)
    assert win._original_scroll_message=='' and win._original_complete


def test_preview_removes_blank_margins_without_trimming_formula(qapp):
    from pptx_finder.screenshots.formula_preview import trim_white
    source=QImage(1400,300,QImage.Format_RGB32);source.fill(QColor('white'))
    for x,y in [(650,110),(730,160)]:source.setPixelColor(x,y,QColor('black'))
    result=trim_white(source)
    assert result.width()<150 and result.height()<100
    assert result.pixelColor(16,16)==QColor('black')
