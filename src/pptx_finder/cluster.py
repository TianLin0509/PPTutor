"""Lightweight near-duplicate grouping for PPT versions.

This replaces the previous datasketch dependency with an in-repo MinHash +
banding implementation. It keeps the same public API while avoiding numpy/scipy
in packaged builds.
"""
from __future__ import annotations

import sqlite3
import struct

import xxhash

from .text_tokenize import normalize

NUM_PERM = 64
THRESHOLD = 0.8
SHINGLE_K = 4
BAND_SIZE = 4


def _shingles(text: str, k: int = SHINGLE_K) -> set[str]:
    t = "".join(normalize(text).split())
    if len(t) <= k:
        return {t} if t else set()
    return {t[i:i + k] for i in range(len(t) - k + 1)}


def _hash(seed: int, value: str) -> int:
    return xxhash.xxh64_intdigest(value, seed=seed)


def minhash_of(text: str) -> tuple[int, ...]:
    shingles = _shingles(text)
    if not shingles:
        return ()
    sig: list[int] = []
    for seed in range(NUM_PERM):
        sig.append(min(_hash(seed, sh) for sh in shingles))
    return tuple(sig)


def _sig_bytes(sig: tuple[int, ...]) -> bytes:
    return b"".join(struct.pack("<Q", x & ((1 << 64) - 1)) for x in sig)


def _file_text(conn: sqlite3.Connection, file_id: int) -> str:
    rows = conn.execute(
        "SELECT raw_text FROM pages_raw WHERE file_id=? ORDER BY page_no", (file_id,)
    ).fetchall()
    return "\n".join((r["raw_text"] or "") for r in rows)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _candidate_pairs(signatures: dict[int, tuple[int, ...]]) -> set[tuple[int, int]]:
    buckets: dict[tuple[int, tuple[int, ...]], list[int]] = {}
    for fid, sig in signatures.items():
        if len(sig) < BAND_SIZE:
            continue
        for start in range(0, len(sig), BAND_SIZE):
            band = sig[start:start + BAND_SIZE]
            if len(band) == BAND_SIZE:
                buckets.setdefault((start, band), []).append(fid)
    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(set(members))
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pairs.add((a, b))
    return pairs


def compute_groups(conn: sqlite3.Connection) -> dict[int, int]:
    """Cluster status='ok' files and write group_id into the minhash table."""
    file_ids = [r["id"] for r in conn.execute("SELECT id FROM files WHERE status='ok'")]
    signatures: dict[int, tuple[int, ...]] = {}
    shingles: dict[int, set[str]] = {}
    for fid in file_ids:
        text = _file_text(conn, fid)
        sh = _shingles(text)
        if not sh:
            continue
        shingles[fid] = sh
        signatures[fid] = minhash_of(text)

    parent = {fid: fid for fid in signatures}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for a, b in _candidate_pairs(signatures):
        if _jaccard(shingles[a], shingles[b]) >= THRESHOLD:
            union(a, b)

    components: dict[int, list[int]] = {}
    for fid in signatures:
        components.setdefault(find(fid), []).append(fid)

    for fid, sig in signatures.items():
        conn.execute(
            "INSERT INTO minhash(file_id, sig, group_id) VALUES(?,?,NULL) "
            "ON CONFLICT(file_id) DO UPDATE SET sig=excluded.sig, group_id=NULL",
            (fid, _sig_bytes(sig)),
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
    """Read file_id -> group_id for multi-member groups."""
    return {
        r["file_id"]: r["group_id"]
        for r in conn.execute("SELECT file_id, group_id FROM minhash WHERE group_id IS NOT NULL")
    }
