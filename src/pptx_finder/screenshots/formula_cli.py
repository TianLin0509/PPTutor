"""Exercise the real frozen formula stack without opening capture windows."""
import json,sys
from pathlib import Path


def main(args):
    from PySide6.QtGui import QImage
    from . import formula_component,formula_preview
    from .formula_format import to_word
    index=args.index('--formula');source=Path(args[index+1]);output=Path(args[index+2])
    result={};code=0
    try:
        if not formula_component.is_installed():formula_component.install()
        latex=formula_component.recognize(QImage(str(source)));result={'latex':latex,'word':''}
        try:result['word']=to_word(latex)
        except ValueError as exc:result['word_warning']=str(exc)
        try:
            image=formula_preview.render(latex);preview=output.with_suffix('.png')
            if not image.save(str(preview)):raise OSError('预览保存失败')
            result['preview']=str(preview)
        except Exception as exc:result['preview_warning']=str(exc)
    except Exception as exc:result={'error':str(exc)};code=1
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return code
