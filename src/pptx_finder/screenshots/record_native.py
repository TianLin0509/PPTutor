"""Copy only the selected physical screen pixels, without a full-screen buffer."""
from contextlib import ExitStack
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QWindow


class NativeRegionCapture:
    def __init__(self, screen, local, frame_size):
        import win32api
        probe = QWindow()
        probe.setScreen(screen)
        probe.setGeometry(screen.geometry())
        probe.create()
        try:
            self.monitor = win32api.MonitorFromWindow(int(probe.winId()), 2)
            self.bounds = win32api.GetMonitorInfo(self.monitor)['Monitor']
        finally:
            probe.destroy()
        geometry = screen.geometry()
        area = local.normalized().intersected(QRect(0, 0, geometry.width(), geometry.height()))
        sx, sy = frame_size.width()/geometry.width(), frame_size.height()/geometry.height()
        left, top = round(area.x()*sx), round(area.y()*sy)
        right, bottom = round((area.x()+area.width())*sx), round((area.y()+area.height())*sy)
        self.point = (self.bounds[0]+left, self.bounds[1]+top)
        self.size = (right-left, bottom-top)
        if min(self.size) < 4 or self.size[0]*self.size[1] > 24_000_000:
            raise ValueError('录制区域过大或过小，请重新框选')

    def grab(self):
        import win32api
        import win32con
        import win32gui
        import win32ui
        if win32api.GetMonitorInfo(self.monitor)['Monitor'] != self.bounds:
            raise ValueError('显示器配置已改变，请重新框选')
        handle = win32gui.GetDC(0)
        if not handle:
            raise OSError('无法读取屏幕画面')
        with ExitStack() as resources:
            resources.callback(win32gui.ReleaseDC, 0, handle)
            source = win32ui.CreateDCFromHandle(handle)
            resources.callback(source.DeleteDC)
            memory = source.CreateCompatibleDC()
            resources.callback(memory.DeleteDC)
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, *self.size)
            resources.callback(win32gui.DeleteObject, bitmap.GetHandle())
            previous = memory.SelectObject(bitmap)
            if previous is not None:
                resources.callback(memory.SelectObject, previous)
            memory.BitBlt((0, 0), self.size, source, self.point, win32con.SRCCOPY | 0x40000000)
            info = bitmap.GetInfo()
            pixels = bitmap.GetBitmapBits(True)
            if info['bmBitsPixel'] != 32:
                raise ValueError('当前显示器颜色格式不支持录制')
            return QImage(pixels, *self.size, info['bmWidthBytes'], QImage.Format_RGB32).copy()
