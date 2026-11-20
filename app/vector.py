"""Pinecone vector store: index creation, upsert, access-controlled search, delete.

Isolation: one Pinecone namespace per tenant, plus a metadata filter on allowed_groups,
so a user only ever retrieves chunks from their own tenant that their groups may read.
"""
import time
from functools import lru_cache

from pinecone import Pinecone, ServerlessSpec

from app.config import settings
from app.observability import trace

BATCH = 100


@lru_cache
def _pc() -> Pinecone:
    key = settings().pinecone_api_key
    if not key:
        raise RuntimeError('PINECONE_API_KEY is not set in .env')
    return Pinecone(api_key=key)


@lru_cache
def index():
    """Return the index handle, creating the index first if it does not exist."""
    s = settings()
    pc = _pc()
    if not pc.has_index(s.pinecone_index_name):
        pc.create_index(
            name=s.pinecone_index_name, dimension=s.embed_dim, metric='cosine',
            spec=ServerlessSpec(cloud=s.pinecone_cloud, region=s.pinecone_region))
    deadline = time.time() + s.pinecone_ready_timeout
    while True:
        desc = pc.describe_index(s.pinecone_index_name)
        if desc.status['ready']:
            break
        if time.time() > deadline:
            raise TimeoutError(f'Pinecone index {s.pinecone_index_name!r} not ready after {s.pinecone_ready_timeout}s')
        time.sleep(3)
    if desc.dimension != s.embed_dim or desc.metric != 'cosine':
        raise RuntimeError(
            f'Index {s.pinecone_index_name!r} is not a dense {s.embed_dim}-dim cosine index '
            f'(dimension={desc.dimension}, metric={desc.metric}, type={desc.vector_type}). '
            'Set a new PINECONE_INDEX_NAME in .env (it is created automatically) or delete the old index.')
    return pc.Index(host=desc.host)


@trace('pinecone.upsert', kind='tool')
def upsert_chunks(tenant_id: str, doc_id: str, title: str, allowed_groups: list[str],
                  chunks: list[str], vectors: list[list[float]], version: int = 1,
                  owner_sub: str = '', pages: list[int] | None = None) -> int:
    """Store one document's chunks. Ids are '<doc_id>:<version>:<n>'. Returns the number stored."""
    if len(chunks) != len(vectors):
        raise ValueError('chunks and vectors must have the same length')
    pages = pages or [1] * len(chunks)
    records = [{
        'id': f'{doc_id}:{version}:{n}',
        'values': vec,
        'metadata': {'doc_id': doc_id, 'title': title, 'chunk': n, 'version': version, 'page': pages[n],
                     'owner_sub': owner_sub, 'allowed_groups': list(allowed_groups), 'text': text},
    } for n, (text, vec) in enumerate(zip(chunks, vectors))]
    idx = index()
    for i in range(0, len(records), BATCH):
        idx.upsert(vectors=records[i:i + BATCH], namespace=tenant_id)
    return len(records)


@trace('pinecone.search', kind='retriever')
def search(tenant_id: str, query_vector: list[float], groups: list[str], top_k: int = 5,
           owner_sub: str = '') -> list[dict]:
    """Top matches this user may read (their groups, or documents they own), best first,
    dropping anything below MIN_RELEVANCE.

    Returns [{'id', 'score', 'doc_id', 'title', 'chunk', 'page', 'text'}]. No groups and no owner -> no results.
    """
    allowed = []
    if groups:
        allowed.append({'allowed_groups': {'$in': list(groups)}})
    if owner_sub:
        allowed.append({'owner_sub': {'$eq': owner_sub}})
    if not allowed:
        return []
    res = index().query(
        vector=query_vector, top_k=top_k, namespace=tenant_id, include_metadata=True,
        filter={'$or': allowed})
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
    return f'index {settings().pinecone_index_name!r}, {stats["dimension"]} dims, {stats["total_vector_count"]} vectors'
