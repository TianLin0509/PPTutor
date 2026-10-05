"""Render local KaTeX to a bitmap with a private headless system browser."""
import json,os
from pathlib import Path
from tempfile import TemporaryDirectory
from PySide6.QtGui import QImage
from .formula_format import clean_latex
from .formula_process import run


def trim_white(image):
    from PySide6.QtCore import QRect
    gray=image.convertToFormat(QImage.Format_Grayscale8);pixels=gray.constBits();stride=gray.bytesPerLine()
    left=gray.width();right=-1;top=gray.height();bottom=-1
    for y in range(gray.height()):
        row=pixels[y*stride:y*stride+gray.width()]
        ink=[x for x,value in enumerate(row) if value<255]
        if ink:left=min(left,ink[0]);right=max(right,ink[-1]);top=min(top,y);bottom=y
    if right<left:raise ValueError('公式预览没有可见内容，请核对 LaTeX')
    bounds=QRect(left,top,right-left+1,bottom-top+1).adjusted(-16,-16,16,16).intersected(image.rect())
    return image.copy(bounds)


def browser_path():
    roots=[os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')]
    for root in roots:
        if not root:continue
        for suffix in ['Microsoft/Edge/Application/msedge.exe','Google/Chrome/Application/chrome.exe']:
            path=Path(root)/suffix
            if path.is_file():return path
    return None


def asset_path():
    import sys
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[3]))
    return root/'assets/formula-katex.html'


def preview_html(source):
    expression=json.dumps(clean_latex(source),ensure_ascii=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    library=asset_path().read_text(encoding='utf-8')
    return '''<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:"><link rel="icon" href="data:,">'''+library+'''<style>body{margin:0;background:#fff;color:#243249}#equation{display:flex;align-items:center;justify-content:center;min-height:260px;font-size:30px;padding:20px;box-sizing:border-box}.katex-display{margin:0}</style><div id="equation"></div><script>
const element=document.getElementById('equation');
try{katex.render('''+expression+''',element,{displayMode:true,throwOnError:true,trust:false,strict:'error',maxExpand:100,maxSize:20});document.title=(element.scrollWidth>1400||element.scrollHeight>300)?'FORMULA_CLIPPED':'FORMULA_OK'}
catch(error){element.textContent='预览暂不支持此公式，请核对 LaTeX';document.title='FORMULA_ERROR'}
</script>'''


def render(source,*,cancelled=None):
    browser=browser_path()
    if not browser:raise RuntimeError('未找到本机 Edge 或 Chrome，仍可复制公式语法')
    with TemporaryDirectory(prefix='pptdoctor-formula-preview-') as directory:
        root=Path(directory);html=root/'preview.html';png=root/'preview.png'
        html.write_text(preview_html(source),encoding='utf-8')
        args=[str(browser),'--headless=new','--disable-gpu','--hide-scrollbars','--disable-extensions',
              '--disable-background-networking','--no-first-run','--no-default-browser-check',
              '--window-size=1400,300','--force-device-scale-factor=1','--virtual-time-budget=1800',
              '--user-data-dir='+str(root/'profile'),'--dump-dom','--screenshot='+str(png),html.as_uri()]
        with (root/'dom.html').open('wb') as output:run(args,cancelled=cancelled,timeout=25,stdout=output)
        dom=(root/'dom.html').read_text(encoding='utf-8',errors='replace')
        if '<title>FORMULA_OK</title>' not in dom:
            raise ValueError('公式预览无法完整显示，请核对 LaTeX；仍可复制语法')
        image=QImage(str(png))
        if image.isNull():raise RuntimeError('公式预览未生成，仍可复制公式语法')
        return trim_white(image)
