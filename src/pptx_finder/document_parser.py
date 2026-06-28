"""多格式文档解析的注册表与统一入口。

把"解析一个文件 → 按单元抽出文字"抽象成可按扩展名扩展的注册表：
- pptx 沿用现有 parser.parse_pptx（页 = 单元）；
- txt/docx/xlsx/pdf 等各自登记一个解析器，统一吐回 ParsedDeck（pages 即单元，
  每个单元复用 SlidePage：page_no=单元号、body=该单元文字）。
下游 indexer/db/search 只认 ParsedDeck.pages[].page_no / .raw_text，故零改动。
"""
from __future__ import annotations

import os
import zipfile
from collections.abc import Callable

from lxml import etree

from .config import ext_path
from .models import ParsedDeck, SlidePage
from .parser import parse_pptx

DocParser = Callable[[str], ParsedDeck]

_PARSERS: dict[str, DocParser] = {
    ".pptx": parse_pptx,
}


def doc_result(path: str, units: list[tuple[int, str]], *, status: str = "ok",
               error: str = "") -> ParsedDeck:
    """非 pptx 解析器统一构造结果：units=[(单元号, 文字)] → ParsedDeck（复用 SlidePage）。"""
    pages = [SlidePage(page_no=no, body=text) for no, text in units]
    return ParsedDeck(path=path, page_count=len(pages), pages=pages,
                      status=status, error=error)


def supported_parse_exts() -> tuple[str, ...]:
    """当前能解析「内容」的扩展名（不含仅文件名登记的 .ppt）。"""
    return tuple(_PARSERS)


def parse_document(path: str) -> ParsedDeck:
    """按扩展名分发解析；未知类型返回 status='unsupported' 而非抛异常。"""
    ext = os.path.splitext(path)[1].lower()
    fn = _PARSERS.get(ext)
    if fn is None:
        return ParsedDeck(path=path, status="unsupported", error=f"unsupported ext: {ext}")
    return fn(path)


# ---------- 纯文本 (.txt) ----------

_TXT_LINES_PER_UNIT = 50  # 每个"行块"单元的行数，便于命中定位且不让单元数爆炸


def _decode_text_bytes(data: bytes) -> str:
    """编码识别：utf-8(含 BOM) → GBK → latin-1 兜底（latin-1 永不失败）。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def parse_txt(path: str) -> ParsedDeck:
    """纯文本：读字节 + 编码识别，按固定行数切成"行块"单元。"""
    try:
        with open(ext_path(path), "rb") as f:
            data = f.read()
    except OSError as e:
        return ParsedDeck(path=path, status="error", error=f"open failed: {e}")
    lines = _decode_text_bytes(data).splitlines()
    units: list[tuple[int, str]] = []
    for i in range(0, len(lines), _TXT_LINES_PER_UNIT):
        block = "\n".join(lines[i:i + _TXT_LINES_PER_UNIT])
        if block.strip():
            units.append((len(units) + 1, block))
    return doc_result(path, units)


_PARSERS[".txt"] = parse_txt


# ---------- Word (.docx) ----------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_P = f"{{{_W_NS}}}p"
_W_T = f"{{{_W_NS}}}t"


def parse_docx(path: str) -> ParsedDeck:
    """Word：读 word/document.xml，每个 <w:p> 段落 = 一个单元，段内多 run 无缝拼接。"""
    try:
        with zipfile.ZipFile(ext_path(path)) as zf:
            try:
                data = zf.read("word/document.xml")
            except KeyError:
                return ParsedDeck(path=path, status="error", error="no word/document.xml")
            root = etree.fromstring(data)
    except (OSError, zipfile.BadZipFile) as e:
        return ParsedDeck(path=path, status="error", error=f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 单文件失败不中断批量索引
        return ParsedDeck(path=path, status="error", error=f"{type(e).__name__}: {e}")
    units: list[tuple[int, str]] = []
    for p in root.iter(_W_P):  # iter 递归 → 表格单元格内的 <w:p> 也能抓到
        s = "".join(t.text for t in p.iter(_W_T) if t.text)
        if s.strip():
            units.append((len(units) + 1, s))
    return doc_result(path, units)


_PARSERS[".docx"] = parse_docx


# ---------- Excel (.xlsx) ----------

_X_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xlsx_shared_strings(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """读 xl/sharedStrings.xml → 共享字符串表（每个 <si> 内 <t> 拼接，兼容富文本 run）。"""
    if "xl/sharedStrings.xml" not in names:
        return []
    root = etree.fromstring(zf.read("xl/sharedStrings.xml"))
    si_tag = f"{{{_X_NS}}}si"
    t_tag = f"{{{_X_NS}}}t"
    return ["".join(t.text for t in si.iter(t_tag) if t.text) for si in root.findall(si_tag)]


def _xlsx_sheet_text(data: bytes, sst: list[str]) -> str:
    """解析一张 sheet 的单元格文字：共享串查表 / 内联串 / 数值原样。"""
    root = etree.fromstring(data)
    c_tag = f"{{{_X_NS}}}c"
    v_tag = f"{{{_X_NS}}}v"
    is_tag = f"{{{_X_NS}}}is"
    t_tag = f"{{{_X_NS}}}t"
    cells: list[str] = []
    for c in root.iter(c_tag):
        ctype = c.get("t")
        if ctype == "s":  # 共享字符串：<v> 是 sst 下标
            v = c.find(v_tag)
            if v is not None and v.text and v.text.isdigit():
                idx = int(v.text)
                if 0 <= idx < len(sst):
                    cells.append(sst[idx])
        elif ctype == "inlineStr":  # 内联字符串
            inline = c.find(is_tag)
            if inline is not None:
                cells.append("".join(t.text for t in inline.iter(t_tag) if t.text))
        else:  # 数值 / 公式串
            v = c.find(v_tag)
            if v is not None and v.text:
                cells.append(v.text)
    return " ".join(x for x in cells if x and x.strip())


def parse_xlsx(path: str) -> ParsedDeck:
    """Excel：每张 sheet = 一个单元，单元格文字经共享字符串表还原。"""
    try:
        with zipfile.ZipFile(ext_path(path)) as zf:
            names = zf.namelist()
            sst = _xlsx_shared_strings(zf, names)
            sheet_parts = sorted(
                n for n in names
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            units: list[tuple[int, str]] = []
            for part in sheet_parts:
                try:
                    text = _xlsx_sheet_text(zf.read(part), sst)
                except Exception:  # noqa: BLE001 单表失败不毁整本
                    text = ""
                if text.strip():
                    units.append((len(units) + 1, text))
            return doc_result(path, units)
    except (OSError, zipfile.BadZipFile) as e:
        return ParsedDeck(path=path, status="error", error=f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        return ParsedDeck(path=path, status="error", error=f"{type(e).__name__}: {e}")


_PARSERS[".xlsx"] = parse_xlsx


# ---------- PDF (.pdf) ----------

def parse_pdf(path: str) -> ParsedDeck:
    """PDF：pypdf 抽取每页文本（页 = 单元）。

    pypdf 惰性导入（缺失则返回 error 而非崩整模块）。扫描版（图片型）PDF 抽不出字 →
    status='scanned' 标注，OCR 留后续阶段；加密/损坏 → status='error'。
    """
    try:
        import pypdf
    except ImportError:
        return ParsedDeck(path=path, status="error", error="pypdf not installed")
    try:
        reader = pypdf.PdfReader(ext_path(path))
        n_pages = len(reader.pages)
        units: list[tuple[int, str]] = []
        for i in range(n_pages):
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception:  # noqa: BLE001 单页抽取失败不毁整份
                text = ""
            if text.strip():
                units.append((i + 1, text))
    except Exception as e:  # noqa: BLE001
        return ParsedDeck(path=path, status="error", error=f"{type(e).__name__}: {e}")
    if not units and n_pages > 0:
        return doc_result(path, [], status="scanned",
                          error="no extractable text (likely scanned/image pdf)")
    return doc_result(path, units)


_PARSERS[".pdf"] = parse_pdf
