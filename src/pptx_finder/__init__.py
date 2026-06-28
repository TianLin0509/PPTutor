"""pptx-finder：本地 PPTX 内容搜索与预览助手。"""

__version__ = "1.0.0"


def main() -> int:
    from .app import main as _main

    return _main()
