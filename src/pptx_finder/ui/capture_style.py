"""Shared white capture surfaces and crisp vector icons, with no asset downloads."""
from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect


INK = '#344054'
BLUE = '#2563eb'

STYLE = '''
QWidget { font-family: "Microsoft YaHei"; font-size: 12px; }
QWidget#captureToolbar, QDialog#recordPanel, QDialog#recordResult,
QDialog#screenshotWindow { background: #ffffff; color: #1d2939; }
QWidget#captureToolbar { border: 1px solid #dfe5ed; border-radius: 14px; }
QLabel { color: #344054; background: transparent; border: none; }
QLabel[role="muted"] { color: #667085; font-size: 11px; }
QLabel[role="badge"] { background: #f2f4f7; color: #475467;
                       border-radius: 6px; padding: 4px 9px; font-size: 11px; }
QPushButton, QToolButton { color: #344054; background: transparent; border: 1px solid transparent;
                         border-radius: 8px; padding: 7px 10px; }
QPushButton:hover, QToolButton:hover { background: #f2f5fa; }
QPushButton:pressed, QToolButton:pressed { background: #e8eef8; }
QPushButton:checked, QToolButton:checked { background: #eaf1ff; color: #1d4ed8; border-color: #d8e6ff; }
QPushButton:focus, QToolButton:focus { border-color: #2563eb; }
QPushButton[role="primary"], QToolButton[role="primary"] {
    color: #ffffff; background: #2563eb; font-weight: 600; border-color: #2563eb; }
QPushButton[role="primary"]:hover, QToolButton[role="primary"]:hover { background: #1d4ed8; }
QPushButton[role="subtle"], QToolButton[role="subtle"] { background: #f6f8fb; border-color: #e7ebf1; }
QPushButton[role="danger"]:hover { color: #b42318; background: #fff1f0; }
QPushButton:disabled, QToolButton:disabled { color: #98a2b3; background: #f9fafb; border-color: transparent; }
QFrame[role="divider"] { background: #edf0f4; border: none; }
QToolTip { color: #344054; background: #fff; border: 1px solid #e1e6ee; padding: 7px; }
'''

# Common 24 px coordinate grid, rounded 1.7 px strokes. Render locally at the
# monitor's actual scale; vectors remain sharp on high-DPI Windows displays.
PATHS = {
    'capture': '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
    'scroll': '<rect x="5" y="3" width="14" height="18" rx="3"/><path d="M12 7v10m-3-3 3 3 3-3"/>',
    'record': '<rect x="3" y="5" width="13" height="14" rx="3"/><path d="m16 10 5-3v10l-5-3"/>',
    'small': '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 5v14m-3-7h14m-6 4 3 3 3-3"/>',
    'text': '<path d="M4 5h16M12 5v14m-4 0h8M4 9V5m16 4V5"/>',
    'formula': '<path d="M18 4H7l7 8-7 8h11M4 8v8"/>',
    'check': '<path d="m5 12 4 4L19 6"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
    'arrow': '<path d="m5 19 14-14M9 5h10v10"/>',
    'ellipse': '<ellipse cx="12" cy="12" rx="9" ry="7"/>',
    'rect': '<rect x="4" y="5" width="16" height="14" rx="2"/>',
    'undo': '<path d="M9 5 4 10l5 5m-5-5h10a6 6 0 0 1 0 12"/>',
    'pause': '<path d="M8 5v14M16 5v14"/>',
    'play': '<path d="m8 4 12 8-12 8Z"/>',
    'stop': '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    'save': '<path d="M5 3h12l4 4v14H3V3h2Zm2 0v6h9V3M7 21v-7h10v7"/>',
    'folder': '<path d="M3 7V5h7l2 3h9v12H3V7Z"/>',
}


def icon(name, colour=INK, size=20):
    ratio = max(2.0, QApplication.primaryScreen().devicePixelRatio())
    pixmap = QPixmap(round(size*ratio), round(size*ratio))
    pixmap.fill(Qt.transparent)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="none" stroke="{colour}" stroke-width="1.7" '
           f'stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode('utf-8')))
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, pixmap.width(), pixmap.height()))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


def shadow(widget):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(24)
    effect.setOffset(0, 5)
    effect.setColor(QColor(16, 24, 40, 38))
    widget.setGraphicsEffect(effect)


def setup_button(button, name, *, role='', size=18):
    button.setIcon(icon(name, '#ffffff' if role == 'primary' else INK, size))
    button.setIconSize(QSize(size, size))
    button.setCursor(Qt.PointingHandCursor)
    if role:
        button.setProperty('role', role)
    return button
