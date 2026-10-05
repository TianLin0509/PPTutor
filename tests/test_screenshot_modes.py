from PySide6.QtCore import QPoint,QRect,Qt
import pytest
from PySide6.QtGui import QColor,QImage
from PySide6.QtTest import QTest
from pptx_finder.ui.screenshot_overlay import ScreenshotOverlay
from pptx_finder.ui.screenshot_window import ScreenshotWindow
from pptx_finder.screenshots.encoding import compress,encode
from pptx_finder.screenshots.fidelity import visually_close
from pptx_finder.screenshots.splitting import split_region
from test_screenshots import text_image,detailed_image


def test_toolbar_has_independent_normal_scroll_and_small_modes(qtbot):
    win = ScreenshotOverlay(text_image(),QRect(0,0,800,450))
    win.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(win);win.show()
    normal,small,scroll = [],[],[]
    win.selected.connect(normal.append);win.small_selected.connect(small.append)
    win.scroll_selected.connect(lambda r,g:scroll.append((r,g)))
    QTest.mousePress(win,Qt.LeftButton,pos=QPoint(40,50))
    QTest.mouseRelease(win,Qt.LeftButton,pos=QPoint(740,390))
    assert normal==small==scroll==[]
    assert win.confirm_btn.isVisible() and win.scroll_btn.isVisible() and win.small_btn.isVisible()
    QTest.mouseClick(win.small_btn,Qt.LeftButton)
    assert len(small)==1 and normal==scroll==[]
    QTest.mouseClick(win.scroll_btn,Qt.LeftButton)
    assert scroll[0][0]==QRect(40,50,700,340)
    assert scroll[0][1]==QRect(0,0,800,450)
    QTest.keyClick(win,Qt.Key_Return)
    assert len(normal)==1


def test_normal_capture_never_uses_small_image_encoder(qtbot,tmp_path):
    originals,files = [],[]
    win = ScreenshotWindow(image_writer=lambda i:originals.append(i),clipboard_writer=files.append,output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    image = detailed_image(320,180)
    win._selected(image)
    qtbot.waitUntil(lambda:len(originals)==1 and not win._busy,timeout=5000)
    assert originals[0]==image and files==[] and not win.isVisible()
    assert '原图已复制' in win.status.text()


def test_small_mode_auto_copies_all_without_opening_a_result_dialog(qtbot,tmp_path):
    originals,files = [],[]
    win=ScreenshotWindow(image_writer=originals.append,clipboard_writer=files.append,output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    win._small_selected(detailed_image(320,180))
    qtbot.waitUntil(lambda:bool(files) and not win._busy,timeout=15000)
    assert not win.isVisible() and originals==[]
    assert len(files[0])>1 and all(p.stat().st_size<=50000 for p in files[0])


def test_visual_guard_rejects_erased_text_and_accepts_identical_image(qapp):
    source=text_image();blank=QImage(source.size(),source.format());blank.fill(QColor('white'))
    assert visually_close(source,source)
    assert not visually_close(source,blank)


def test_left_right_gutter_keeps_content_columns_together(qapp):
    image=detailed_image(320,180)
    from PySide6.QtGui import QPainter
    p=QPainter(image);p.fillRect(QRect(140,0,40,180),QColor('white'));p.end()
    a,b=split_region(image,image.rect())
    assert a.height()==b.height()==180
    assert 140<=b.x()<=180 and 140<=a.right()<=180


def test_upper_lower_gutter_keeps_paragraph_blocks_together(qapp):
    image=detailed_image(200,320)
    from PySide6.QtGui import QPainter
    p=QPainter(image);p.fillRect(QRect(0,140,200,40),QColor('white'));p.end()
    a,b=split_region(image,image.rect())
    assert a.width()==b.width()==200
    assert 140<=b.y()<=180 and 140<=a.bottom()<=180


def test_choose_smaller_safe_encoding_even_when_original_png_already_fits(qapp):
    image=QImage(360,180,QImage.Format_RGB888)
    for y in range(180):
        for x in range(360):
            image.setPixelColor(x,y,QColor(x%256,(x+y)%256,y%256))
    part=compress(image)[0]
    assert part.size<=len(encode(image,'PNG'))
    if part.format=='JPEG':
        assert visually_close(image,QImage.fromData(part.data))


def test_closing_before_original_copy_runs_does_not_write_clipboard(qtbot,tmp_path,monkeypatch):
    originals=[]
    win=ScreenshotWindow(image_writer=originals.append,output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    jobs=[]
    monkeypatch.setattr(win,'_run',lambda fn,callback,label:jobs.append(fn))
    win._selected(text_image())
    win.close()
    jobs[0]()
    assert originals==[]


def test_original_copy_failure_allows_retry_and_save_without_small_conversion(qtbot,tmp_path,monkeypatch):
    attempts=[]
    def writer(image):
        attempts.append(image.copy())
        if len(attempts)==1:raise RuntimeError('clipboard busy')
    win=ScreenshotWindow(image_writer=writer,output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    source=text_image()
    win._copy_original(source,scroll_message='用户停止',complete=False)
    qtbot.waitUntil(lambda:not win._busy,timeout=5000)
    assert win.copy_btn.isEnabled() and win.save_btn.isEnabled()
    assert win.copy_btn.text()=='复制原图'
    from PySide6.QtWidgets import QFileDialog
    destination=tmp_path/'original.png'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a,**kw:(str(destination),'PNG'))
    win.save_result()
    qtbot.waitUntil(lambda:not win._busy,timeout=5000)
    assert QImage(str(destination))==source
    win.copy_result()
    qtbot.waitUntil(lambda:not win._busy,timeout=5000)
    assert len(attempts)==2 and attempts[1]==source
    assert '部分长图' in win.status.text()


def test_new_capture_hides_previous_notice_before_freezing(qtbot,tmp_path,monkeypatch):
    win=ScreenshotWindow(output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    win._notice.complete('已复制')
    assert win._notice.isVisible()
    monkeypatch.setattr(win,'_take_frames',lambda:None)
    win.begin_capture()
    assert not win._notice.isVisible()
    win.close()


@pytest.mark.parametrize('close_on_events',[False,True])
def test_scroll_notice_cannot_obscure_its_own_native_wheel_target(qtbot,tmp_path,monkeypatch,close_on_events):
    from PySide6.QtCore import QObject,Signal
    from PySide6.QtGui import QGuiApplication
    import pptx_finder.screenshots.scroll_native as native
    import pptx_finder.ui.scroll_capture as controller
    win=ScreenshotWindow(output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win._notice.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    calls=[]
    class Target:
        def __init__(self,*a):pass
        def wheel(self):
            assert not win._notice.isVisible(),'own feedback obscures target'
            calls.append('wheel')
    class Engine(QObject):
        progress=Signal(str)
        finished=Signal(object,str,bool)
        def __init__(self,capture,wheel,parent=None):
            super().__init__(parent);self.wheel=wheel
        def start(self):
            self.progress.emit('滚动中')
            self.wheel()
        def stop(self,**kwargs):pass
    monkeypatch.setattr(native,'NativeScrollTarget',Target)
    monkeypatch.setattr(controller,'ScrollCapture',Engine)
    if close_on_events:
        monkeypatch.setattr(QGuiApplication,'processEvents',lambda *a,**kw:win.close())
    screen=QGuiApplication.primaryScreen()
    geometry=screen.geometry()
    win._start_scroll(QRect(0,0,geometry.width(),geometry.height()),geometry)
    assert calls==([] if close_on_events else ['wheel'])
    assert win._notice.isVisible()!=close_on_events
    if close_on_events:
        assert not win.isVisible()
    win.close()
