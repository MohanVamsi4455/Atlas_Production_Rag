"""Keyword search (BM25) in Elasticsearch, over its REST API.

Why plain REST (httpx) instead of the official client: no extra dependency, and no version mismatch between a
client library and your Elastic Cloud version (hosted deployment or serverless project).

Every chunk is stored as one document, with the SAME id as its Pinecone vector ("<doc_id>:<version>:<n>"),
so the two result lists can be merged by id. Tenant isolation and document permissions are filters in every query.
"""
import json
import logging
from functools import lru_cache
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.observability import trace

log = logging.getLogger('atlas.keyword')
BULK_BATCH = 200

# `text` is analysed twice: with the English analyzer (stemming: "leaves" matches "leave") and with the standard
# analyzer (exact words, so codes like ZX-4471 match as written). BM25 is Elasticsearch's default scoring.
MAPPING = {'mappings': {'properties': {
    'tenant_id': {'type': 'keyword'},
    'doc_id': {'type': 'keyword'},
    'owner_sub': {'type': 'keyword'},
    'allowed_groups': {'type': 'keyword'},
    'title': {'type': 'text', 'analyzer': 'english'},
    'text': {'type': 'text', 'analyzer': 'english', 'fields': {'plain': {'type': 'text', 'analyzer': 'standard'}}},
    'page': {'type': 'integer'},
    'chunk': {'type': 'integer'},
    'version': {'type': 'integer'},
}}}


def _needs_key(url: str) -> bool:
    """Elastic Cloud (https) always needs an API key. A self-hosted Elasticsearch started without security
    (http://localhost:9200 on your computer, http://atlas-elasticsearch:9200 next to the app on the server) does not."""
    return urlparse(url).scheme.lower() != 'http'


def enabled() -> bool:
    """True when Elasticsearch is configured and not switched off with KEYWORD_BACKEND."""
    s = settings()
    if s.keyword_backend.lower() in ('none', 'off', 'pinecone'):
        return False
    return bool(s.elastic_url and (s.elastic_api_key or not _needs_key(s.elastic_url)))


@lru_cache
def _http() -> httpx.Client:
    s = settings()
    headers = {'Authorization': f'ApiKey {s.elastic_api_key}'} if s.elastic_api_key else {}
    return httpx.Client(base_url=s.elastic_url.rstrip('/'), timeout=15, headers=headers)


def _request(method: str, path: str, *, ok=(200, 201), **kw) -> dict:
    """One call to Elasticsearch. Raises RuntimeError with Elasticsearch's own reason (never the API key)."""
    r = _http().request(method, path, **kw)
    if r.status_code in ok:
        return r.json() if r.content else {}
    try:
        err = r.json().get('error', {})
        reason = err.get('reason') or err.get('type') or r.text[:200] if isinstance(err, dict) else str(err)[:200]
    except ValueError:
        reason = r.text[:200]
    hint = {401: ' (the API key is wrong or expired)', 403: ' (the API key lacks permission for this index)',
            404: ' (index or endpoint not found)'}.get(r.status_code, '')
    raise RuntimeError(f'Elasticsearch {method} {path.split("?")[0]} -> {r.status_code}: {reason}{hint}')


_index_ready = False


def ensure_index() -> None:
    """Create the index with the right mapping if it does not exist yet (safe to call repeatedly)."""
    global _index_ready
    if _index_ready:
        return
    name = settings().elastic_index
    head = _http().head(f'/{name}')
    if head.status_code == 404:
        r = _http().put(f'/{name}', json=MAPPING)
        if r.status_code not in (200, 201) and 'resource_already_exists' not in r.text:
            _request('PUT', f'/{name}', ok=(), json=MAPPING)      # raises with the reason
    elif head.status_code not in (200,):
        _request('HEAD', f'/{name}', ok=())                         # raises (401, 403 ...)
    _index_ready = True


def index_records(records: list[dict]) -> int:
    """Bulk-index [{'id', 'tenant_id', 'doc_id', 'title', 'text', 'page', 'chunk', 'version', 'allowed_groups',
    'owner_sub'}]. Documents become searchable before the call returns. Returns how many were indexed."""
    if not records:
        return 0
    ensure_index()
    name = settings().elastic_index
    done = 0
    for i in range(0, len(records), BULK_BATCH):
        lines = []
        for rec in records[i:i + BULK_BATCH]:
            doc = {k: v for k, v in rec.items() if k != 'id'}
            lines.append(json.dumps({'index': {'_index': name, '_id': rec['id']}}))
            lines.append(json.dumps(doc))
        body = '\n'.join(lines) + '\n'
        res = _request('POST', '/_bulk?refresh=wait_for', content=body.encode('utf-8'),
                       headers={'Content-Type': 'application/x-ndjson'})
        if res.get('errors'):
            bad = next((it['index']['error'] for it in res.get('items', []) if it.get('index', {}).get('error')), {})
            raise RuntimeError(f'Elasticsearch bulk indexing failed: {bad.get("reason") or bad.get("type") or "unknown"}')
        done += len(lines) // 2
    return done


@trace('elasticsearch.index', kind='tool')
def upsert_chunks(tenant_id: str, doc_id: str, title: str, allowed_groups: list[str], chunks: list[str],
                  version: int = 1, owner_sub: str = '', pages: list[int] | None = None) -> int:
    """Index one document's chunks under the same ids as their Pinecone vectors."""
    pages = pages or [1] * len(chunks)
    return index_records([{
        'id': f'{doc_id}:{version}:{n}', 'tenant_id': tenant_id, 'doc_id': doc_id, 'title': title, 'text': text,
        'page': pages[n], 'chunk': n, 'version': version, 'allowed_groups': list(allowed_groups), 'owner_sub': owner_sub,
    } for n, text in enumerate(chunks)])


@trace('elasticsearch.search', kind='retriever')
def search(tenant_id: str, query: str, groups: list[str], owner_sub: str = '', top_k: int = 10) -> list[dict]:
    """BM25 matches this user may read, best first.

    Weak matches are dropped: a hit must score at least ELASTIC_MIN_SCORE and at least ELASTIC_MIN_RATIO of the best hit.
    Returns [{'id', 'bm25', 'doc_id', 'title', 'chunk', 'page', 'text'}]. No groups and no owner -> no results.
    """
    allowed = []
    if groups:
        allowed.append({'terms': {'allowed_groups': list(groups)}})
    if owner_sub:
        allowed.append({'term': {'owner_sub': owner_sub}})
    if not allowed or not (query or '').strip():
        return []
    s = settings()
    body = {
        'size': top_k,
        '_source': ['doc_id', 'title', 'chunk', 'page', 'text'],
        'query': {'bool': {
            'must': [{'multi_match': {'query': query, 'type': 'most_fields',
                                      'fields': ['text', 'text.plain^0.7', 'title^0.5']}}],
            'filter': [{'term': {'tenant_id': tenant_id}}, {'bool': {'should': allowed, 'minimum_should_match': 1}}],
        }},
    }
    res = _request('POST', f'/{s.elastic_index}/_search', json=body)
    hits = [{'id': h['_id'], 'bm25': h['_score'], **{k: h['_source'].get(k) for k in ('doc_id', 'title', 'chunk', 'page', 'text')}}
            for h in res.get('hits', {}).get('hits', [])]
    if not hits:
        return []
    cutoff = max(s.elastic_min_score, s.elastic_min_ratio * hits[0]['bm25'])
    return [h for h in hits if h['bm25'] >= cutoff]


@trace('elasticsearch.delete', kind='tool')
def delete_document(tenant_id: str, doc_id: str) -> int:
    """Remove every chunk of a document. Returns how many were deleted (0 if the index does not exist yet)."""
    name = settings().elastic_index
    body = {'query': {'bool': {'filter': [{'term': {'tenant_id': tenant_id}}, {'term': {'doc_id': doc_id}}]}}}
    r = _http().post(f'/{name}/_delete_by_query?refresh=true&conflicts=proceed', json=body)
    if r.status_code == 404:
        return 0
    if r.status_code != 200:
        _request('POST', f'/{name}/_delete_by_query', ok=(), json=body)
    return int(r.json().get('deleted', 0))


def count(tenant_id: str | None = None) -> int:
    name = settings().elastic_index
    body = {'query': {'term': {'tenant_id': tenant_id}}} if tenant_id else None
    r = _http().post(f'/{name}/_count', json=body) if body else _http().get(f'/{name}/_count')
    if r.status_code == 404:
        return 0
    if r.status_code != 200:
        _request('GET', f'/{name}/_count', ok=())
    return int(r.json().get('count', 0))


def check_connection() -> str:
    """Raise if Elasticsearch is unreachable or the key is wrong."""
    info = _request('GET', '/')
    version = (info.get('version') or {}).get('number', '?')
    flavor = (info.get('version') or {}).get('build_flavor', '')
    return f'Elasticsearch {version}{" " + flavor if flavor else ""}, index {settings().elastic_index!r}: {count()} chunks'
