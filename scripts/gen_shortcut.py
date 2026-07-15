"""生成应用图标 .ico + 在桌面创建快捷方式（指向打包好的 exe）。"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DIST = Path(r"C:\Users\lintian\pptx-finder\dist\PPT Doctor")
EXE = DIST / "PPT Doctor.exe"


def make_shortcut() -> Path:
    import win32com.client

    desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
    lnk = desktop / "PPT Doctor.lnk"
    ws = win32com.client.Dispatch("WScript.Shell")
    sc = ws.CreateShortcut(str(lnk))
    sc.TargetPath = str(EXE)
    sc.WorkingDirectory = str(DIST)
    sc.IconLocation = f"{EXE},0"
    sc.Description = "PPT Doctor · PPT 查询助手"
    sc.Save()
    return lnk


def main() -> None:
    if not EXE.exists():
        print("EXE_MISSING", EXE)
        return
    lnk = make_shortcut()
    print("ICO_FROM_EXE")
    print("LNK:", lnk, "| exists:", lnk.exists())


if __name__ == "__main__":
    main()
