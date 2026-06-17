"""版本归组：MinHash-LSH 近似重复检测，把同一份 PPT 的多个版本聚为一组。

纯本地、无大模型。全文字符级 shingle 的 Jaccard 相似度 ≥ 阈值即判同源
（用户场景：版本间通常仅少数页差异，Jaccard 多在 0.85~0.97）。
差异定位（精确到第几页）属 P2，本模块不做。
"""
from __future__ import annotations

import sqlite3

from datasketch import MinHash, MinHashLSH

from .text_tokenize import normalize

NUM_PERM = 128
THRESHOLD = 0.8
SHINGLE_K = 4


def _shingles(text: str, k: int = SHINGLE_K) -> set[str]:
    t = "".join(normalize(text).split())
    if len(t) <= k:
        return {t} if t else set()
    return {t[i:i + k] for i in range(len(t) - k + 1)}


def minhash_of(text: str) -> MinHash:
    m = MinHash(num_perm=NUM_PERM)
    for sh in _shingles(text):
        m.update(sh.encode("utf-8"))
    return m


def _file_text(conn: sqlite3.Connection, file_id: int) -> str:
    rows = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? ORDER BY page_no", (file_id,)
    ).fetchall()
    return "\n".join((r["raw_text"] or "") for r in rows)


def compute_groups(conn: sqlite3.Connection) -> dict[int, int]:
    """对全库 status='ok' 文件聚类，写入 minhash 表的 group_id。
    返回 {file_id: group_id}，仅含多成员组（单文件不归组）。
    """
    file_ids = [r["id"] for r in conn.execute("SELECT id FROM files WHERE status='ok'")]
    lsh = MinHashLSH(threshold=THRESHOLD, num_perm=NUM_PERM)
    mhs: dict[int, MinHash] = {}
    for fid in file_ids:
        text = _file_text(conn, fid)
        if not text.strip():
            continue
        m = minhash_of(text)
        mhs[fid] = m
        lsh.insert(str(fid), m)

    # 并查集：把相似对连通成组
    parent = {fid: fid for fid in mhs}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for fid, m in mhs.items():
        for other in lsh.query(m):
            o = int(other)
            if o != fid:
                union(fid, o)

    components: dict[int, list[int]] = {}
    for fid in mhs:
        components.setdefault(find(fid), []).append(fid)

    # 写库：保存签名；多成员组分配稳定 group_id（成员最小 id），单成员置 NULL
    for fid, m in mhs.items():
        sig = m.hashvalues.astype("uint64").tobytes()
        conn.execute(
            "INSERT INTO minhash(file_id, sig, group_id) VALUES(?,?,NULL) "
            "ON CONFLICT(file_id) DO UPDATE SET sig=excluded.sig, group_id=NULL",
            (fid, sig),
        )
    result: dict[int, int] = {}
    for members in components.values():
        if len(members) < 2:
            continue
        gid = min(members)
        for fid in members:
            conn.execute("UPDATE minhash SET group_id=? WHERE file_id=?", (gid, fid))
            result[fid] = gid
    conn.commit()
    return result


def group_map(conn: sqlite3.Connection) -> dict[int, int]:
    """读取 file_id -> group_id（仅多成员组）。"""
    return {
        r["file_id"]: r["group_id"]
        for r in conn.execute("SELECT file_id, group_id FROM minhash WHERE group_id IS NOT NULL")
    }
