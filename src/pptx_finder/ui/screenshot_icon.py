from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def screenshot_icon(color: str, size: int = 16) -> QIcon:
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(2, 2)
    painter.setPen(QPen(QColor(color), 1.4))
    for x, y, dx, dy in ((3, 3, 1, 1), (size-3, 3, -1, 1), (3, size-3, 1, -1), (size-3, size-3, -1, -1)):
        painter.drawLine(x, y, x + 4 * dx, y)
        painter.drawLine(x, y, x, y + 4 * dy)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
