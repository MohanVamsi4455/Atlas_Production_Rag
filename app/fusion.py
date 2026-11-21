"""Merge two ranked result lists (meaning from Pinecone, keywords from Elasticsearch) into one.

Reciprocal Rank Fusion: each list gives a chunk 1 / (k + rank). Chunks found by both lists add up, so they rise to
the top. It uses only the RANKS, so the two score scales (cosine 0-1 and BM25 0-30) never have to be compared.
"""


def fuse_rrf(dense: list[dict], keyword: list[dict], top_k: int = 5, k: int = 60,
             dense_weight: float = 1.0, keyword_weight: float = 1.0) -> list[dict]:
    """Return up to top_k merged hits, best first.

    dense:   hits from Pinecone  ({'id','score','doc_id','title','chunk','page','text'}), already above MIN_RELEVANCE
    keyword: hits from Elasticsearch ({'id','bm25', same fields}), already above the BM25 cutoffs
    dense_weight / keyword_weight scale each list's say (1.0 each = equal). Each result has the usual fields plus 'score' (the fused value), 'dense_score', 'bm25_score' and 'via'.
    """
    merged: dict[str, dict] = {}
    for rank, h in enumerate(dense, 1):
        m = merged.setdefault(h['id'], {**h, 'dense_score': None, 'bm25_score': None, 'rrf': 0.0})
        m.update({kk: h.get(kk) for kk in ('doc_id', 'title', 'chunk', 'page', 'text')})
        m['dense_score'] = h['score']
        m['rrf'] += dense_weight / (k + rank)
    for rank, h in enumerate(keyword, 1):
        m = merged.setdefault(h['id'], {**{kk: h.get(kk) for kk in ('id', 'doc_id', 'title', 'chunk', 'page', 'text')},
                                        'dense_score': None, 'bm25_score': None, 'rrf': 0.0})
        for kk in ('doc_id', 'title', 'chunk', 'page', 'text'):
            if m.get(kk) in (None, ''):
                m[kk] = h.get(kk)
        m['bm25_score'] = h['bm25']
        m['rrf'] += keyword_weight / (k + rank)
    out = []
    for m in merged.values():
        m['score'] = m.pop('rrf')
        m['via'] = ('both' if m['dense_score'] is not None and m['bm25_score'] is not None
                    else 'dense' if m['dense_score'] is not None else 'keyword')
        out.append(m)
    out.sort(key=lambda m: (-m['score'], -(m['dense_score'] or 0.0)))
    return out[:top_k]
