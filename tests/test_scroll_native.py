import sys
import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QWidget
from pptx_finder.screenshots.scroll_native import NativeScrollTarget

pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='Windows native capture')


def test_friendly_qt_screen_name_does_not_require_windows_device_name(qtbot,monkeypatch):
    import win32api
    import win32gui
    from PySide6.QtGui import QGuiApplication
    widget=QWidget()
    widget.setAttribute(Qt.WA_DontShowOnScreen,True)
    qtbot.addWidget(widget)
    hwnd=int(widget.winId())
    monkeypatch.setattr(win32gui,'WindowFromPoint',lambda point:hwnd)
    screen=QGuiApplication.primaryScreen()
    geometry=screen.geometry()
    size=screen.grabWindow(0).size()
    target=NativeScrollTarget(screen,QRect(10,10,100,100),size)
    monitor=win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd,2))['Monitor']
    assert target.point==(monitor[0]+round(59*size.width()/geometry.width()),
                          monitor[1]+round(59*size.height()/geometry.height()))
    assert target.root==win32gui.GetAncestor(hwnd,2)


def test_negative_screen_coordinates_and_wheel_delta_are_encoded_signed(monkeypatch):
    import win32gui
    import win32con
    target=NativeScrollTarget.__new__(NativeScrollTarget)
    target.hwnd=10;target.root=10;target.bounds=(-2000,-100,0,1000);target.point=(-1300,-10)
    monkeypatch.setattr(win32gui,'IsWindow',lambda h:True)
    monkeypatch.setattr(win32gui,'IsIconic',lambda h:False)
    monkeypatch.setattr(win32gui,'GetWindowRect',lambda h:target.bounds)
    monkeypatch.setattr(win32gui,'WindowFromPoint',lambda p:10)
    monkeypatch.setattr(win32gui,'GetAncestor',lambda h,f:10)
    messages=[]
    monkeypatch.setattr(win32gui,'PostMessage',lambda *args:messages.append(args))
    target.wheel()
    hwnd,message,delta,position=messages[0]
    def signed(v):return v-65536 if v>=32768 else v
    assert message==win32con.WM_MOUSEWHEEL
    assert signed(delta>>16)==-120
    assert (signed(position&65535),signed(position>>16))==(-1300,-10)


def test_obscured_target_does_not_receive_wheel(monkeypatch):
    import win32gui
    target=NativeScrollTarget.__new__(NativeScrollTarget)
    target.hwnd=10;target.root=10;target.bounds=(0,0,100,100);target.point=(50,50)
    monkeypatch.setattr(win32gui,'IsWindow',lambda h:True)
    monkeypatch.setattr(win32gui,'IsIconic',lambda h:False)
    monkeypatch.setattr(win32gui,'GetWindowRect',lambda h:target.bounds)
    monkeypatch.setattr(win32gui,'WindowFromPoint',lambda p:20)
    monkeypatch.setattr(win32gui,'GetAncestor',lambda h,f:h)
    with pytest.raises(ValueError,match='被遮挡'):
        target.wheel()
