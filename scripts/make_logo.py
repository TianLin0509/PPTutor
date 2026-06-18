"""把用户给的 logo png（蓝色芯片 ><，深灰底）处理成透明背景、自动裁切、缩放，
   并导出 PNG 资源 + data URI（供 mock HTML 内嵌 / 落地做 app 图标）。

   抠背景用「从四边 flood-fill」：只把与边缘连通且接近背景灰的像素设透明，
   内部黑箭头被蓝色包围、不连通到边缘 → 保留。
"""
from __future__ import annotations

import base64
import os
from collections import deque
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402

SRC = Path(r"C:\Users\lintian\.claude-session-hub\images\20260618122504-173bc6.png")
OUT_DIR = Path(r"C:\Users\lintian\pptx-finder\assets")
OUT_PNG = OUT_DIR / "logo.png"
OUT_B64 = OUT_DIR / "logo_b64.txt"

THRESH = 90  # 与背景色的欧氏距离阈值（容纳抗锯齿过渡）


def dist2(a: QColor, b: QColor) -> int:
    return (a.red() - b.red()) ** 2 + (a.green() - b.green()) ** 2 + (a.blue() - b.blue()) ** 2


def main() -> None:
    QGuiApplication([])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    img = QImage(str(SRC))
    # 先缩到宽 360 处理（加速 + 最终尺寸足够 Retina）
    img = img.scaledToWidth(360, mode=Qt.SmoothTransformation).convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()

    bg = img.pixelColor(2, 2)  # 角落取背景参考色
    t2 = THRESH * THRESH

    # flood-fill from all edge pixels
    visited = bytearray(w * h)
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        for y in (0, h - 1):
            q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            q.append((x, y))

    transparent = QColor(0, 0, 0, 0)
    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        idx = y * w + x
        if visited[idx]:
            continue
        visited[idx] = 1
        c = img.pixelColor(x, y)
        if dist2(c, bg) <= t2:
            img.setPixelColor(x, y, transparent)
            q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    # autocrop 到非透明边界
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 12:
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    if maxx < 0:
        print("CROP_EMPTY")
        return
    pad = 4
    minx, miny = max(0, minx - pad), max(0, miny - pad)
    maxx, maxy = min(w - 1, maxx + pad), min(h - 1, maxy + pad)
    cropped = img.copy(minx, miny, maxx - minx + 1, maxy - miny + 1)

    # 缩到高 160（保持比例）
    if cropped.height() > 160:
        cropped = cropped.scaledToHeight(160, mode=Qt.SmoothTransformation)

    ok = cropped.save(str(OUT_PNG), "PNG")
    data = OUT_PNG.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    uri = f"data:image/png;base64,{b64}"
    OUT_B64.write_text(uri, encoding="utf-8")

    # 同时生成方形 .ico（exe 自身 / 快捷方式图标）
    from PySide6.QtGui import QPainter
    canvas = QImage(256, 256, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    sq = cropped.scaled(228, 228, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pnt = QPainter(canvas)
    pnt.drawImage((256 - sq.width()) // 2, (256 - sq.height()) // 2, sq)
    pnt.end()
    ico_ok = canvas.save(str(OUT_DIR / "app.ico"), "ICO")

    print("SAVED:", OUT_PNG, "ok=", ok)
    print("SIZE:", cropped.width(), "x", cropped.height(), "| png_bytes=", len(data), "| b64_len=", len(b64))
    print("ICO:", OUT_DIR / "app.ico", "ok=", ico_ok)


if __name__ == "__main__":
    main()
