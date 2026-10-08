"""Incremental GIF encoding: bounded frame memory, timed frames, atomic publish."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from PIL import GifImagePlugin, Image


class GifStream:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.partial')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError('GIF 文件已存在，请使用其他名称')
        self.output = self.temporary.open('xb')
        self.size = None
        self.frames = 0
        self.duration_ms = 0

    def append(self, image: Image.Image, duration_ms: int):
        if image.width * image.height > 24_000_000 or min(image.size) < 4:
            raise ValueError('录制区域过大或过小，请重新框选')
        if self.size is not None and image.size != self.size:
            raise ValueError('显示器分辨率发生变化，请停止并重新框选')
        if self.output is None:
            raise ValueError('GIF 编码已结束')
        if self.size is None:
            self.size = image.size
        # GIF supports at most 256 colours. Keep physical dimensions unchanged;
        # use per-frame palettes so later screen content can introduce colours.
        frame = image.convert('RGB').quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                                               dither=Image.Dither.NONE)
        duration = max(10, min(655350, round(duration_ms / 10) * 10))
        if not self.frames:
            header, _ = GifImagePlugin.getheader(frame, info={'loop': 0, 'optimize': False})
            for block in header:
                self.output.write(block)
        for block in GifImagePlugin.getdata(frame, duration=duration, disposal=1,
                                             include_color_table=bool(self.frames)):
            self.output.write(block)
        self.frames += 1
        self.duration_ms += duration

    def finish(self) -> Path:
        if not self.frames or self.output is None:
            raise ValueError('尚未录到画面，GIF 未保存')
        self.output.write(b';')
        self.output.flush()
        os.fsync(self.output.fileno())
        self.output.close()
        self.output = None
        # Windows rename is atomic and refuses to replace another file. Use an
        # exclusive hard link elsewhere to provide the same no-overwrite rule.
        if os.name == 'nt':
            os.rename(self.temporary, self.path)
        else:
            os.link(self.temporary, self.path)
            self.temporary.unlink()
        return self.path

    def discard(self):
        if self.output is not None:
            self.output.close()
            self.output = None
        self.temporary.unlink(missing_ok=True)


def pil_image(image):
    from PySide6.QtGui import QImage
    rgb = image.convertToFormat(QImage.Format_RGB888)
    return Image.frombytes('RGB', (rgb.width(), rgb.height()), bytes(rgb.constBits()),
                           'raw', 'RGB', rgb.bytesPerLine())
