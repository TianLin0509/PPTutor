from pathlib import Path
from threading import Event
import struct
import random

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from pptx_finder.screenshots.encoding import CaptureCancelled, compress
from pptx_finder.screenshots.delivery import export_files, hdrop_payload, store_parts
from pptx_finder.ui.screenshot_overlay import ScreenshotOverlay, crop_physical
from pptx_finder.ui.screenshot_window import ScreenshotWindow


def text_image(width=800, height=450):
    image=QImage(width,height,QImage.Format_RGB32)
    image.fill(QColor('white'))
    p=QPainter(image)
    p.setPen(QColor('#243b62'))
    p.drawText(QRect(35,35,width-70,height-70),Qt.AlignLeft,'PPT Doctor · Chinese text / 算力基础设施方案\n5000P → 6000P')
    p.end()
    return image


def detailed_image(width=640,height=360):
    # Deterministic high-entropy raster, genuinely exercising multi-part output.
    data=random.Random(42).randbytes(width*height*3)
    return QImage(data,width,height,width*3,QImage.Format_RGB888).copy()


def test_text_stays_lossless_and_full_resolution(qapp):
    image=text_image()
    parts=compress(image)
    assert len(parts)==1 and parts[0].format=='PNG'
    assert QImage.fromData(parts[0].data).convertToFormat(QImage.Format_RGB888)==image.convertToFormat(QImage.Format_RGB888)


def test_complex_image_splits_into_bounded_full_resolution_parts(qapp):
    image=detailed_image(320,180)
    parts=compress(image)
    assert len(parts)==1
    coverage=set()
    for part in parts:
        assert 0<len(part.data)<=50000
        decoded=QImage.fromData(part.data)
        x,y,w,h=part.region
        assert (decoded.width(),decoded.height())==(w,h)
        assert part.quality is None or part.quality>=75
        for row in range(y,y+h):
            coverage.update((row,col) for col in range(x,x+w))
    assert len(coverage)==image.width()*image.height()


def test_cancel_does_not_return_upload_ready_parts(qapp):
    cancel=Event();cancel.set()
    with pytest.raises(CaptureCancelled):compress(text_image(),cancelled=cancel)


def test_complex_image_cannot_silently_downscale_or_exceed_limit(qapp):
    parts=compress(detailed_image(),max_bytes=2000,max_parts=1)
    assert len(parts)==1 and parts[0].size>2000
    assert QImage.fromData(parts[0].data).size()==detailed_image().size()


@pytest.mark.parametrize('image',[QImage(),QImage(5001,5001,QImage.Format_RGB32)])
def test_invalid_input_is_rejected(image,qapp):
    with pytest.raises(ValueError):compress(image)


def test_transparency_is_composited_on_white(qapp):
    image=QImage(12,12,QImage.Format_ARGB32);image.fill(Qt.transparent)
    part=compress(image)[0]
    assert QImage.fromData(part.data).pixelColor(0,0)==QColor('white')


def test_delivery_stores_byte_exact_numbered_files_and_metadata(tmp_path,qapp):
    parts=compress(text_image())
    paths=store_parts(parts,tmp_path)
    assert paths[0].read_bytes()==parts[0].data
    assert (paths[0].parent/'manifest.json').is_file()
    assert paths[0].name.endswith('-01.png')


def test_file_clipboard_can_carry_multiple_unicode_paths(tmp_path):
    paths=[tmp_path/'第一张.jpg',tmp_path/'第二张.png']
    payload=hdrop_payload(paths)
    assert struct.unpack('<IiiII',payload[:20])==(20,0,0,0,1)
    assert payload[20:].decode('utf-16-le').split('\0')==[str(p.absolute()) for p in paths]+['','']


def test_export_never_overwrites_changed_user_file(tmp_path,qapp):
    paths=store_parts(compress(text_image()),tmp_path/'cache')
    dest=tmp_path/'output';dest.mkdir()
    changed=dest/paths[0].name;changed.write_bytes(b'personal content')
    with pytest.raises(ValueError,match='未覆盖'):export_files(paths,dest)
    assert changed.read_bytes()==b'personal content'


@pytest.mark.parametrize('scale',[1,1.25,1.5,2])
def test_capture_uses_physical_pixels_and_preserves_reverse_drag(scale):
    bounds=QRect(-1920,0,800,450)
    image=text_image(round(800*scale),round(450*scale))
    result=crop_physical(image,QRect(50,40,300,200),bounds)
    assert (result.width(),result.height())==(round(350*scale)-round(50*scale),round(240*scale)-round(40*scale))
    assert result.devicePixelRatio()==1.0


def test_overlay_drag_emits_crop_and_escape_cancels(qtbot):
    overlay=ScreenshotOverlay(text_image(),QRect(0,0,800,450))
    overlay.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(overlay);overlay.show()
    received=[];cancel=[]
    overlay.selected.connect(received.append);overlay.cancelled.connect(lambda:cancel.append(True))
    from PySide6.QtCore import QPoint
    QTest.mousePress(overlay,Qt.LeftButton,pos=QPoint(320,240))
    QTest.mouseRelease(overlay,Qt.LeftButton,pos=QPoint(40,30))
    assert received==[] and overlay.toolbar.isVisible()
    QTest.mouseClick(overlay.confirm_btn,Qt.LeftButton)
    assert len(received)==1 and received[0].width()>=280
    QTest.keyClick(overlay,Qt.Key_Escape)
    assert cancel==[True]


def test_result_window_auto_copies_all_and_individual_part(qtbot,tmp_path):
    copied=[]
    win=ScreenshotWindow(clipboard_writer=lambda paths:copied.append(paths),output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(win)
    assert win.process_image(detailed_image(320,180))
    qtbot.waitUntil(lambda:len(copied)==1 and not win._busy,timeout=15000)
    assert win.grab().toImage().pixelColor(0,0)==QColor('white')
    assert len(copied[0])==1
    assert all(p.stat().st_size<=50000 for p in copied[0])
    win.copy_result(0)
    qtbot.waitUntil(lambda:len(copied)==2 and not win._busy,timeout=5000)
    assert len(copied[1])==1


def test_copy_failure_keeps_results_available_for_save_or_retry(qtbot,tmp_path):
    def fail(paths):raise RuntimeError('clipboard busy')
    win=ScreenshotWindow(clipboard_writer=fail,output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(win)
    win.process_image(text_image())
    qtbot.waitUntil(lambda:bool(win._paths) and not win._busy,timeout=5000)
    assert '未成功' in win.status.text()
    assert win.save_btn.isEnabled() and win.copy_btn.isEnabled()


def test_closing_pending_capture_preserves_clipboard_and_restores_owner(qtbot,tmp_path):
    from PySide6.QtWidgets import QWidget
    owner=QWidget();owner.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(owner);owner.show()
    copied=[]
    win=ScreenshotWindow(owner,clipboard_writer=lambda p:copied.append(p),output_root=tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen,True)
    win.begin_capture();assert not owner.isVisible()
    win.close();qtbot.wait(180)
    assert owner.isVisible() and not win._overlays and copied==[]


def test_main_window_screenshot_entry_dispatches_without_losing_existing_tools(qtbot,monkeypatch):
    from pptx_finder import db
    from pptx_finder.ui.main_window import MainWindow
    from test_ui import StubRender
    import pptx_finder.ui.screenshot_window as mod
    conn=db.connect(':memory:');db.init_db(conn)
    win=MainWindow(conn=conn,render_worker=StubRender(),do_index=False)
    win.setAttribute(Qt.WA_DontShowOnScreen,True);qtbot.addWidget(win)
    called=[];monkeypatch.setattr(mod,'open_screenshot',lambda parent:called.append(parent))
    win.rail_screenshot_btn.click()
    assert called==[win]
    assert win.rail_imgtext_btn and win.rail_material_btn and win.rail_report_btn and win.rail_settings_btn
    win._shutdown()
