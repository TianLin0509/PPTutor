"""Vector marks in overlay coordinates, burned into native pixels at delivery."""
from dataclasses import dataclass
import math
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF


@dataclass(frozen=True)
class Mark:
    kind: str
    start: QPointF
    end: QPointF


def paint_mark(painter: QPainter, mark: Mark):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor('#e43c42'), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    bounds=QRectF(mark.start,mark.end).normalized()
    if mark.kind=='ellipse':
        painter.drawEllipse(bounds)
    elif mark.kind=='rect':
        painter.drawRect(bounds)
    elif mark.kind=='arrow':
        dx,dy=mark.end.x()-mark.start.x(),mark.end.y()-mark.start.y()
        length=math.hypot(dx,dy)
        if length>=2:
            painter.drawLine(mark.start,mark.end)
            angle=math.atan2(dy,dx)
            size=min(12,length*.45)
            points=[mark.end]
            for turn in [-.5,.5]:
                points.append(mark.end-QPointF(size*math.cos(angle+turn),size*math.sin(angle+turn)))
            painter.setBrush(QColor('#e43c42'))
            painter.drawPolygon(QPolygonF(points))
    painter.restore()


def annotated(image: QImage, marks: list[Mark], selection) -> QImage:
    if not marks:
        return image
    result=image.copy()
    painter=QPainter(result)
    painter.scale(image.width()/selection.width(),image.height()/selection.height())
    painter.translate(-selection.x(),-selection.y())
    for mark in marks:paint_mark(painter,mark)
    painter.end()
    return result
