"""Two independent Windows hotkeys with visible conflicts and rollback."""
import ctypes,logging,sys
from ctypes import wintypes
from PySide6.QtCore import QObject,QAbstractNativeEventFilter,QTimer,Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication,QDialog,QFormLayout,QLabel,QKeySequenceEdit,QPushButton,QWidget
from ..config import data_dir
from ..screenshots.preferences import CapturePreferences

log=logging.getLogger(__name__)
IDS={'capture':0x5053,'repeat':0x5054}


def parse_key(spec):
    if not isinstance(spec,str):raise ValueError('快捷键格式无效')
    if not spec.strip():return None
    parts=spec.upper().replace('META','WIN').split('+')
    flags={'ALT':1,'CTRL':2,'SHIFT':4,'WIN':8}
    if len(parts)<2 or len(set(parts))!=len(parts):raise ValueError('使用 Ctrl / Alt / Shift 与一个按键组合')
    mods=0
    for part in parts[:-1]:
        if part not in flags:raise ValueError('快捷键修饰键无效')
        mods|=flags[part]
    key=parts[-1]
    if len(key)==1 and key.isascii() and key.isalnum():vk=ord(key)
    elif key.startswith('F') and key[1:].isdigit() and 1<=int(key[1:])<=11:vk=111+int(key[1:])
    elif key in ('PRINT','PRINTSCREEN'):vk=0x2c
    else:raise ValueError('支持字母、数字、F1–F11 或 PrintScreen；F12 为系统保留键')
    return mods,vk


class _Filter(QAbstractNativeEventFilter):
    def __init__(self,controller):super().__init__();self.controller=controller
    def nativeEventFilter(self,etype,message):
        if etype not in ('windows_generic_MSG','windows_dispatcher_MSG',b'windows_generic_MSG',b'windows_dispatcher_MSG'):
            return False,0
        if not int(message):return False,0
        msg=wintypes.MSG.from_address(int(message))
        controller=self.controller
        if msg.message==0x312 and msg.hWnd==controller._hwnd and not controller.closed:
            for key,ident in IDS.items():
                if msg.wParam==ident and key in controller.registered:
                    QTimer.singleShot(0,lambda key=key:controller.activate(key))
                    return True,0
        return False,0


class ScreenshotShortcuts(QObject):
    def __init__(self,app,owner,*,preferences=None,register=None,unregister=None):
        super().__init__(owner)
        self.app=app;self.owner=owner;self.closed=False
        self.preferences=preferences or CapturePreferences(data_dir()/'screenshot-preferences.json')
        self.specs=self.preferences.keys();self.registered=set();self.errors={}
        self._window=QWidget(owner,Qt.Tool)
        hwnd=int(self._window.winId())
        self._hwnd=hwnd
        if register is None:
            if sys.platform=='win32':
                native=ctypes.WinDLL('user32',use_last_error=True)
                native.RegisterHotKey.argtypes=[wintypes.HWND,ctypes.c_int,wintypes.UINT,wintypes.UINT]
                native.RegisterHotKey.restype=wintypes.BOOL
                native.UnregisterHotKey.argtypes=[wintypes.HWND,ctypes.c_int]
                native.UnregisterHotKey.restype=wintypes.BOOL
                register=lambda ident,mods,vk:bool(native.RegisterHotKey(hwnd,ident,mods|0x4000,vk))
                unregister=lambda ident:native.UnregisterHotKey(hwnd,ident)
            else:
                register=lambda *a:False;unregister=lambda *a:None
        self._register=register;self._unregister=unregister or (lambda *a:None)
        self._filter=_Filter(self)
        app.installNativeEventFilter(self._filter)
        self._bind(self.specs)
        app.aboutToQuit.connect(self.close)
        self.dialog=None

    def _bind(self,specs):
        self.errors={}
        seen=set()
        for key,spec in specs.items():
            try:
                parsed=parse_key(spec)
                if parsed is None:continue
                if parsed in seen:raise ValueError('两个动作不能使用相同快捷键')
                seen.add(parsed)
                if self._register(IDS[key],*parsed):self.registered.add(key)
                else:self.errors[key]='快捷键被占用或系统未允许注册'
            except Exception as exc:self.errors[key]=str(exc)

    def _unbind(self):
        for key in list(self.registered):self._unregister(IDS[key])
        self.registered.clear()

    def set_bindings(self,specs):
        if self.closed:return False,'快捷键服务已关闭'
        try:
            parsed=[parse_key(specs[key]) for key in IDS]
            if parsed[0] is not None and parsed[0]==parsed[1]:raise ValueError('两个动作不能使用相同快捷键')
        except (KeyError,ValueError) as exc:return False,str(exc)
        previous=dict(self.specs)
        self._unbind();self._bind(specs)
        error='；'.join(self.errors.values())
        if not error:
            try:self.preferences.update(keys=specs)
            except Exception as exc:error=f'快捷键未保存：{exc}'
        if error:
            self._unbind();self._bind(previous)
            if self.errors:error+='；旧快捷键恢复失败，请检查设置'
            return False,error
        self.specs=dict(specs)
        return True,'快捷键已保存；留空的动作已停用'

    def activate(self,key):
        if self.closed:return
        from .screenshot_window import ScreenshotWindow,open_screenshot
        if isinstance(self.owner,ScreenshotWindow):
            self.owner.begin_capture(repeat=key=='repeat')
        else:open_screenshot(self.owner,repeat=key=='repeat')

    def description(self):
        return ' · '.join(f'{"框选" if key=="capture" else "重复区域"}：{self.specs[key] or "停用"}'
                          +(f'（{self.errors[key]}）' if key in self.errors else '') for key in IDS)

    def show_settings(self):
        if self.dialog is None:
            self.dialog=QDialog(self.owner);self.dialog.setWindowTitle('截图快捷键')
            self.dialog.setStyleSheet('QDialog { background: #fff; } QLabel { color: #243249; } QKeySequenceEdit { background: #f7f9fc; color: #243249; border: 1px solid #dfe5ed; padding: 6px; } QPushButton { background: #edf3fc; color: #2867bd; border: 1px solid #dfe5ed; padding: 8px; }')
            form=QFormLayout(self.dialog)
            self.editors={key:QKeySequenceEdit(QKeySequence(self.specs[key])) for key in IDS}
            for editor in self.editors.values():editor.setMaximumSequenceLength(1)
            form.addRow('框选截图',self.editors['capture']);form.addRow('重复上次区域',self.editors['repeat'])
            self.label=QLabel(self.description());self.label.setWordWrap(True);form.addRow(self.label)
            button=QPushButton('保存快捷键');form.addRow(button)
            def save():
                values={key:editor.keySequence().toString(QKeySequence.PortableText) for key,editor in self.editors.items()}
                ok,message=self.set_bindings(values)
                self.label.setText(message+'\n'+self.description())
            button.clicked.connect(save)
        self.label.setText(self.description())
        self.dialog.show();self.dialog.raise_()

    def close(self):
        if self.closed:return
        self.closed=True;self._unbind()
        self.app.removeNativeEventFilter(self._filter)
        if self.dialog:self.dialog.close()
        self._window.close()


def install_screenshot_shortcuts(app,owner):
    controller=ScreenshotShortcuts(app,owner)
    owner._screenshot_shortcuts=controller
    app._screenshot_shortcuts=controller
    return controller
