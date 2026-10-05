"""Choose whitespace gutters in either direction before a geometric fallback."""
from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage


def _gutter(tile: QImage, horizontal: bool) -> tuple[int, int] | None:
    tile = tile.convertToFormat(QImage.Format_RGB888)
    width, height, pitch = tile.width(), tile.height(), tile.bytesPerLine()
    blob = bytes(tile.constBits())
    length = height if horizontal else width
    lo, hi = max(16, length // 4), min(length-16, length * 3 // 4)
    runs = []
    start = None
    for pos in range(lo, hi):
        if horizontal:
            line = blob[pos*pitch:pos*pitch+width*3]
            channels = (line[c::3] for c in range(3))
        else:
            channels = (blob[pos*3+c:(height-1)*pitch+pos*3+c+1:pitch] for c in range(3))
        blank = all(max(channel)-min(channel) <= 14 for channel in channels)
        if blank and start is None:
            start = pos
        if start is not None and (not blank or pos == hi-1):
            end = pos if not blank else pos+1
            if end-start >= 8:
                runs.append((start,end))
            start = None
    if not runs:
        return None
    # Prefer a broad gutter near the centre over isolated gaps in a sentence.
    start,end = max(runs, key=lambda r: (r[1]-r[0])/length - abs((r[0]+r[1])/2-length/2)/length*.25)
    return (start+end)//2, end-start


def split_region(image: QImage, rect: QRect) -> tuple[QRect,QRect]:
    tile = image.copy(rect)
    choices = []
    for horizontal, length in ((True,rect.height()),(False,rect.width())):
        if length < 32:
            continue
        gutter = _gutter(tile,horizontal)
        if gutter:
            cut,gap = gutter
            choices.append((gap/length - abs(cut-length/2)/length*.25, horizontal,cut,gap))
    if choices:
        _,horizontal,cut,gap = max(choices)
        overlap = min(4,gap//4)
    else:
        # No safe whitespace was found: balanced tiles keep all pixels with
        # enough overlap to help a reader reconstruct text at the boundary.
        horizontal = rect.height() >= rect.width()
        length = rect.height() if horizontal else rect.width()
        if length < 32:
            raise ValueError('画面细节过多，无法继续分图；请缩小截图区域')
        cut = length//2
        overlap = min(16,length//8)
    if horizontal:
        return (QRect(rect.x(),rect.y(),rect.width(),cut+overlap),
                QRect(rect.x(),rect.y()+cut-overlap,rect.width(),rect.height()-cut+overlap))
    return (QRect(rect.x(),rect.y(),cut+overlap,rect.height()),
            QRect(rect.x()+cut-overlap,rect.y(),rect.width()-cut+overlap,rect.height()))
