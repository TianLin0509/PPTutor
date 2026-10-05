"""Conservative native-resolution overlap matching with fixed-edge handling."""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter,defaultdict
from PySide6.QtGui import QImage, QPainter

MAX_SCROLL_PIXELS = 24_000_000
MAX_SCROLL_HEIGHT = 40_000


class StitchMismatch(ValueError):
    pass


def _rows(image: QImage) -> list[bytes]:
    image = image.convertToFormat(QImage.Format_RGB888)
    data, pitch = image.constBits(),image.bytesPerLine()
    # Sample a spread of native columns, excluding narrow borders/scrollbars.
    left,right = min(8,image.width()//8),max(1,image.width()-max(18,image.width()//25))
    xs = sorted(set(round(left+(right-left)*i/15) for i in range(16)))
    return [bytes(data[y*pitch+x*3+c] for x in xs for c in range(3)) for y in range(image.height())]


def _distance(a: bytes,b: bytes) -> float:
    return sum(abs(x-y) for x,y in zip(a,b))/len(a)


def _fixed_edges(old: list[bytes],new: list[bytes]) -> tuple[int,int]:
    height = len(old)
    top = bottom = 0
    for y in range(min(height//4,250)):
        if _distance(old[y],new[y]) > 1:
            break
        top += 1
    for y in range(height-1,max(height-height//4,height-250),-1):
        if _distance(old[y],new[y]) > 1:
            break
        bottom += 1
    def credible(rows):
        # Plain white paragraph gaps at the viewport edge move with the body;
        # do not mistake those for a fixed browser footer/header.
        return (len(set(rows))>=3 or
                sum(sum(row) for row in rows)/max(1,sum(len(row) for row in rows))<235)
    return (top if top>=20 and credible(old[:top]) else 0,
            bottom if bottom>=20 and credible(old[height-bottom:]) else 0)


@dataclass(frozen=True)
class Match:
    shift: int
    header: int = 0
    footer: int = 0


def overlap(old: QImage,new: QImage) -> Match:
    if old.size()!=new.size() or new.isNull():
        raise StitchMismatch('滚动期间截图区域尺寸改变，已停止')
    a,b = _rows(old),_rows(new)
    h = len(a)
    if old == new:
        return Match(0)
    header,footer = _fixed_edges(a,b)
    start,end = header,h-footer
    span = end-start
    if span < 60:
        raise StitchMismatch('滚动区域过小或固定元素过多，请重新框选正文')
    def score(shift):
        rows = range(start,end-shift,max(1,(span-shift)//90))
        values = [_distance(a[y+shift],b[y]) for y in rows]
        return sum(values)/max(1,len(values))
    # Row signatures propose exact offsets in O(height), so a high-detail
    # image shifted by 137px is not missed by a coarse 4px search grid.
    index = defaultdict(list)
    for y in range(start,end):index[a[y]].append(y)
    votes = Counter()
    limit = max(2,int(span*.65))
    for y in range(start,min(end,start+span//2),max(1,span//12)):
        for position in index.get(b[y],[]):
            shift = position-y
            if 0<shift<limit:votes[shift]+=1
    proposed = {s for s,v in votes.items() if v>=2}
    if proposed:
        offsets = sorted({n for s in proposed for n in range(max(1,s-2),min(limit,s+3))})
    else:
        offsets = range(1,limit)
    scored = [(score(shift),shift) for shift in offsets]
    error,shift = min(scored)
    alternatives = [value for value,offset in scored if abs(offset-shift)>=16]
    if error > 2.5 or (alternatives and min(alternatives)-error < .35):
        raise StitchMismatch('页面重叠区域无法可靠匹配，可能有动画、固定栏或重复内容；已停止')
    return Match(shift,header,footer)


class Stitcher:
    def __init__(self,first: QImage):
        if first.isNull() or first.width()*first.height()>MAX_SCROLL_PIXELS:
            raise StitchMismatch('滚动截图区域为空或过大')
        self.previous = first.copy()
        self.chunks = [first.copy()]
        self.footer = QImage()
        self.total_height = first.height()
        self.frames = 1
        self._footer_height = None

    def add(self,frame: QImage) -> int:
        match = overlap(self.previous,frame)
        if not match.shift:
            return 0
        if self._footer_height is None:
            self._footer_height = match.footer
            if match.footer:
                self.chunks[0] = self.chunks[0].copy(0,0,frame.width(),frame.height()-match.footer)
        elif match.footer != self._footer_height:
            raise StitchMismatch('底部固定栏发生变化，未继续拼接')
        height = self.total_height+match.shift
        if height>MAX_SCROLL_HEIGHT or height*frame.width()>MAX_SCROLL_PIXELS:
            raise StitchMismatch('长图已达到大小上限，请分段截图')
        bottom = frame.height()-match.footer
        self.chunks.append(frame.copy(0,bottom-match.shift,frame.width(),match.shift))
        self.footer = frame.copy(0,bottom,frame.width(),match.footer) if match.footer else QImage()
        self.previous = frame.copy()
        self.total_height = height
        self.frames += 1
        return match.shift

    def result(self) -> QImage:
        result = QImage(self.previous.width(),self.total_height,QImage.Format_RGB32)
        painter = QPainter(result)
        y = 0
        for chunk in self.chunks:
            painter.drawImage(0,y,chunk)
            y += chunk.height()
        if not self.footer.isNull():
            painter.drawImage(0,y,self.footer)
        painter.end()
        result.setDevicePixelRatio(1)
        return result
