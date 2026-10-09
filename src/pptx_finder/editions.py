"""Package editions affect bundled components and update downloads, never user data."""
import json
import sys
from pathlib import Path

LABELS = {'base': '基础版', 'ocr': '文字识别版', 'full': '完整版'}
MARKER = '_internal/edition.json'


def read_edition(root: Path) -> str:
    marker = root / MARKER
    if marker.is_file():
        edition = json.loads(marker.read_text('utf-8'))['edition']
        if edition not in LABELS:
            raise ValueError('安装包版本类型无效')
        return edition
    # Legacy releases had no marker and used the full-size update channel.
    return 'full'


def current_edition() -> str:
    return read_edition(Path(sys.executable).parent) if getattr(sys, 'frozen', False) else 'full'


def manifest_name(edition: str) -> str:
    if edition not in LABELS:
        raise ValueError('安装包版本类型无效')
    return 'manifest.json' if edition == 'full' else f'manifest-{edition.title()}.json'


def package_suffix(edition: str) -> str:
    if edition not in LABELS:
        raise ValueError('安装包版本类型无效')
    return '' if edition == 'full' else '-' + edition.title()
