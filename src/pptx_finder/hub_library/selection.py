"""Keep high-ranking anchors and query/document coverage in a bounded read queue."""
def diverse_candidates(ordered, limit):
    anchors = min(len(ordered), max(1, (limit + 2) // 3))
    selected, remaining = list(ordered[:anchors]), list(ordered[anchors:])
    documents, queries = set(), set()

    def cover(item):
        documents.add(item['ref']['file_id'])
        queries.update(q['query'] for q in item['matched_queries'] if q['kind'] == 'body')

    def gain(item):
        body_queries = {q['query'] for q in item['matched_queries'] if q['kind'] == 'body'}
        # Body evidence gets coverage priority; filename-only pages remain leads.
        return (int(bool(body_queries) and item['ref']['file_id'] not in documents),
                len(body_queries - queries), item['retrieval_score'])

    for item in selected:
        cover(item)
    while remaining and len(selected) < limit:
        best = max(remaining, key=gain)
        if gain(best)[:2] == (0, 0):
            selected.extend(remaining[:limit-len(selected)])
            break
        selected.append(best)
        remaining.remove(best)
        cover(best)
    return selected
