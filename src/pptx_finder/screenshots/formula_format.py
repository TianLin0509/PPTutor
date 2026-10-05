"""Bounded LaTeX -> MathML -> Word UnicodeMath, with explicit unsupported cases."""
from lxml import etree
import unicodedata,re

MAX_SOURCE = 4096
_FUNCTIONS = {'sin','cos','tan','cot','sec','csc','sinh','cosh','tanh','arcsin','arccos','arctan',
              'log','ln','exp','max','min','det','gcd','lim','limsup','liminf','sup','inf'}
_ACCENTS = {'→':'\u20d7','^':'\u0302','~':'\u0303','¯':'\u0305','˙':'\u0307','¨':'\u0308','ˇ':'\u030c','˘':'\u0306'}


class FormulaFormatError(ValueError):
    pass


def clean_latex(source):
    if not isinstance(source,str):raise FormulaFormatError('公式组件返回内容无效，请重试')
    source=source.strip()
    for opening,closing in [('$$','$$'),('\\[','\\]'),('\\(','\\)'),('$','$')]:
        if source.startswith(opening) and source.endswith(closing):
            source=source[len(opening):-len(closing)].strip();break
    if not source or len(source)>MAX_SOURCE or '\0' in source:
        raise FormulaFormatError('请框选单个公式；公式为空或过长')
    return source


def _tag(node):return etree.QName(node).localname


def _style(text,variant):
    if variant=='normal':return '"'+text.replace('"','""')+'"'
    bases={'bold':(0x1D400,0x1D41A,0x1D7CE),'double-struck':(0x1D538,0x1D552,0x1D7D8),
           'script':(0x1D49C,0x1D4B6,None),'fraktur':(0x1D504,0x1D51E,None),
           'bold-italic':(0x1D468,0x1D482,None)}
    special={'double-struck':{'C':'ℂ','H':'ℍ','N':'ℕ','P':'ℙ','Q':'ℚ','R':'ℝ','Z':'ℤ'},
             'script':{'B':'ℬ','E':'ℰ','F':'ℱ','H':'ℋ','I':'ℐ','L':'ℒ','M':'ℳ','R':'ℛ','e':'ℯ','g':'ℊ','o':'ℴ'},
             'fraktur':{'C':'ℭ','H':'ℌ','I':'ℑ','R':'ℜ','Z':'ℨ'}}
    if variant not in bases:raise FormulaFormatError('暂不支持此数学字体：'+variant)
    upper,lower,digit=bases[variant];result=''
    for char in text:
        if char in special.get(variant,{}):result+=special[variant][char]
        elif 'A'<=char<='Z':result+=chr(upper+ord(char)-65)
        elif 'a'<=char<='z':result+=chr(lower+ord(char)-97)
        elif '0'<=char<='9' and digit:result+=chr(digit+ord(char)-48)
        else:raise FormulaFormatError('此字体含尚未支持的符号，请保留 LaTeX 使用')
    return result


def to_word(source):
    from latex2mathml.converter import convert
    try:
        source=clean_latex(source)
        if re.search(r'\\(?:newcommand|renewcommand|providecommand|def|gdef|edef|xdef|let|newenvironment|mathchoice)\b',source):
            raise FormulaFormatError('自定义宏暂不支持 Word 转换，请保留 LaTeX 使用')
        xml=convert(source)
        root=etree.fromstring(xml.encode('utf-8'),etree.XMLParser(resolve_entities=False,no_network=True))
        result=_emit(root,0)
        if not result.strip() or '\\' in result:raise FormulaFormatError('此公式含暂不支持的命令')
        return result.strip()
    except FormulaFormatError:raise
    except Exception as exc:raise FormulaFormatError('公式结构无法转换为 Word，请检查 LaTeX') from exc


def _emit(node,depth):
    if depth>40:raise FormulaFormatError('公式嵌套过深')
    tag=_tag(node);children=list(node)
    if node.get('mathcolor') or (children and node.get('mathvariant')):
        raise FormulaFormatError('此数学字体或颜色暂不支持 Word 转换，请保留 LaTeX 使用')
    emit=lambda n:_emit(n,depth+1)
    group=lambda n:'('+emit(n)+')'
    if tag in ('math','mrow','mtd','mstyle'):
        if tag=='mstyle' and node.get('mathvariant'):
            raise FormulaFormatError('暂不支持此组数学字体，请保留 LaTeX 使用')
        result=''.join(emit(c) for c in children)
        # A cases environment has an opening brace and table without a closing
        # fence. Word's empty right delimiter is the UnicodeMath symbol ┤.
        if tag=='mrow' and children and _tag(children[0])=='mo' and children[0].text=='{' and any(_tag(c)=='mtable' for c in children) and _tag(children[-1])=='mtable':
            result+='┤'
        return result
    if tag in ('mi','mn','mo','mtext'):
        text=node.text or ''
        if any('BOLD' in unicodedata.name(char,'') for char in text):
            raise FormulaFormatError('Word 输入无法可靠保留粗体数学含义，请复制 LaTeX 并在 Word 选择 LaTeX 输入')
        if '\\' in text:raise FormulaFormatError('暂不支持的命令：'+text)
        if tag=='mo' and len(text)>1 and text.isalpha() and text not in _FUNCTIONS:
            raise FormulaFormatError('此命名算子暂不支持 Word 转换，请保留 LaTeX 使用')
        variant=node.get('mathvariant')
        if variant and 'bold' in variant:raise FormulaFormatError('粗体数学含义请用 LaTeX 格式保留')
        if tag=='mtext':return '"'+text.replace('"','""')+'"'
        if variant and variant!='italic':text=_style(text,variant)
        if text in _FUNCTIONS:return text+' '
        if text=='\u2062':return ' '
        return text
    if tag=='mfrac' and len(children)==2:
        if node.get('linethickness') in ('0','0px','0pt'):raise FormulaFormatError('暂不支持无分数线结构')
        return group(children[0])+'/'+group(children[1])
    if tag=='msqrt':return '√('+''.join(emit(c) for c in children)+')'
    if tag=='mroot' and len(children)==2:return '√('+emit(children[1])+'&'+emit(children[0])+')'
    if tag in ('msub','msup','msubsup'):
        if len(children)!={'msub':2,'msup':2,'msubsup':3}[tag]:raise FormulaFormatError('上下标结构不完整')
        base=emit(children[0]).rstrip()
        if _tag(children[0]) not in ('mi','mo','mn'):base=group(children[0])
        if tag=='msub':return base+'_'+group(children[1])+' '
        if tag=='msup':return base+'^'+group(children[1])+' '
        return base+'_'+group(children[1])+'^'+group(children[2])+' '
    if tag in ('munder','mover','munderover'):
        if len(children)<2:raise FormulaFormatError('上下方结构不完整')
        mark=children[1].text if _tag(children[1])=='mo' else None
        if tag=='mover' and mark in _ACCENTS:return group(children[0])+_ACCENTS[mark]
        base=emit(children[0]).rstrip()
        if base in ('∑','∏','∫','∬','∭','⋃','⋂','lim','max','min'):
            if tag=='mover':return base+'^'+group(children[1])+' '
            result=base+'_'+group(children[1])
            if tag=='munderover':result+='^'+group(children[2])
            return result+' '
        raise FormulaFormatError('暂不支持此上下方装饰结构')
    if tag=='mtable':
        rows=[]
        for row in children:
            if _tag(row)!='mtr' or any(_tag(c)!='mtd' for c in row):raise FormulaFormatError('暂不支持带编号的公式表格')
            rows.append('&'.join(emit(c) for c in row))
        if not rows:raise FormulaFormatError('公式表格为空')
        return '■('+'@'.join(rows)+')'
    if tag=='mspace':
        if node.get('linebreak'):raise FormulaFormatError('此多行公式暂不支持 Word 转换，请保留 LaTeX 使用')
        return ' '
    if tag=='mfenced':
        sep=node.get('separators',',')
        if len(sep)!=1:raise FormulaFormatError('暂不支持此分隔符')
        return node.get('open','(')+sep.join(emit(c) for c in children)+node.get('close',')')
    raise FormulaFormatError('Word 转换暂不支持：'+tag)
