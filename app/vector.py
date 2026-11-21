"""Pinecone vector store: index creation, hybrid upsert, access-controlled search, delete.

Two modes, chosen by the index itself (no code change needed to switch):
  * dotproduct index -> HYBRID: every chunk record holds a dense vector (meaning, from Gemini) and a sparse vector
    (keywords, from Pinecone's sparse model). One query scores both.
  * cosine index     -> DENSE-ONLY: exactly the original behaviour.

Isolation: one Pinecone namespace per tenant, plus a metadata filter on allowed_groups,
so a user only ever retrieves chunks from their own tenant that their groups may read.
"""
import logging
import time
from functools import lru_cache

from pinecone import Pinecone, ServerlessSpec

from app.config import settings
from app.observability import trace

log = logging.getLogger('atlas.vector')
BATCH = 100


@lru_cache
def _pc() -> Pinecone:
    key = settings().pinecone_api_key
    if not key:
        raise RuntimeError('PINECONE_API_KEY is not set in .env')
    return Pinecone(api_key=key)


@lru_cache
def _index_info():
    """(index handle, metric). Creates the index first if it does not exist."""
    s = settings()
    pc = _pc()
    name = s.pinecone_index_name
    if not pc.has_index(name):
        metric = s.pinecone_metric.lower()
        if metric not in ('dotproduct', 'cosine'):
            raise ValueError(f'PINECONE_METRIC must be dotproduct or cosine, got {metric!r}')
        pc.create_index(
            name=name, dimension=s.embed_dim, metric=metric,
            spec=ServerlessSpec(cloud=s.pinecone_cloud, region=s.pinecone_region))
    deadline = time.time() + s.pinecone_ready_timeout
    while True:
        desc = pc.describe_index(name)
        if desc.status['ready']:
            break
        if time.time() > deadline:
            raise TimeoutError(f'Pinecone index {name!r} not ready after {s.pinecone_ready_timeout}s')
        time.sleep(3)
    metric = str(desc.metric).lower()
    vtype = str(getattr(desc, 'vector_type', 'dense') or 'dense').lower()
    if desc.dimension != s.embed_dim or metric not in ('cosine', 'dotproduct') or vtype != 'dense':
        raise RuntimeError(
            f'Index {name!r} is not a dense {s.embed_dim}-dimension cosine/dotproduct index '
            f'(dimension={desc.dimension}, metric={desc.metric}, type={desc.vector_type}). '
            'Set a different PINECONE_INDEX_NAME (it is created automatically) or delete the old index.')
    return pc.Index(host=desc.host), metric


def index():
    return _index_info()[0]


def is_hybrid() -> bool:
    """True when the index can hold sparse vectors next to the dense ones (dotproduct metric)."""
    return _index_info()[1] == 'dotproduct'


@trace('pinecone.upsert', kind='tool')
def upsert_chunks(tenant_id: str, doc_id: str, title: str, allowed_groups: list[str],
                  chunks: list[str], vectors: list[list[float]], version: int = 1,
                  owner_sub: str = '', pages: list[int] | None = None,
                  sparse: list[dict | None] | None = None) -> int:
    """Store one document's chunks. Ids are '<doc_id>:<version>:<n>'. Returns the number stored.

    sparse: one {'indices','values'} (or None) per chunk. Used only on a hybrid index; chunks without one
    are stored dense-only and still found by meaning.
    """
    if len(chunks) != len(vectors):
        raise ValueError('chunks and vectors must have the same length')
    if sparse is not None and len(sparse) != len(chunks):
        raise ValueError('sparse must have one entry per chunk')
    pages = pages or [1] * len(chunks)
    hybrid = is_hybrid() and sparse is not None
    records = []
    for n, (text, vec) in enumerate(zip(chunks, vectors)):
        meta = {'doc_id': doc_id, 'title': title, 'chunk': n, 'version': version, 'page': pages[n],
                'owner_sub': owner_sub, 'allowed_groups': list(allowed_groups), 'text': text}
        rec = {'id': f'{doc_id}:{version}:{n}', 'values': vec, 'metadata': meta}
        if hybrid and sparse[n]:
            rec['sparse_values'] = {'indices': list(sparse[n]['indices']), 'values': list(sparse[n]['values'])}
            meta['sparse_model'] = settings().sparse_model
        records.append(rec)
    idx = index()
    for i in range(0, len(records), BATCH):
        idx.upsert(vectors=records[i:i + BATCH], namespace=tenant_id)
    return len(records)


@trace('pinecone.search', kind='retriever')
def search(tenant_id: str, query_vector: list[float], groups: list[str], top_k: int = 5,
           owner_sub: str = '', sparse_query: dict | None = None, alpha: float | None = None) -> list[dict]:
    """Top matches this user may read (their groups, or documents they own), best first,
    dropping anything below MIN_RELEVANCE.

    On a hybrid index with a sparse_query, the dense part is scaled by alpha and the sparse part by (1 - alpha),
    so one query scores meaning and keywords together. Without a sparse_query (or on a cosine index) it is a plain
    dense search. Returns [{'id', 'score', 'doc_id', 'title', 'chunk', 'page', 'text'}].
    No groups and no owner -> no results.
    """
    allowed = []
    if groups:
        allowed.append({'allowed_groups': {'$in': list(groups)}})
    if owner_sub:
        allowed.append({'owner_sub': {'$eq': owner_sub}})
    if not allowed:
        return []
    args = dict(top_k=top_k, namespace=tenant_id, include_metadata=True, filter={'$or': allowed})
    if is_hybrid() and sparse_query and sparse_query.get('indices'):
        a = settings().hybrid_alpha if alpha is None else alpha
        args['vector'] = [v * a for v in query_vector]
        args['sparse_vector'] = {'indices': list(sparse_query['indices']),
                                 'values': [v * (1 - a) for v in sparse_query['values']]}
    else:
        args['vector'] = query_vector
    res = index().query(**args)
    floor = settings().min_relevance
    return [{'id': m['id'], 'score': m['score'],
             **{k: m['metadata'].get(k) for k in ('doc_id', 'title', 'chunk', 'page', 'text')}}
            for m in res['matches'] if m['score'] >= floor]


@trace('pinecone.delete', kind='tool')
def delete_document(tenant_id: str, doc_id: str) -> int:
    """Remove every chunk of a document (all versions). Returns how many were deleted."""
    idx = index()
    ids = [i for page in idx.list(prefix=f'{doc_id}:', namespace=tenant_id) for i in page]
    for i in range(0, len(ids), BATCH):
        idx.delete(ids=ids[i:i + BATCH], namespace=tenant_id)
    return len(ids)


def check_connection() -> str:
    """Raise if Pinecone is unreachable or the index is missing/misconfigured."""
    idx = index()
    stats = idx.describe_index_stats()
    mode = 'hybrid (dense + sparse)' if is_hybrid() else 'dense-only'
    return f'index {settings().pinecone_index_name!r}, {stats["dimension"]} dims, {stats["total_vector_count"]} vectors, {mode}'
