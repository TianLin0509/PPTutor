"""核心数据模型。所有模块共享的契约。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SlidePage:
    """单页内容。page_no 为 1-based 放映顺序（已还原，非 slideN.xml 的 N）。"""

    page_no: int
    title: str = ""
    body: str = ""
    notes: str = ""
    smartart: str = ""

    @property
    def raw_text(self) -> str:
        parts = [self.title, self.body, self.notes, self.smartart]
        return "\n".join(p for p in parts if p and p.strip())


@dataclass
class ParsedDeck:
    """一个 .pptx 解析结果。status: ok | encrypted | error。"""

    path: str
    page_count: int = 0
    pages: list[SlidePage] = field(default_factory=list)
    status: str = "ok"
    error: str = ""
    # OpenXML docProps/core.xml 里的 dcterms:created（Unix 秒），读不到就是 0。
    # 文件系统 mtime 会被复制、同步、下载重写，按它分年会把整个库压进「今年」；
    # 这个字段才是稿子真正诞生的时间。
    created_at: float = 0.0


@dataclass
class FileRecord:
    """索引库中的文件记录。"""

    id: int
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    content_hash: str
    page_count: int
    status: str
    error: str = ""


@dataclass
class SearchHit:
    """一次内容命中：在第几页、上下文片段（含高亮标记）。"""

    page_no: int
    snippet: str


@dataclass
class FileResult:
    """一个文件的聚合搜索结果。"""

    file_id: int
    path: str
    name: str
    ext: str
    mtime: float
    size: int
    page_count: int
    status: str
    score: float
    name_hit: bool
    hits: list[SearchHit] = field(default_factory=list)
    # 相关度硬分层的命中质量；命中来源由 name_hit 单独表达。
    # phrase 类既适用于单词全字匹配（FINAL），也适用于多词短语（AI SP）。
    match_kind: str = "partial"
    # P1 版本归组
    group_id: int | None = None
    is_latest: bool = False
    content_hash: str = ""
    duplicate_paths: list[str] = field(default_factory=list)
    # 放在 dataclass 末尾，兼容潜在外部位置参数构造；内部调用全部使用关键字。
    # 默认搜索仍不区分大小写地召回，但排序优先与用户输入大小写一致的命中。
    case_exact: bool = False
    # 「全部文件」范围会返回文件夹（Everything 的行为）。文件夹没有内容、没有页，
    # 双击应该是打开这个目录而不是拿它当文档打开——UI 据此分流。
    # 内容搜索永远不会置位，所以 PPT 那条路的行为一个字节都不变。
    is_dir: bool = False
