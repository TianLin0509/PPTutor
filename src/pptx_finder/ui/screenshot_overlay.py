"""Per-monitor frozen frame selection; capture stays in physical pixels."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QGridLayout, QButtonGroup,
                              QPushButton, QToolButton, QLabel, QFrame, QWidget)
from ..screenshots.annotations import Mark, annotated, paint_mark
from .capture_style import STYLE, BLUE, icon, setup_button, shadow


def crop_physical(image: QImage, logical: QRect, bounds: QRect) -> QImage:
    area = logical.normalized().intersected(QRect(0, 0, bounds.width(), bounds.height()))
    if area.width() < 4 or area.height() < 4 or bounds.width() <= 0 or bounds.height() <= 0:
        return QImage()
    sx, sy = image.width() / bounds.width(), image.height() / bounds.height()
    left, top = round(area.x() * sx), round(area.y() * sy)
    right = round((area.x() + area.width()) * sx)
    bottom = round((area.y() + area.height()) * sy)
    result = image.copy(left, top, right - left, bottom - top)
    result.setDevicePixelRatio(1.0)
    return result


class ScreenshotOverlay(QWidget):
    selected = Signal(QImage)
    small_selected = Signal(QImage)
    scroll_selected = Signal(object, object)
    record_selected = Signal(object, object)
    text_selected = Signal(QImage)
    formula_selected = Signal(QImage)
    region_selected = Signal(object)
    cancelled = Signal()

    def __init__(self, image: QImage, geometry: QRect, initial_selection=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._image = image
        self._origin: QPoint | None = None
        self._selection = QRect()
        self.marks = []
        self._drawing = None
        self._mode = 'select'
        self._screen_geometry = QRect(geometry)
        self.setGeometry(geometry)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.toolbar = QWidget(self)
        self.toolbar.setObjectName('captureToolbar')
        self.toolbar.setAttribute(Qt.WA_StyledBackground, True)
        self.toolbar.setStyleSheet(STYLE)
        shadow(self.toolbar)
        rows = QVBoxLayout(self.toolbar)
        rows.setContentsMargins(12, 10, 12, 12)
        rows.setSpacing(8)
        drawing = QHBoxLayout()
        drawing.setSpacing(4)
        rows.addLayout(drawing)
        self.tools = {}
        self._group = QButtonGroup(self)
        for mode, name, tip in [('select','capture','重新框选'),('arrow','arrow','箭头标注'),
                               ('ellipse','ellipse','圈注'),('rect','rect','矩形标注')]:
            button = setup_button(QPushButton(), name)
            button.setFixedSize(32, 32)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCheckable(True)
            button.setChecked(mode == 'select')
            self._group.addButton(button)
            button.clicked.connect(lambda checked=False, m=mode: self._choose_tool(m))
            self.tools[mode] = button
            drawing.addWidget(button)
        self.undo_btn = setup_button(QPushButton(), 'undo')
        self.undo_btn.setFixedSize(32, 32)
        self.undo_btn.setToolTip('撤销标注 · Ctrl + Z')
        self.undo_btn.setAccessibleName('撤销标注')
        self.undo_btn.clicked.connect(self.undo)
        drawing.addWidget(self.undo_btn)
        drawing.addStretch(1)
        self.size_label = QLabel()
        self.size_label.setProperty('role', 'badge')
        drawing.addWidget(self.size_label)
        self.cancel_btn = setup_button(QPushButton(), 'close', role='danger')
        self.cancel_btn.setFixedSize(30, 30)
        self.cancel_btn.setToolTip('取消 · Esc / 右键')
        self.cancel_btn.setAccessibleName('取消截图')
        drawing.addWidget(self.cancel_btn)
        line = QFrame(); line.setProperty('role', 'divider'); line.setFixedHeight(1)
        rows.addWidget(line)
        self.actions = QGridLayout()
        self.actions.setSpacing(4)
        rows.addLayout(self.actions)
        self.confirm_btn = self._action('完成', 'check', primary=True)
        self.scroll_btn = self._action('滚动截图', 'scroll')
        self.record_btn = self._action('录制 GIF', 'record')
        self.small_btn = self._action('小图模式', 'small')
        self.text_btn = self._action('提取文字', 'text')
        self.formula_btn = self._action('识别公式', 'formula')
        self._action_buttons = [self.scroll_btn, self.record_btn, self.small_btn,
                                self.text_btn, self.formula_btn, self.confirm_btn]
        self._layout_actions()
        self.confirm_btn.setToolTip('普通截图：直接复制原图')
        self.small_btn.setToolTip('AI 上传：压缩并按每张 50 KB 分图')
        self.scroll_btn.setToolTip('从当前位置向下滚动，拼成长图')
        self.confirm_btn.clicked.connect(lambda: self._confirm('normal'))
        self.small_btn.clicked.connect(lambda: self._confirm('small'))
        self.scroll_btn.clicked.connect(lambda: self._confirm('scroll'))
        self.record_btn.clicked.connect(lambda: self._confirm('record'))
        self.record_btn.setToolTip('录制选区屏幕变化；停止后自动保存 GIF（无声音）')
        self.text_btn.clicked.connect(lambda: self._confirm('text'))
        self.formula_btn.clicked.connect(lambda: self._confirm('formula'))
        self.formula_btn.setToolTip('框选单个公式 → 本机识别 → LaTeX / Word 输入格式和预览')
        self.text_btn.setToolTip('本机识别选区文字并复制；点击查看文字可核对修改')
        self.cancel_btn.clicked.connect(self.cancelled)
        self.toolbar.hide()
        self.hint = QWidget(self)
        self.hint.setObjectName('captureToolbar')
        self.hint.setAttribute(Qt.WA_StyledBackground, True)
        self.hint.setStyleSheet(STYLE)
        hint_row = QHBoxLayout(self.hint)
        hint_row.setContentsMargins(14, 9, 14, 9); hint_row.setSpacing(12)
        emblem = QLabel(); emblem.setPixmap(icon('capture', BLUE).pixmap(QSize(20, 20)))
        hint_row.addWidget(emblem)
        hint_row.addWidget(QLabel('PPT Doctor'))
        self.hint_text = QLabel('拖动框选 · Esc 取消')
        self.hint_text.setProperty('role', 'muted')
        hint_row.addWidget(self.hint_text)
        self.hint.adjustSize(); self.hint.move(20, 20)
        shadow(self.hint)
        self._update_tools()
        if initial_selection is not None:
            self._selection = QRect(initial_selection).intersected(self.rect())
            self._show_toolbar()

    def _action(self, label, name, *, primary=False):
        button = setup_button(QToolButton(), name, role='primary' if primary else '', size=22)
        button.setText(label)
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setFixedSize(88, 62)
        return button

    def _layout_actions(self):
        while self.actions.count():
            self.actions.takeAt(0)
        columns = 6 if self.width() >= 584 else 3
        for i, button in enumerate(self._action_buttons):
            self.actions.addWidget(button, i // columns, i % columns)

    def _size_text(self):
        sx, sy = self._image.width()/max(1, self.width()), self._image.height()/max(1, self.height())
        a = self._selection
        w = round((a.x()+a.width())*sx) - round(a.x()*sx)
        h = round((a.y()+a.height())*sy) - round(a.y()*sy)
        return f'{w} × {h}'

    def _choose_tool(self, mode):
        self._mode = mode

    def _update_tools(self):
        self.undo_btn.setEnabled(bool(self.marks))
        self.scroll_btn.setEnabled(not self.marks)
        self.record_btn.setEnabled(not self.marks)
        self.record_btn.setToolTip('请先撤销标注再录制' if self.marks else '录制选区屏幕变化；停止后自动保存 GIF（无声音）')
        self.scroll_btn.setToolTip('请先撤销标注再滚动截图' if self.marks else '从当前位置向下滚动，拼成长图')

    def undo(self):
        if self.marks:
            self.marks.pop()
        self._update_tools()
        self.update()

    def _show_toolbar(self):
        if crop_physical(self._image,self._selection,self.rect()).isNull():
            return
        self.size_label.setText(self._size_text())
        self._layout_actions()
        self.toolbar.adjustSize()
        width,height = self.toolbar.width(),self.toolbar.height()
        x = max(0,min(self.width()-width,self._selection.right()-width+1))
        y = self._selection.bottom()+8
        if y+height > self.height():
            y = max(0,self._selection.bottom()-height-8)
        self.toolbar.move(x,y)
        self.toolbar.show()
        self.hint.hide()
        self.update()

    def _point(self,event):
        p = event.position()
        a = self._selection
        return QPointF(max(a.left(),min(a.right(),p.x())),max(a.top(),min(a.bottom(),p.y())))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.drawImage(self.rect(), self._image)
        painter.fillRect(self.rect(), QColor(10, 18, 28, 95))
        if not self._selection.isEmpty():
            area = self._selection.normalized().intersected(self.rect())
            painter.setClipRect(area)
            painter.drawImage(self.rect(), self._image)
            for mark in self.marks:
                paint_mark(painter,mark)
            if self._drawing:
                paint_mark(painter,self._drawing)
            painter.setClipping(False)
            painter.setPen(QPen(QColor(BLUE), 1.5))
            painter.drawRect(QRectF(area))
            painter.setPen(QPen(QColor(BLUE), 3, Qt.SolidLine, Qt.RoundCap))
            length = min(14, area.width()/3, area.height()/3)
            for x, y, dx, dy in ((area.left(),area.top(),1,1), (area.x()+area.width(),area.top(),-1,1),
                                 (area.left(),area.y()+area.height(),1,-1),
                                 (area.x()+area.width(),area.y()+area.height(),-1,-1)):
                painter.drawLine(QPointF(x,y), QPointF(x+dx*length,y))
                painter.drawLine(QPointF(x,y), QPointF(x,y+dy*length))
            if not self.toolbar.isVisible():
                font = QFont('Microsoft YaHei'); font.setPixelSize(11)
                painter.setFont(font)
                text = self._size_text()
                width = painter.fontMetrics().horizontalAdvance(text)+20
                badge = QRect(max(4,min(area.left(),self.width()-width-4)),
                              max(4,area.top()-30),width,24)
                painter.setPen(Qt.NoPen); painter.setBrush(QColor('#ffffff'))
                painter.drawRoundedRect(badge,6,6)
                painter.setPen(QColor('#475467')); painter.drawText(badge,Qt.AlignCenter,text)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.LeftButton:
            if self._mode != 'select' and self._selection.contains(event.position().toPoint()):
                point = self._point(event)
                self._drawing = Mark(self._mode,point,point)
                return
            if self._mode != 'select':
                return
            self.marks.clear()
            self._update_tools()
            self.toolbar.hide()
            self.hint.show()
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)

    def mouseMoveEvent(self, event):
        if self._drawing:
            self._drawing = Mark(self._mode,self._drawing.start,self._point(event))
            self.update()
            return
        if self._origin is not None:
            self._selection = self._drag_rect(event.position().toPoint())
            self.update()

    def _drag_rect(self, endpoint: QPoint) -> QRect:
        # QRect.normalized() uses inclusive endpoints, which would trim two
        # pixels on a reverse drag. Represent a half-open pixel area explicitly.
        start = self._origin
        return QRect(min(start.x(), endpoint.x()), min(start.y(), endpoint.y()),
                     abs(endpoint.x() - start.x()), abs(endpoint.y() - start.y()))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drawing:
            mark = Mark(self._mode,self._drawing.start,self._point(event))
            if (mark.end-mark.start).manhattanLength() >= 3:
                self.marks.append(mark)
            self._drawing = None
            self._update_tools()
            self.update()
            return
        if event.button() == Qt.LeftButton and self._origin is not None:
            self._selection = self._drag_rect(event.position().toPoint())
            self._origin = None
            result = crop_physical(self._image, self._selection, self.rect())
            if not result.isNull():
                self._show_toolbar()
            else:
                self.update()

    def _confirm(self, mode):
        result = crop_physical(self._image,self._selection,self.rect())
        if result.isNull():
            return
        if mode in ('scroll','record') and self.marks:
            return
        self.region_selected.emit(QRect(self._selection))
        if mode == 'scroll':
            self.scroll_selected.emit(QRect(self._selection),QRect(self._screen_geometry))
        elif mode == 'record':
            self.record_selected.emit(QRect(self._selection),QRect(self._screen_geometry))
        elif mode == 'small':
            self.small_selected.emit(annotated(result,self.marks,self._selection))
        elif mode == 'text':
            self.text_selected.emit(result)
        elif mode == 'formula':
            self.formula_selected.emit(result)
        else:
            self.selected.emit(annotated(result,self.marks,self._selection))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key_Return,Qt.Key_Enter):
            self._confirm('normal')
        elif event.key() == Qt.Key_Z and event.modifiers() & Qt.ControlModifier:
            self.undo()
