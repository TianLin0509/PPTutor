"""Bounded hidden helper jobs; only the process tree created here is stopped."""
import os,subprocess,time
from .encoding import CaptureCancelled


def run(command,*,cancelled=None,timeout=90,stderr=None,stdout=None):
    options={'stdin':subprocess.DEVNULL,'stdout':stdout or subprocess.DEVNULL,'stderr':stderr or subprocess.DEVNULL}
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:options['start_new_session']=True
    if cancelled is not None and cancelled.is_set():raise CaptureCancelled()
    child=subprocess.Popen(command,**options)
    deadline=time.monotonic()+timeout
    try:
        while child.poll() is None:
            if cancelled is not None and cancelled.is_set():raise CaptureCancelled()
            if time.monotonic()>deadline:raise TimeoutError('本机公式处理超时，请缩小选区重试')
            time.sleep(.05)
        if cancelled is not None and cancelled.is_set():raise CaptureCancelled()
        if child.returncode:raise RuntimeError('本机公式组件运行失败，请重试或重新安装组件')
    finally:
        if child.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(child.pid,signal.SIGKILL)
            child.wait(timeout=10)
