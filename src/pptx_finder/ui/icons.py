"""主界面线条图标（v2.0；方案 D 统一 1.5 线宽）：24 网格、圆头线条，按屏幕实际缩放比渲染。

旧图标是 18px 位图直接放大，在 225% 缩放的屏幕上发虚；这里统一走矢量，
任何缩放比下都清晰。路径风格对齐 Lucide（MIT），只保留本应用用到的几枚。
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

PATHS: dict[str, str] = {
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
    "dashboard": ('<rect x="3" y="3" width="7" height="9" rx="1.5"/>'
                  '<rect x="14" y="3" width="7" height="5" rx="1.5"/>'
                  '<rect x="14" y="12" width="7" height="9" rx="1.5"/>'
                  '<rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
    "version": ('<path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5"/>'
                '<path d="M12 7.5V12l3 2"/>'),
    "health": ('<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2'
               '-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7Z"/>'
               '<path d="M3.5 12H9l1-2 2 5 2-7 1.5 4H20.5"/>'),
    "screenshot": '<path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M18 22V8a2 2 0 0 0-2-2H2"/>',
    "imgtext": ('<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2'
                'M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M8 8h8M12 8v9"/>'),
    "materials": ('<rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="9" cy="9" r="2"/>'
                  '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>'),
    "report": ('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
               '<path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>'),
    "settings": ('<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1'
                 'a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7'
                 'l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1'
                 ' 1 1.7v.2a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0'
                 'l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.5a2 2 0 0'
                 ' 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4'
                 '-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'),
    "theme": ('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4'
              'M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    "update": '<circle cx="12" cy="12" r="9"/><path d="m16 12-4-4-4 4M12 16V8"/>',
    "copy": ('<rect x="8" y="8" width="13" height="13" rx="2"/>'
             '<path d="M4 16V5a2 2 0 0 1 2-2h11"/>'),
    "link": ('<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
             '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'),
    "open": ('<path d="M21 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6"/>'
             '<path d="m21 3-9 9M15 3h6v6"/>'),
    "file": ('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
             '<path d="M14 2v6h6"/>'),
    "folder": ('<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2'
               'A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>'),
    "clipboard": ('<rect x="8" y="2" width="8" height="4" rx="1"/>'
                  '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6'
                  'a2 2 0 0 1 2-2h2"/>'),
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "arrow_left": '<path d="M19 12H5m6-6-6 6 6 6"/>',
    "arrow_right": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "keyboard": ('<rect x="2" y="5" width="20" height="14" rx="2"/>'
                 '<path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M7 15h10"/>'),
    "minimize": '<path d="M5 12h14"/>',
    "maximize": '<rect x="5" y="5" width="14" height="14" rx="1.5"/>',
    "restore": ('<rect x="4" y="8" width="12" height="12" rx="1.5"/>'
                '<path d="M8 8V5.5A1.5 1.5 0 0 1 9.5 4h9A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H16"/>'),
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    # —— 方案 D 新增（2026-11-25）：深浅切换、浮窗角标、欢迎页与健康 / 组件页的线性图标 ——
    "moon": '<path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a6.8 6.8 0 0 0 11 11Z"/>',
    "sun": ('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4'
            'M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    "popout": '<path d="M9 5h10v10M19 5 6 18"/>',
    "alert": '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/>',
    "check": '<path d="m5 12 5 5L20 7"/>',
    "refresh": '<path d="M20 11A8 8 0 0 0 5.6 6.6L3 9"/><path d="M3 4v5h5"/><path d="M4 13a8 8 0 0 0 14.4 4.4L21 15"/><path d="M21 20v-5h-5"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
    "star": '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z"/>',
    "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
    "plug": '<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
    "palette": ('<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.6-.9 1.2-1.8-.5-1-.1-2.2 1.1-2.2H17a4 4 0 0 0 4-4'
                'c0-5.5-4-10-9-10Z"/><circle cx="7.5" cy="11" r="1"/><circle cx="10" cy="7" r="1"/>'
                '<circle cx="14.5" cy="7" r="1"/>'),
    "disk": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18M7 17h.01"/>',
}


def _ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    # 至少按 2 倍渲染：主窗在高分屏与普通屏之间拖动时也不糊
    return max(2.0, float(dpr))


@lru_cache(maxsize=512)
def _pixmap(name: str, color: str, size: int, stroke: float, ratio: float) -> QPixmap:
    px = max(1, round(size * ratio))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
           f'stroke-linejoin="round">{PATHS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def icon(name: str, color: str = "#667085", size: int = 18, stroke: float = 1.5) -> QIcon:
    """按名字取一枚线条图标；颜色由调用方按主题 token 传入。"""
    return QIcon(_pixmap(name, color, int(size), float(stroke), _ratio()))
