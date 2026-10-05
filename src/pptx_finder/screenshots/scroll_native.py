"""Send wheel messages to the selected window, without moving the mouse."""
import sys


class NativeScrollTarget:
    def __init__(self,screen,local_rect,frame_size):
        if sys.platform!='win32':
            raise ValueError('自动滚动截图当前支持 Windows')
        import win32api
        import win32gui
        from PySide6.QtGui import QWindow
        geo = screen.geometry()
        # Qt may expose a friendly model name rather than \\.\DISPLAYn.
        # A never-shown native window lets Windows identify the actual monitor.
        probe = QWindow()
        probe.setScreen(screen)
        probe.setGeometry(geo)
        probe.create()
        try:
            monitor = win32api.MonitorFromWindow(int(probe.winId()),2)
            info = win32api.GetMonitorInfo(monitor)
        finally:
            probe.destroy()
        left,top,_,_ = info['Monitor']
        self.point = (left+round(local_rect.center().x()*frame_size.width()/geo.width()),
                      top+round(local_rect.center().y()*frame_size.height()/geo.height()))
        self.hwnd = win32gui.WindowFromPoint(self.point)
        self.root = win32gui.GetAncestor(self.hwnd,2)
        if not self.hwnd or not self.root:
            raise ValueError('未找到可滚动的目标窗口')
        self.bounds = win32gui.GetWindowRect(self.root)

    def wheel(self):
        import win32gui
        import win32con
        if (not win32gui.IsWindow(self.hwnd) or win32gui.IsIconic(self.root)
                or win32gui.GetWindowRect(self.root)!=self.bounds
                or win32gui.GetAncestor(win32gui.WindowFromPoint(self.point),2)!=self.root):
            raise ValueError('目标窗口移动、关闭或被遮挡，滚动截图已停止')
        x,y = self.point
        position = (x & 0xffff)|((y & 0xffff)<<16)
        delta = (-120 & 0xffff)<<16
        win32gui.PostMessage(self.hwnd,win32con.WM_MOUSEWHEEL,delta,position)
