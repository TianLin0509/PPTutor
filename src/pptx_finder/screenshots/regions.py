"""Last selection is valid only for the same monitor geometry and pixel scale."""
from PySide6.QtCore import QRect


def binding(screen,image,area):
    geometry=screen.geometry()
    return {'screen':screen.name(),
            'bounds':[geometry.x(),geometry.y(),geometry.width(),geometry.height()],
            'pixels':[image.width(),image.height()],
            'area':[area.x(),area.y(),area.width(),area.height()]}


def matching_region(saved,screen,image):
    if not saved:return None
    if (binding(screen,image,QRect()) | {'area':saved['area']}) != saved:return None
    return QRect(*saved['area'])
