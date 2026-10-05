import random
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor,QImage,QPainter
from pptx_finder.screenshots.stitching import Stitcher,StitchMismatch,overlap
from pptx_finder.ui.scroll_capture import ScrollCapture


def document(width=160,height=1400):
    data=random.Random(51).randbytes(width*height*3)
    return QImage(data,width,height,width*3,QImage.Format_RGB888).convertToFormat(QImage.Format_RGB32)


def test_native_resolution_stitch_covers_document_pixel_exactly(qapp):
    source=document()
    stitch=Stitcher(source.copy(0,0,160,400))
    for position in [137,310,470,660,800,1000]:
        assert stitch.add(source.copy(0,position,160,400))>0
    assert stitch.result()==source
    assert stitch.add(source.copy(0,1000,160,400))==0


def test_fixed_header_footer_appear_only_once(qapp):
    source=document(height=1100)
    def frame(position):
        result=QImage(160,400,QImage.Format_RGB32);result.fill(QColor('#eeeeee'))
        p=QPainter(result)
        p.fillRect(0,0,160,30,QColor('#234568'))
        p.drawImage(0,30,source.copy(0,position,160,340))
        p.fillRect(0,370,160,30,QColor('#777777'));p.end()
        return result
    stitch=Stitcher(frame(0))
    for position in [150,300,450,600,760]:stitch.add(frame(position))
    expected=QImage(160,1160,QImage.Format_RGB32)
    p=QPainter(expected);p.fillRect(0,0,160,30,QColor('#234568'));p.drawImage(0,30,source);p.fillRect(0,1130,160,30,QColor('#777777'));p.end()
    assert stitch.result()==expected


def test_unrelated_changed_frame_is_rejected(qapp):
    a=document(height=400);b=QImage(a.size(),a.format());b.fill(Qt.white)
    with pytest.raises(StitchMismatch):overlap(a,b)


def test_ambiguous_repeating_lines_are_not_silently_spliced(qapp):
    image=QImage(160,400,QImage.Format_RGB32);image.fill(Qt.white)
    p=QPainter(image)
    for y in range(0,400,24):p.fillRect(0,y,160,12,QColor('#234568'))
    p.end()
    changed=image.copy();p=QPainter(changed);p.fillRect(0,380,160,20,Qt.white);p.end()
    with pytest.raises(StitchMismatch):overlap(image,changed)


def test_timer_capture_stops_at_bottom_and_keeps_every_pixel(qtbot):
    source=document(height=1000);position=0
    def capture():return source.copy(0,position,160,400)
    def wheel():
        nonlocal position
        position=min(600,position+120)
    engine=ScrollCapture(capture,wheel,interval=1)
    completed=[];engine.finished.connect(lambda image,msg,ok:completed.append((image,msg,ok)))
    engine.start()
    qtbot.waitUntil(lambda:bool(completed),timeout=10000)
    assert completed[0][0]==source and completed[0][2]


def test_manual_stop_is_marked_partial_and_never_claims_bottom(qtbot):
    source=document(height=1000);position=0
    def wheel():
        nonlocal position
        position+=120
    engine=ScrollCapture(lambda:source.copy(0,position,160,400),wheel,interval=30)
    completed=[];engine.finished.connect(lambda image,msg,ok:completed.append((image,msg,ok)))
    engine.start();engine.stop()
    assert completed and not completed[0][2] and '用户停止' in completed[0][1]


def test_discard_stop_never_publishes_late_result(qtbot):
    source=document(height=1000)
    engine=ScrollCapture(lambda:source.copy(0,0,160,400),lambda:None,interval=30)
    completed=[];engine.finished.connect(lambda *args:completed.append(args))
    engine.start();engine.stop(discard=True);qtbot.wait(70)
    assert completed==[]


def test_non_scrollable_target_does_not_claim_completed_long_image(qtbot):
    source=document(height=400)
    engine=ScrollCapture(lambda:source,lambda:None,interval=1)
    completed=[];engine.finished.connect(lambda *args:completed.append(args))
    engine.start()
    qtbot.waitUntil(lambda:bool(completed),timeout=10000)
    assert not completed[0][2] and '未发生滚动' in completed[0][1]


def test_white_body_gaps_are_not_treated_as_fixed_footer(qapp):
    source=document(height=900)
    painter=QPainter(source)
    painter.fillRect(0,365,160,70,Qt.white)
    painter.fillRect(0,505,160,70,Qt.white)
    painter.end()
    stitch=Stitcher(source.copy(0,0,160,400))
    for position in [140,280,420,500]:
        stitch.add(source.copy(0,position,160,400))
    assert stitch.result()==source
