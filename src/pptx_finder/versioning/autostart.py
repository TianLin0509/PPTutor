"""开机自启：写/删「启动」文件夹里的快捷方式（不需管理员权限，比注册表稳）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "pptx-finder"


def _startup_lnk() -> Path:
    base = os.environ.get("APPDATA", str(Path.home()))
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.lnk"


def is_enabled() -> bool:
    return _startup_lnk().exists()


def _target() -> str:
    """打包后 sys.executable 即 exe；源码运行时退回 exe（若存在）或 python。"""
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return exe
    # 源码态：尽量指向已打包的 exe（开发期自启意义不大，仅兜底）
    guess = Path.cwd() / "dist" / "pptx-finder" / "pptx-finder.exe"
    return str(guess) if guess.exists() else exe


def set_enabled(on: bool) -> bool:
    lnk = _startup_lnk()
    if not on:
        try:
            lnk.unlink(missing_ok=True)
            return True
        except OSError:
            return False
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        import win32com.client

        sh = win32com.client.Dispatch("WScript.Shell")
        sc = sh.CreateShortcut(str(lnk))
        sc.TargetPath = _target()
        sc.Description = "pptx-finder · 后台守护 PPT 版本"
        sc.Save()
        return True
    except Exception:  # noqa: BLE001
        return False
