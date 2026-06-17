"""日志配置：写 data_dir/app.log（滚动），让打包分发后也能排障。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import data_dir

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    root.setLevel(level)
    try:
        fh = RotatingFileHandler(
            data_dir() / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except Exception:  # 日志落盘失败也绝不能阻断应用启动
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)
