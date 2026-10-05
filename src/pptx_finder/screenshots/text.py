"""Local OCR using the application's existing recognition component."""
from pathlib import Path
from tempfile import TemporaryDirectory
from .encoding import CaptureCancelled, MAX_PIXELS, encode


def ordered_text(rows):
    lines=[]
    for row in sorted(rows,key=lambda r:(r['box'][1],r['box'][0])):
        text=str(row.get('text','')).strip()
        if not text:continue
        x,y,right,bottom=row['box']
        if bottom<=y or right<=x:continue
        matching=next((line for line in reversed(lines)
                       if min(line['bottom'],bottom)-max(line['top'],y)>=
                       .5*min(bottom-y,line['bottom']-line['top'])),None)
        if matching is None:
            matching={'top':y,'bottom':bottom,'words':[]};lines.append(matching)
        matching['words'].append((x,text))
    return '\n'.join('\t'.join(t for x,t in sorted(line['words']))
                     for line in lines)


def recognize_image(image,*,cancelled=None,recognizer=None):
    from .. import imgtext_ocr
    if image.isNull() or image.width()*image.height()>MAX_PIXELS:
        raise ValueError('截图为空或过大，请缩小区域')
    def check():
        if cancelled and cancelled.is_set():raise CaptureCancelled()
    check()
    with TemporaryDirectory(prefix='pptdoctor-shot-text-') as directory:
        source=Path(directory)/'source.png'
        source.write_bytes(encode(image,'PNG'))
        check()
        rows=(recognizer or imgtext_ocr.recognize_one)(source)
        check()
        return ordered_text(rows)
