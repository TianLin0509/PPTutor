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
    # 相关度硬分层：完整短语（文件名 > 内容）> 紧凑全字 > 部分命中。
    # phrase 类用于未加引号的多词查询（如 AI SP），旧调用方不传时按部分命中处理。
    match_kind: str = "partial"
    # P1 版本归组
    group_id: int | None = None
    is_latest: bool = False
    content_hash: str = ""
    duplicate_paths: list[str] = field(default_factory=list)
