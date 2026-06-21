"""版本库：快照 / 列版本 / 重组恢复 / 导出。

按页（part）去重存储：pptx 是 zip，逐 part 内容寻址存进 objects/，重复的只存一份；
每版只记一份 manifest（name→hash 列表）。改几个字只新增变化的 part，大幅省空间。
重组后做保真自检；万一失败，该版回退为完整拷贝（mode=full），保证一定能恢复。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import xxhash

from ..config import data_dir, ext_path
from ..parser import parse_pptx
from ..text_tokenize import tokenize
from . import store


def vault_dir() -> Path:
    p = data_dir() / "vault"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return vault_dir() / "versions.db"


def doc_id_for(path: str) -> str:
    norm = os.path.normcase(os.path.abspath(path))
    return xxhash.xxh64(norm.encode("utf-8")).hexdigest()


def _doc_dir(doc_id: str) -> Path:
    d = vault_dir() / doc_id
    (d / "versions").mkdir(parents=True, exist_ok=True)
    (d / "objects").mkdir(parents=True, exist_ok=True)
    return d


def _objects_dir(doc_id: str) -> Path:
    return _doc_dir(doc_id) / "objects"


def _manifest_path(doc_id: str, version_id: str) -> Path:
    return _doc_dir(doc_id) / "versions" / f"{version_id}.json"


def version_file(doc_id: str, version_id: str) -> Path:
    """mode=full 回退时的完整 pptx 路径。"""
    return _doc_dir(doc_id) / "versions" / f"{version_id}.pptx"


def _file_hash(path: str) -> str:
    h = xxhash.xxh64()
    with open(ext_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _new_vid() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _write_zip(dest: str, doc_id: str, names: list[str], parts: dict[str, str]) -> None:
    objd = _objects_dir(doc_id)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            z.writestr(name, (objd / parts[name]).read_bytes())


def _dedup_store(doc_id: str, path: str) -> tuple[list[str], dict[str, str]]:
    """解压 pptx，逐 part 内容寻址去重存进 objects/，返回 (顺序, name->hash)。"""
    objd = _objects_dir(doc_id)
    names: list[str] = []
    parts: dict[str, str] = {}
    with zipfile.ZipFile(ext_path(path)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            data = zf.read(name)
            h = xxhash.xxh64(data).hexdigest()
            obj = objd / h
            if not obj.exists():
                obj.write_bytes(data)
            names.append(name)
            parts[name] = h
    return names, parts


def _verify(doc_id: str, names: list[str], parts: dict[str, str]) -> bool:
    """重组到临时文件并验证能正常解析（保真自检）。"""
    fd, tmp = tempfile.mkstemp(suffix=".pptx")
    os.close(fd)
    try:
        _write_zip(tmp, doc_id, names, parts)
        return parse_pptx(tmp).status == "ok"
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _change_summary(conn, latest_vid: str, new_pages: list, new_pc: int, old_pc: int) -> str:
    """对比上一版逐页文本，给一句大致改动简述（改了几页 + 页数增减）。"""
    try:
        old = {r["page_no"]: r["content"] for r in conn.execute(
            "SELECT page_no, content FROM version_pages_fts WHERE version_id=?", (latest_vid,))}
    except Exception:  # noqa: BLE001
        old = {}
    new = {pno: txt for pno, txt in new_pages}
    changed_pages = sum(1 for p in (set(old) & set(new)) if (old.get(p) or "") != (new.get(p) or ""))
    parts = []
    if changed_pages:
        parts.append(f"改 {changed_pages} 页")
    d = new_pc - old_pc
    if d > 0:
        parts.append(f"+{d} 页")
    elif d < 0:
        parts.append(f"{d} 页")
    return " · ".join(parts) if parts else "内容微调"


def snapshot(conn, path: str, session_id: str = "") -> str | None:
    """对 path 当前内容拍快照（按页去重）；内容相对最新版未变则跳过（返回 None）。"""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None
    chash = _file_hash(path)
    did = doc_id_for(path)
    latest = store.latest_version(conn, did)
    if latest is not None and latest["content_hash"] == chash:
        return None

    vid = _new_vid()
    _doc_dir(did)
    mode = "dedup"
    names: list[str] = []
    parts: dict[str, str] = {}
    try:
        names, parts = _dedup_store(did, path)
        if not _verify(did, names, parts):
            mode = "full"
    except Exception:  # noqa: BLE001 解压失败 → 完整拷贝兜底
        mode = "full"
    if mode == "full":
        shutil.copy2(ext_path(path), version_file(did, vid))
        names, parts = [], {}

    _manifest_path(did, vid).write_text(
        json.dumps({"mode": mode, "names": names, "parts": parts}), encoding="utf-8"
    )

    # 解析逐页文本（供跨版本搜 + 页数）
    deck = parse_pptx(path)
    pages = []
    if deck.status == "ok":
        pages = [(pg.page_no, tokenize(pg.raw_text)) for pg in deck.pages]
    now = datetime.datetime.now().timestamp()
    try:
        size = os.path.getsize(ext_path(path))
    except OSError:
        size = 0
    changed = (_change_summary(conn, latest["version_id"], pages, deck.page_count, latest["page_count"] or 0)
               if latest is not None else "")
    store.upsert_doc(conn, did, path, now)
    store.add_version(conn, vid, did, now, session_id, deck.page_count, size, chash, changed=changed)
    store.index_pages(conn, did, vid, pages)
    store.set_latest(conn, did, vid)
    conn.commit()
    return vid


def rebuild_to(doc_id: str, version_id: str, dest: str) -> bool:
    """把某版本重组/恢复到 dest（dedup 重组 zip / full 直接拷贝）。"""
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    mf = _manifest_path(doc_id, version_id)
    if not mf.exists():
        return False
    m = json.loads(mf.read_text(encoding="utf-8"))
    if m.get("mode") == "full":
        src = version_file(doc_id, version_id)
        if not src.exists():
            return False
        shutil.copy2(src, ext_path(dest))
        return True
    try:
        _write_zip(ext_path(dest), doc_id, m["names"], m["parts"])
        return True
    except Exception:  # noqa: BLE001
        return False
