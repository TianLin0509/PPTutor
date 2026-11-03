"""Read existing Doctor SQLite content, without migrations, scanning or writes."""
import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import VERSION
from .text import capabilities, comparable, excerpt, match_expression, normalize, outline_headings, terms, verify
from .selection import diverse_candidates

class PptutorError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

def digest(text):
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()

def default_db():
    if os.environ.get('PPTUTOR_DB'):
        return Path(os.environ['PPTUTOR_DB'])
    if os.environ.get('PPTX_FINDER_DATA_DIR'):
        return Path(os.environ['PPTX_FINDER_DATA_DIR']) / 'index.db'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'pptx-finder/index.db'
    return Path.home() / '.local/share/pptx-finder/index.db'

class Library:
    def __init__(self, path=None, roots=None, extensions=None, seconds=12):
        if not 0<float(seconds)<=120:
            raise PptutorError('INVALID_BUDGET', '单次读取预算应大于 0 且不超过 120 秒。')
        self.path = Path(path or default_db()).absolute()
        if not self.path.is_file():
            raise PptutorError('INDEX_NOT_FOUND', '未找到 Doctor 索引；请指定 --db 或 PPTUTOR_DB。')
        self.roots = [str(Path(p).absolute()).replace('\\', '/').rstrip('/').casefold() for p in roots or []]
        self.extensions = tuple(extensions or ('.pptx', '.ppt'))
        if any(not e.startswith('.') or len(e) > 12 for e in self.extensions):
            raise PptutorError('INVALID_EXTENSIONS', '扩展名应带点，例如 .pptx 或 .pdf。')
        self.deadline = time.monotonic() + seconds
        uri=self.path.as_uri()
        if str(self.path).startswith('\\\\'):
            from urllib.parse import quote
            uri='file:////'+quote(str(self.path).replace('\\','/').lstrip('/'),safe='/:')
        self.conn = sqlite3.connect(uri + '?mode=ro', uri=True, timeout=0.4)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA query_only=ON')
        self.conn.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 2000)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'files', 'pages_raw', 'pages_fts'} <= tables:
            self.close()
            raise PptutorError('INCOMPATIBLE_SCHEMA', '索引缺少 files/pages_raw/pages_fts；没有修改索引。')
        self.meta = dict(self.conn.execute('SELECT key,value FROM meta')) if 'meta' in tables else {}
        version = self.meta.get('index_version')
        if version not in ('6', '7'):
            self.close()
            raise PptutorError('UNSUPPORTED_INDEX_VERSION', f'当前索引版本 {version!r} 尚未验证；没有清库或迁移。')
        self.index_id = digest(str(self.path).lower())[:16]
        # A short read transaction pins WAL snapshots for each operation.
        self.conn.execute('BEGIN')

    def close(self):
        if getattr(self, 'conn', None) is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def filters(self, alias='f'):
        predicates = [f"lower({alias}.ext) IN ({','.join('?' for _ in self.extensions)})"]
        params = [e.casefold() for e in self.extensions]
        if self.roots:
            normalized = f"lower(replace({alias}.path, char(92), '/'))"
            alternatives = []
            for root in self.roots:
                alternatives.append(f'({normalized}=? OR substr({normalized},1,?)=?)')
                params.extend((root, len(root)+1, root + '/'))
            predicates.append('(' + ' OR '.join(alternatives) + ')')
        return predicates, params

    def scope(self):
        return {'index_path': str(self.path), 'index_id': self.index_id,
                'index_version': self.meta.get('index_version'), 'roots': self.roots,
                'extensions': list(self.extensions), 'last_completed_scan_at': self.meta.get('last_completed_scan_at'),
                'read_only': True, 'text_source': 'Doctor pages_raw (extracted body/notes/SmartArt combined)',
                'images': 'P2: not inspected', 'normalization': capabilities()}

    def status(self):
        pred, params = self.filters()
        where = ' AND '.join(pred)
        groups = [dict(r) for r in self.conn.execute(f'SELECT f.ext,f.status,count(*) AS files FROM files f WHERE {where} GROUP BY f.ext,f.status', params)]
        pages = self.conn.execute(f"SELECT count(*) FROM pages_raw pr JOIN files f ON f.id=pr.file_id WHERE {where}", params).fetchone()[0]
        text_pages = self.conn.execute(f"SELECT count(*) FROM pages_raw pr JOIN files f ON f.id=pr.file_id WHERE {where} AND length(trim(coalesce(pr.raw_text,'')))>0", params).fetchone()[0]
        return {'ok': True, 'version': VERSION, 'scope': self.scope(), 'file_status_counts': groups,
                'indexed_pages': pages, 'nonempty_text_pages': text_pages,
                'warnings': ([] if capabilities()['traditional_to_simplified'] else ['OpenCC 未安装：繁简转换不可用；索引若已转换为简体，请使用简体查询。'])}

    def catalog(self, file_id=None, offset=0, limit=30):
        if not 1 <= limit <= 100 or offset < 0:
            raise PptutorError('INVALID_BUDGET', '目录单次 limit 应为 1–100，offset 非负。')
        pred, params = self.filters()
        if file_id is not None:
            pred.append('f.id=?'); params.append(int(file_id))
            rows = list(self.conn.execute(f"SELECT f.id AS file_id,f.path,f.name,pr.page_no,pr.raw_text FROM files f JOIN pages_raw pr ON pr.file_id=f.id WHERE {' AND '.join(pred)} ORDER BY pr.page_no LIMIT ? OFFSET ?", params + [limit+1, offset]))
            sample=[r[0] for r in self.conn.execute('SELECT raw_text FROM pages_raw WHERE file_id=? ORDER BY page_no LIMIT 400',(int(file_id),))]
            headings=outline_headings([r['raw_text'] for r in rows[:limit]],sample)
            items = [{'file_id': r['file_id'], 'name': r['name'], 'page': r['page_no'],
                      'heading': heading} for r,heading in zip(rows[:limit],headings)]
        else:
            rows = list(self.conn.execute(f"SELECT f.id AS file_id,f.name,f.path,f.page_count,f.status,f.indexed_at FROM files f WHERE {' AND '.join(pred)} AND f.page_count>0 ORDER BY f.indexed_at DESC,f.id DESC LIMIT ? OFFSET ?", params + [limit+1, offset]))
            items = [dict(r) for r in rows[:limit]]
        return {'ok': True, 'version': VERSION, 'scope': self.scope(), 'items': items,
                'has_more': len(rows)>limit, 'next_offset': offset+len(items) if len(rows)>limit else None}

    def reference(self, row):
        return {'index_id': self.index_id, 'file_id': row['file_id'], 'page': row['page_no'],
                'page_hash': digest(row['raw_text'] or ''), 'content_hash': row['content_hash'] or '',
                'path': row['path'], 'name': row['name'], 'indexed_at': row['indexed_at'],
                'size': row['size'], 'mtime': row['mtime']}

    def _search_one(self, query, limit=60, scan_cap=2000):
        expression = match_expression(query)
        if not expression:
            raise PptutorError('EMPTY_QUERY', '查询应包含文字或数字。')
        pred, params = self.filters()
        pred.append("f.status='ok'")
        pred.append('pages_fts MATCH ?'); params.append(expression)
        sql = f"""SELECT f.id AS file_id,f.path,f.name,f.content_hash,f.indexed_at,f.mtime,f.size,
          pr.page_no,pr.raw_text,bm25(pages_fts) AS lexical_rank
          FROM pages_fts JOIN files f ON f.id=pages_fts.file_id
          JOIN pages_raw pr ON pr.file_id=pages_fts.file_id AND pr.page_no=pages_fts.page_no
          WHERE {' AND '.join(pred)} ORDER BY lexical_rank,f.id,pr.page_no LIMIT ?"""
        rows = list(self.conn.execute(sql, params+[scan_cap+1]))
        verified = [r for r in rows[:scan_cap] if verify(r['raw_text'] or '', query)]
        selected = verified[:limit]
        # Filename matches get a separate, explicit path. Expand to actual pages
        # for model reading; never turn a filename into a page-level evidence claim.
        remaining = limit-len(selected)
        name_pages = []
        name_truncated = False
        if remaining:
            npred, nparams = self.filters()
            npred.append("f.status='ok'")
            for needle in [normalize(t) for t in terms(query)]:
                npred.append('instr(lower(f.name_norm),?)>0'); nparams.append(needle)
            names = list(self.conn.execute(f"SELECT f.id AS file_id,f.path,f.name,f.content_hash,f.indexed_at,f.mtime,f.size,pr.page_no,pr.raw_text FROM files f JOIN pages_raw pr ON pr.file_id=f.id WHERE {' AND '.join(npred)} ORDER BY f.id,pr.page_no LIMIT ?", nparams+[min(remaining*3,180)+1]))
            occupied = {(r['file_id'],r['page_no']) for r in selected}
            name_pages = [r for r in names if (r['file_id'],r['page_no']) not in occupied][:remaining]
            name_truncated = len(names)>min(remaining*3,180) or len(name_pages)==remaining
        return [(r, 'body') for r in selected] + [(r, 'filename_context') for r in name_pages], {
            'query': query, 'fts_scanned': min(len(rows),scan_cap), 'verified_body_hits': len(verified),
            'returned': len(selected)+len(name_pages), 'fts_scan_truncated': len(rows)>scan_cap,
            'body_return_truncated': len(verified)>limit, 'filename_return_truncated': name_truncated}

    def search(self, plan, limit=30, per_query=60, *, parallel=True):
        if not 1 <= limit <= 100 or not 1 <= per_query <= 100:
            raise PptutorError('INVALID_BUDGET', 'limit/per-query 应为 1–100。')
        raw_queries = plan.get('queries', [])
        if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= 16:
            raise PptutorError('INVALID_PLAN', '计划应包含 1–16 个 queries。')
        queries, seen = [], set()
        # Original input always has a retrieval channel, not a hard ranking bonus.
        original = plan.get('query', '').strip()
        if original:
            raw_queries = [{'query': original, 'rationale': '用户原词'}] + raw_queries
        for q in raw_queries:
            if isinstance(q, str): q = {'query': q}
            if not isinstance(q, dict):
                raise PptutorError('INVALID_PLAN', '每个查询应为字符串或含 query 的对象。')
            value = q.get('query', '').strip()
            if not value or len(value)>200 or len(terms(value))>16:
                raise PptutorError('INVALID_PLAN', '每个查询需要 1–200 字符、最多 16 个词。')
            folded = comparable(value)
            if folded in seen: continue
            seen.add(folded); queries.append({'query': value, 'rationale': str(q.get('rationale', ''))[:250]})
        if len(queries)>16:
            raise PptutorError('INVALID_PLAN', '含原词在内最多 16 条不同查询。')
        candidates, traces = {}, []
        if parallel and len(queries) > 1:
            # Independent read connections plus an autocommit observer: reject
            # mixed snapshots if a writer commits while the batch is in flight.
            from ..db import connect_readonly
            observer = connect_readonly(self.path)
            try:
                version = observer.execute('PRAGMA data_version').fetchone()[0]

                def one(query):
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise PptutorError('QUERY_TIMEOUT', '素材查询超时，请减少查询组')
                    with Library(self.path, self.roots, self.extensions, seconds=remaining) as reader:
                        return reader._search_one(query['query'], per_query)

                with ThreadPoolExecutor(max_workers=min(4, len(queries)), thread_name_prefix='hub-search') as pool:
                    results = list(pool.map(one, queries))
                if observer.execute('PRAGMA data_version').fetchone()[0] != version:
                    raise PptutorError('INDEX_CHANGED', '查询期间索引已更新，请重试本批查询')
            finally:
                observer.close()
        else:
            results = [self._search_one(q['query'], per_query) for q in queries]
        for q, (found, trace) in zip(queries, results):
            trace['rationale'] = q['rationale']; traces.append(trace)
            for rank, (row, kind) in enumerate(found, 1):
                key = (row['file_id'],row['page_no'])
                if key not in candidates:
                    snippet, cut = excerpt(row['raw_text'] or '', q['query'])
                    candidates[key] = {'ref': self.reference(row), 'id': f"{key[0]}:{key[1]}",
                        'snippet': snippet, 'snippet_truncated': cut, 'text_chars': len(row['raw_text'] or ''),
                        'retrieval_score': 0.0, 'matched_queries': [], 'duplicate_refs': [],
                        '_dedup': digest(comparable(row['raw_text'] or ''))}
                item = candidates[key]
                item['retrieval_score'] += (1.0 if kind=='body' else 0.25)/(60+rank)
                item['matched_queries'].append({'query': q['query'], 'kind': kind, 'rank': rank})
        ordered = sorted(candidates.values(), key=lambda x: (-x['retrieval_score'], x['id']))
        dedup, unique = {}, []
        for item in ordered:
            body_key = item.pop('_dedup')
            # Empty and very short text are not meaningful duplicate pages.
            if item['text_chars']>=40 and body_key in dedup:
                dedup[body_key]['duplicate_refs'].append(item['ref'])
                known = {(q['query'], q['kind']) for q in dedup[body_key]['matched_queries']}
                dedup[body_key]['matched_queries'].extend(q for q in item['matched_queries'] if (q['query'], q['kind']) not in known)
                continue
            dedup[body_key] = item; unique.append(item)
        return {'ok': True, 'version': VERSION, 'scope': self.scope(), 'intent': plan.get('intent', original),
                'queries': traces, 'candidates': diverse_candidates(unique, limit), 'unique_candidates': len(unique),
                'raw_candidates': len(candidates), 'duplicates_collapsed': len(candidates)-len(unique),
                'truncated': len(unique)>limit or any(t['fts_scan_truncated'] or t['body_return_truncated'] or t['filename_return_truncated'] for t in traces),
                'ranking_kind': 'fusion anchors plus body-query/document coverage; read queue only, host model must rerank',
                'coverage': 'searched extracted text only; no OCR, image inspection or complete-library absence claim'}

    def read(self, refs, neighbors=0):
        if not isinstance(refs, list) or not 1<=len(refs)<=40 or not 0<=neighbors<=2:
            raise PptutorError('INVALID_BUDGET', '读取 1–40 页，相邻页半径 0–2。')
        pred, params = self.filters()
        output, missing, seen = [], [], set()
        for ref in refs:
            if ref.get('index_id', self.index_id)!=self.index_id:
                raise PptutorError('WRONG_INDEX', '引用来自另一索引。')
            fid, page = int(ref['file_id']), int(ref['page'])
            if fid<1 or page<1:
                raise PptutorError('INVALID_REFERENCE', 'file_id 与 page 必须为正整数。')
            rows = list(self.conn.execute(f"SELECT f.id AS file_id,f.path,f.name,f.content_hash,f.indexed_at,f.size,f.mtime,pr.page_no,pr.raw_text FROM files f JOIN pages_raw pr ON pr.file_id=f.id WHERE {' AND '.join(pred)} AND f.id=? AND pr.page_no BETWEEN ? AND ? ORDER BY pr.page_no", params+[fid,max(1,page-neighbors),page+neighbors]))
            center = next((r for r in rows if r['page_no']==page), None)
            if center is None:
                missing.append({'file_id': fid, 'page': page, 'reason': 'not found in selected scope'}); continue
            if ref.get('path') and os.path.normcase(os.path.abspath(ref['path'])) != os.path.normcase(os.path.abspath(center['path'])):
                raise PptutorError('REFERENCE_CHANGED', '索引行号已指向另一文件，请重新检索。')
            if ref.get('page_hash') and ref['page_hash']!=digest(center['raw_text'] or ''):
                raise PptutorError('REFERENCE_CHANGED', '检索后索引页文字已变化，请重新检索。')
            if ref.get('content_hash') and ref['content_hash']!=(center['content_hash'] or ''):
                raise PptutorError('REFERENCE_CHANGED', '检索后文件索引版本已变化，请重新检索。')
            for row in rows:
                key = (row['file_id'],row['page_no'])
                if key in seen: continue
                seen.add(key)
                source = Path(row['path'])
                try:
                    stat = source.stat()
                    freshness = 'metadata_matches_index' if stat.st_size==row['size'] and abs(stat.st_mtime-row['mtime'])<1.0 else 'source_metadata_changed'
                except OSError:
                    freshness = 'source_unavailable'
                output.append({'id': f'{key[0]}:{key[1]}', 'ref': self.reference(row),
                    'text': row['raw_text'] or '', 'text_truncated': False, 'source_state': freshness,
                    'is_neighbor': row['page_no']!=page, 'source_channel': 'indexed_extracted_text'})
        return {'ok': True, 'version': VERSION, 'scope': self.scope(), 'pages': output,
                'missing': missing, 'truncated': False, 'visual_inspection': 'P2: not performed'}
