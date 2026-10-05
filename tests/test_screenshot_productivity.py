from pathlib import Path
from threading import Event
import ctypes
from ctypes import wintypes
import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
from pptx_finder.screenshots.annotations import Mark, annotated
from pptx_finder.screenshots.preferences import CapturePreferences
from pptx_finder.screenshots.regions import binding, matching_region
from pptx_finder.screenshots.text import recognize_image, ordered_text
from pptx_finder.screenshots.encoding import CaptureCancelled
from pptx_finder.ui.screenshot_overlay import ScreenshotOverlay
from pptx_finder.ui.screenshot_window import ScreenshotWindow
from pptx_finder.ui.screenshot_shortcuts import ScreenshotShortcuts, parse_key, IDS


def image():
    result=QImage(800,450,QImage.Format_RGB32);result.fill(QColor('white'));return result


def hidden(qtbot,widget):
    widget.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(widget);return widget


@pytest.mark.parametrize('mode',['arrow','ellipse','rect'])
def test_draw_undo_and_deliver_marks_at_physical_scale(qtbot,mode):
    source=image()
    overlay=hidden(qtbot,ScreenshotOverlay(source,QRect(0,0,400,225),QRect(20,20,300,170)))
    overlay.show()
    QTest.mouseClick(overlay.tools[mode],Qt.LeftButton)
    QTest.mousePress(overlay,Qt.LeftButton,pos=QPoint(60,60))
    QTest.mouseRelease(overlay,Qt.LeftButton,pos=QPoint(140,110))
    assert len(overlay.marks)==1 and not overlay.scroll_btn.isEnabled()
    normal,ocr=[],[]
    overlay.selected.connect(normal.append);overlay.text_selected.connect(ocr.append)
    overlay._confirm('normal');overlay._confirm('text')
    assert normal[0].size()==ocr[0].size() and normal[0]!=ocr[0]
    assert source==image() and ocr[0].pixelColor(80,80)==QColor('white')
    QTest.keyClick(overlay,Qt.Key_Z,Qt.ControlModifier)
    assert not overlay.marks and overlay.scroll_btn.isEnabled()
    overlay._confirm('normal');assert normal[1]==ocr[0]


def test_arrow_native_pixels_and_original_not_modified(qapp):
    source=image()
    result=annotated(source,[Mark('arrow',QPointF(40,40),QPointF(90,40))],QRect(20,20,400,225))
    assert result.pixelColor(60,40).red()>200 and result.pixelColor(60,40).green()<100
    assert source.pixelColor(60,40)==QColor('white')


def test_preferences_merge_preserves_region_and_keys(tmp_path):
    path=tmp_path/'prefs.json';a=CapturePreferences(path);b=CapturePreferences(path)
    a.update(region={'screen':'mock','bounds':[0,0,400,225],'pixels':[800,450],'area':[20,20,100,50]})
    b.update(keys={'capture':'Alt+F9','repeat':''})
    reread=CapturePreferences(path)
    assert reread.region()['area']==[20,20,100,50] and reread.keys()['repeat']==''
    path.write_text('broken',encoding='utf-8')
    with pytest.raises(ValueError):a.update(keys={})
    assert path.read_text()=='broken'


class Screen:
    def __init__(self,geometry=QRect(-400,0,400,225)):self.geo=geometry
    def geometry(self):return self.geo
    def name(self):return 'fixture-screen'


def test_repeat_validates_monitor_and_dpi(qapp):
    screen=Screen();saved=binding(screen,image(),QRect(20,20,100,50))
    assert matching_region(saved,screen,image())==QRect(20,20,100,50)
    assert matching_region(saved,Screen(QRect(0,0,400,225)),image()) is None
    assert matching_region(saved,screen,image().scaled(400,225)) is None


def test_repeat_keyboard_confirmation_and_cancel(qtbot):
    overlay=hidden(qtbot,ScreenshotOverlay(image(),QRect(0,0,800,450),QRect(30,40,120,80)))
    normal,cancelled=[],[];overlay.selected.connect(normal.append);overlay.cancelled.connect(lambda:cancelled.append(True))
    overlay.show();assert overlay.toolbar.isVisible()
    assert not normal
    QTest.keyClick(overlay,Qt.Key_Enter);assert normal[0].size().width()==120
    QTest.keyClick(overlay,Qt.Key_Escape);assert cancelled


def test_repeat_window_entry_preserves_confirmation_and_refuses_changed_dpi(qtbot,qapp,tmp_path,monkeypatch):
    from PySide6.QtGui import QGuiApplication,QPixmap
    screen=Screen(QRect(0,0,400,225))
    screen.grabWindow=lambda ident:QPixmap.fromImage(image())
    prefs=CapturePreferences(tmp_path/'prefs.json')
    prefs.update(region=binding(screen,image(),QRect(20,20,100,50)))
    win=window(qtbot,tmp_path,lambda text:None,None)
    win._preferences=prefs
    monkeypatch.setattr(QGuiApplication,'screens',lambda:[screen])
    real_show=ScreenshotOverlay.show
    def hide_show(overlay):
        overlay.setAttribute(Qt.WA_DontShowOnScreen,True);real_show(overlay)
    monkeypatch.setattr(ScreenshotOverlay,'show',hide_show)
    selected=[];monkeypatch.setattr(win,'_selected',selected.append)
    win.begin_capture(repeat=True)
    qtbot.waitUntil(lambda:bool(win._overlays))
    assert win._overlays[0]._selection==QRect(20,20,100,50) and not selected
    QTest.keyClick(win._overlays[0],Qt.Key_Enter);assert selected[0].width()==200
    win._close_overlays()
    screen.grabWindow=lambda ident:QPixmap.fromImage(image().scaled(400,225))
    win.begin_capture(repeat=True)
    qtbot.waitUntil(lambda:not win._capture_pending)
    assert not win._overlays and '配置已改变' in win.status.text()


def test_ocr_dialog_cancel_during_worker_suppresses_result(qtbot,tmp_path,monkeypatch):
    written=[];jobs=[]
    win=window(qtbot,tmp_path,written.append,lambda p:[{'text':'late','box':(0,0,50,20)}])
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win._text_selected(image());win._text.dialog.reject()
    result=jobs[0][0]();jobs[0][1](result)
    assert not written and not win._busy


def test_closing_text_dialog_does_not_cancel_image_compression(qtbot,tmp_path,monkeypatch):
    win=window(qtbot,tmp_path,lambda text:None,lambda p:[])
    monkeypatch.setattr(win,'_run',lambda *args:None)
    win._text.show();win.process_image(image())
    serial=win._serial
    win._text.dialog.reject()
    assert win._busy and not win._cancel.is_set() and win._serial==serial
    win.close()


def test_text_copy_can_retry_after_cancel(qtbot,tmp_path,monkeypatch):
    written=[];jobs=[]
    win=window(qtbot,tmp_path,written.append,lambda p:[])
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win._text.copy('first');win._text.dialog.reject()
    win._text.copy('retry')
    result=jobs[-1][0]();jobs[-1][1](result)
    assert written==['retry']


def test_stale_install_progress_cannot_override_new_task(qtbot,tmp_path):
    win=window(qtbot,tmp_path,lambda text:None,lambda p:[])
    win._serial=2;win._text._active_serial=2
    win._notice.label.setText('正在识别')
    win._text._progress(1,'旧下载进度')
    assert win._notice.label.text()=='正在识别'
    win._text._progress(2,'当前进度')
    assert win._notice.label.text()=='当前进度'


def test_closing_component_install_allows_new_capture_to_install_again(qtbot,tmp_path,monkeypatch):
    from pptx_finder import imgtext_ocr
    win=window(qtbot,tmp_path,lambda text:None,None);jobs=[]
    monkeypatch.setattr(imgtext_ocr,'is_installed',lambda:False)
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    monkeypatch.setattr(win,'_take_frames',lambda:None)
    win._text_selected(image());win._text.install()
    assert win._busy and not win._text.dialog.install_btn.isEnabled()
    win.close();assert not win._busy
    win.begin_capture();win._text_selected(image())
    assert win._text.dialog.install_btn.isEnabled()
    # Completion of the cancelled install cannot restart recognition or
    # disable the fresh install page.
    jobs[0][1]('')
    assert len(jobs)==1 and not win._busy and win._text.dialog.install_btn.isEnabled()


def test_copying_text_after_owner_reopens_updates_and_unlocks_dialog(qtbot,tmp_path,monkeypatch):
    written=[];jobs=[]
    win=window(qtbot,tmp_path,written.append,lambda p:[])
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win.close();win.show();win._text.show()
    win._text.dialog.editor.setPlainText('恢复复制')
    QTest.mouseClick(win._text.dialog.copy_btn,Qt.LeftButton)
    result=jobs[0][0]();jobs[0][1](result)
    assert written==['恢复复制'] and not win._busy
    assert win._text.dialog.copy_btn.isEnabled() and '已复制' in win._text.dialog.status.text()


def test_text_reading_order_and_temporary_cleanup(qapp):
    paths=[]
    def read(path):
        paths.append(path);assert path.is_file()
        return [{'text':'50 KB','box':(200,30,290,50)},{'text':'第一行','box':(10,10,90,28)},
                {'text':'限制','box':(10,32,90,52)},{'text':'','box':(0,0,1,1)}]
    assert recognize_image(image(),recognizer=read)=='第一行\n限制\t50 KB'
    assert not paths[0].exists()
    cancel=Event();cancel.set()
    with pytest.raises(CaptureCancelled):recognize_image(image(),cancelled=cancel,recognizer=read)


def window(qtbot,tmp_path,writer,recognizer):
    win=hidden(qtbot,ScreenshotWindow(output_root=tmp_path,text_writer=writer,recognizer=recognizer))
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._text.dialog.setAttribute(Qt.WA_DontShowOnScreen,True)
    return win


def test_text_auto_copy_and_edited_result(qtbot,tmp_path):
    written=[]
    win=window(qtbot,tmp_path,written.append,lambda p:[{'text':'中文 50KB','box':(0,0,100,30)}])
    win._text_selected(image())
    qtbot.waitUntil(lambda:bool(written) and not win._busy,timeout=5000)
    assert written==['中文 50KB'] and not win.isVisible()
    assert win._notice.text_btn.isVisible() and win.text_result_btn.isEnabled()
    win._text.show();win._text.dialog.editor.setPlainText('中文 49KB')
    QTest.mouseClick(win._text.dialog.copy_btn,Qt.LeftButton)
    qtbot.waitUntil(lambda:len(written)==2 and not win._busy)
    assert written[-1]=='中文 49KB'


def test_close_during_ocr_does_not_publish_late_text(qtbot,tmp_path,monkeypatch):
    written=[];jobs=[]
    win=window(qtbot,tmp_path,written.append,lambda p:[{'text':'late','box':(0,0,50,20)}])
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:jobs.append((fn,cb)))
    win._text_selected(image());win.close()
    result=jobs[0][0]();jobs[0][1](result)
    assert not written and not win._notice.isVisible()


def test_cancel_while_waiting_for_clipboard_preserves_existing_data(monkeypatch):
    import sys
    if sys.platform!='win32':pytest.skip('Windows clipboard context')
    from contextlib import contextmanager
    import win32clipboard as cb
    from pptx_finder.screenshots.delivery import copy_text
    cancel=Event();changes=[]
    @contextmanager
    def acquired(hwnd):
        cancel.set()
        yield
    monkeypatch.setattr('pptx_finder.native_clipboard.opened_clipboard',acquired)
    monkeypatch.setattr(cb,'EmptyClipboard',lambda:changes.append('empty'))
    monkeypatch.setattr(cb,'SetClipboardData',lambda *args:changes.append('write'))
    with pytest.raises(CaptureCancelled):copy_text('late',hwnd=1,cancelled=cancel)
    assert not changes


def test_clipboard_error_retains_editable_text(qtbot,tmp_path):
    def fail(text):raise RuntimeError('剪贴板被占用')
    win=window(qtbot,tmp_path,fail,lambda p:[{'text':'保留文字','box':(0,0,80,20)}])
    win._text_selected(image());qtbot.waitUntil(lambda:not win._busy)
    assert win._text.dialog.editor.toPlainText()=='保留文字'
    assert '剪贴板被占用' in win._text.dialog.status.text()


def test_missing_component_and_download_failure_are_visible(qtbot,tmp_path,monkeypatch):
    from pptx_finder import imgtext_ocr
    written=[];win=window(qtbot,tmp_path,written.append,None)
    monkeypatch.setattr(imgtext_ocr,'is_installed',lambda:False)
    win._text_selected(image())
    assert win._text.dialog.install_btn.isVisible() and not written
    def fail(**kwargs):raise RuntimeError('网络不可用')
    monkeypatch.setattr(imgtext_ocr,'install',fail)
    win._text.install();qtbot.waitUntil(lambda:not win._busy)
    assert '网络不可用' in win._text.dialog.status.text() and not written


@pytest.mark.parametrize('spec',['S','Ctrl+Ctrl+S','Ctrl+F12','Ctrl+Alt','Ctrl+S, Ctrl+R'])
def test_invalid_hotkeys_rejected(spec):
    with pytest.raises(ValueError):parse_key(spec)


def test_hotkey_conflict_rollback_and_disabled_setting(qtbot,qapp,tmp_path):
    owner=hidden(qtbot,QWidget());active={}
    def register(ident,mods,vk):
        if vk==ord('X'):return False
        active[ident]=(mods,vk);return True
    controller=ScreenshotShortcuts(qapp,owner,preferences=CapturePreferences(tmp_path/'keys.json'),register=register,unregister=lambda i:active.pop(i,None))
    original=dict(active)
    try:
        assert not controller.set_bindings({'capture':'Ctrl+Alt+X','repeat':'Ctrl+Alt+R'})[0]
        assert active==original
        assert controller.set_bindings({'capture':'Alt+F9','repeat':''})[0]
        assert list(active)==[IDS['capture']]
        assert controller.preferences.keys()['repeat']==''
    finally:controller.close()
    assert not active


def test_native_message_routes_capture_and_repeat(qtbot,qapp,tmp_path,monkeypatch):
    owner=hidden(qtbot,QWidget());calls=[]
    controller=ScreenshotShortcuts(qapp,owner,preferences=CapturePreferences(tmp_path/'keys.json'),register=lambda *args:True)
    monkeypatch.setattr(controller,'activate',calls.append)
    try:
        for key in IDS:
            msg=wintypes.MSG();msg.hWnd=controller._hwnd;msg.message=0x312;msg.wParam=IDS[key]
            assert controller._filter.nativeEventFilter(b'windows_generic_MSG',ctypes.addressof(msg))[0]
            qtbot.waitUntil(lambda:len(calls)==(1 if key=='capture' else 2))
        assert calls==['capture','repeat']
    finally:controller.close()


@pytest.mark.parametrize('mode',['text','image','files'])
def test_recreated_native_window_uses_current_clipboard_owner(qtbot,tmp_path,monkeypatch,mode):
    import sys
    if sys.platform!='win32':pytest.skip('Windows native window ownership')
    from contextlib import contextmanager
    win=hidden(qtbot,ScreenshotWindow(output_root=tmp_path))
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    old=int(win.winId());win.destroy();win.show()
    current=int(win.winId());assert current!=old
    calls=[];results=[]
    @contextmanager
    def acquired(hwnd):
        calls.append(hwnd)
        assert hwnd==current
        raise RuntimeError('ownership probe')
        yield
    monkeypatch.setattr('pptx_finder.native_clipboard.opened_clipboard',acquired)
    monkeypatch.setattr(win,'_run',lambda fn,cb,label:results.append(fn()))
    if mode=='text':win._text.copy('probe')
    elif mode=='image':win._copy_original(image())
    else:
        part=tmp_path/'part.jpg';part.write_bytes(b'valid size')
        win._paths=[part];win._closed=False;win._cancel=Event();win.copy_result()
    assert calls==[current] and results==['ownership probe']
