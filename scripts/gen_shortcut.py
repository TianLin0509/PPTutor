"""生成应用图标 .ico + 在桌面创建快捷方式（指向打包好的 exe）。"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DIST = Path(r"C:\Users\lintian\pptx-finder\dist\PPT Doctor")
EXE = DIST / "PPT Doctor.exe"
ICO = DIST / "app.ico"


def make_ico() -> bool:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter

    QGuiApplication([])
    src = Path(r"C:\Users\lintian\pptx-finder\assets\logo.png")
    if not src.exists():
        return False
    logo = QImage(str(src))
    if logo.isNull():
        return False
    # 贴到 256x256 透明方形画布居中（ICO 需方形）
    canvas = QImage(256, 256, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    scaled = logo.scaled(228, 228, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p = QPainter(canvas)
    p.drawImage((256 - scaled.width()) // 2, (256 - scaled.height()) // 2, scaled)
    p.end()
    ok = canvas.save(str(ICO), "ICO")
    return bool(ok and ICO.exists())


def make_shortcut(icon_path: str) -> Path:
    import win32com.client

    desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
    lnk = desktop / "PPT Doctor.lnk"
    ws = win32com.client.Dispatch("WScript.Shell")
    sc = ws.CreateShortcut(str(lnk))
    sc.TargetPath = str(EXE)
    sc.WorkingDirectory = str(DIST)
    sc.IconLocation = f"{icon_path},0"
    sc.Description = "PPT Doctor · PPT 查询助手"
    sc.Save()
    return lnk


def main() -> None:
    if not EXE.exists():
        print("EXE_MISSING", EXE)
        return
    ico_ok = False
    try:
        ico_ok = make_ico()
    except Exception as e:  # noqa: BLE001
        print("ICO_ERR", e)
    icon = str(ICO) if ico_ok else str(EXE)
    lnk = make_shortcut(icon)
    print("ICO_OK" if ico_ok else "ICO_FALLBACK_EXE")
    print("LNK:", lnk, "| exists:", lnk.exists())


if __name__ == "__main__":
    main()
