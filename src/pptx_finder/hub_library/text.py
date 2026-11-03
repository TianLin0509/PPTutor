"""Compatibility with Doctor's v6/v7 character/trigram FTS; no theme dictionary."""
import re
import unicodedata
from collections import Counter

try:
    from opencc import OpenCC
    _converter = OpenCC('t2s')
except ImportError:
    _converter = None

TOKEN = re.compile(r'[a-z0-9]+|[㐀-䶿一-鿿豈-﫿]|[^\W_]')
PHRASE = re.compile(r'"([^"]+)"')
QUOTES = str.maketrans({'“': '"', '”': '"', '「': '"', '」': '"', '《': '"', '》': '"'})

def normalize(text):
    value = unicodedata.normalize('NFKC', text or '')
    if _converter is not None:
        value = _converter.convert(value)
    return value.casefold()

def comparable(text):
    return re.sub(r'\s+', ' ', normalize(text)).strip()

def terms(query):
    query = query.translate(QUOTES)
    return [x for x in PHRASE.sub(' ', query).split() if x] + PHRASE.findall(query)

def clause(term):
    base = TOKEN.findall(normalize(term))
    if not base:
        return ''
    if len(base) == 1 and base[0].isascii() and len(base[0]) >= 3:
        word = base[0]
        return ' AND '.join('"' + word[i:i+3] + '"' for i in range(len(word)-2))
    return '"' + ' '.join(base) + '"'

def match_expression(query):
    pieces = [clause(t) for t in terms(query)]
    return ' AND '.join('(' + x + ')' for x in pieces if x)

def verify(text, query):
    value = comparable(text)
    needles = [comparable(x) for x in terms(query)]
    return bool(needles) and all(n and n in value for n in needles)

def excerpt(text, query, width=650):
    """Verbatim excerpts. Search full bodies; excerpt length never limits recall."""
    if len(text) <= width:
        return text, False
    folded = text.casefold()
    positions = [folded.find(t.casefold()) for t in terms(query)]
    positions = [x for x in positions if x >= 0]
    start = max(0, min(positions, default=0) - width // 4)
    if start + width > len(text):
        start = max(0, len(text) - width)
    return text[start:start+width], True

def capabilities():
    return {'nfkc': True, 'casefold': True, 'traditional_to_simplified': _converter is not None,
            'whitespace_normalization': True, 'normalizer': 'Doctor-compatible character/trigram FTS v6/v7'}

def outline_lines(text):
    lines=[]
    for raw in (text or '').splitlines():
        value=raw.strip()
        if len(value)<4: continue
        # Some PDF text joins a page number and its following header.
        value=re.sub(r'^\d+\s*(?=[A-Za-z㐀-鿿])','',value).strip()
        if sum(c.isalpha() for c in value)<3: continue
        lines.append(value)
    return lines

def outline_headings(texts, sample):
    """Remove corpus-specific repeated headers, not a hand-written topic list."""
    common=Counter()
    for text in sample:
        common.update(set(comparable(x) for x in outline_lines(text)))
    threshold=max(3, len(sample)*0.25)
    repeated={x for x,n in common.items() if n>=threshold}
    headings=[]
    for text in texts:
        lines=outline_lines(text)
        useful=[x for x in lines if comparable(x) not in repeated]
        headings.append(' · '.join((useful or lines)[:3])[:220])
    return headings
