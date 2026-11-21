"""Offline tests for Elasticsearch keyword search and rank fusion. No network, no keys.

A simulated Elasticsearch (httpx.MockTransport) records every request, so these tests check exactly what would be
sent to a real cluster.

Run:  python test/test_keyword.py        (or: python -m pytest test/test_keyword.py)
"""
import json
import pathlib
import sys
import types
import warnings

warnings.filterwarnings('ignore')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import fusion, keyword  # noqa: E402


_REAL_HTTP = keyword._http     # other tests swap this out; the header test needs the real one


def cfg(**over):
    base = dict(elastic_url='https://es.test', elastic_api_key='SECRET-KEY-123', elastic_index='atlas-chunks',
                elastic_min_score=1.0, elastic_min_ratio=0.3, keyword_backend='auto')
    base.update(over)
    return types.SimpleNamespace(**base)


class FakeES:
    """Records requests and answers from a routing table: {(METHOD, path-prefix): (status, json)}."""

    def __init__(self, routes=None):
        self.requests, self.routes = [], routes or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode('utf-8') if request.content else ''
        self.requests.append({'method': request.method, 'path': request.url.path, 'query': request.url.query.decode(),
                              'body': body, 'headers': dict(request.headers)})
        for (method, prefix), (status, payload) in self.routes.items():
            exact = request.url.path == prefix if prefix == '/' else request.url.path.startswith(prefix)
            if request.method == method and exact:
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={'error': {'type': 'index_not_found_exception', 'reason': 'no such index'}})

    def bodies(self, method, prefix):
        return [json.loads(r['body']) for r in self.requests if r['method'] == method and r['path'].startswith(prefix) and r['body']]


def use_es(routes=None, **settings_over):
    fake = FakeES(routes)
    keyword.settings = lambda: cfg(**settings_over)
    client = httpx.Client(base_url='https://es.test', transport=httpx.MockTransport(fake.handler),
                          headers={'Authorization': 'ApiKey SECRET-KEY-123'})
    keyword._http = lambda: client
    keyword._index_ready = False
    return fake


def es_hit(id_, score, text='some text', page=1):
    return {'_id': id_, '_score': score, '_source': {'doc_id': 'd', 'title': 'T', 'chunk': 0, 'page': page, 'text': text}}


# ---------------- configuration ----------------
def test_enabled_needs_url_and_key_and_respects_the_switch():
    keyword.settings = lambda: cfg()
    assert keyword.enabled()
    keyword.settings = lambda: cfg(elastic_url='')
    assert not keyword.enabled()
    keyword.settings = lambda: cfg(elastic_api_key='')
    assert not keyword.enabled()
    for off in ('none', 'off', 'pinecone', 'NONE'):
        keyword.settings = lambda off=off: cfg(keyword_backend=off)
        assert not keyword.enabled()
    keyword.settings = lambda: cfg(keyword_backend='elasticsearch')
    assert keyword.enabled()


def test_a_self_hosted_http_elasticsearch_needs_no_key_but_elastic_cloud_does():
    for url in ('http://localhost:9200', 'http://127.0.0.1:9200', 'http://host.docker.internal:9200', 'http://atlas-elasticsearch:9200'):
        keyword.settings = lambda url=url: cfg(elastic_url=url, elastic_api_key='')
        assert keyword.enabled(), url
    keyword.settings = lambda: cfg(elastic_url='https://abc.es.ap-south-1.aws.elastic.cloud', elastic_api_key='')
    assert not keyword.enabled()                              # Elastic Cloud without a key would only give 401 errors
    keyword.settings = lambda: cfg(elastic_url='http://localhost:9200', keyword_backend='none')
    assert not keyword.enabled()                              # the switch still turns it off


def test_no_authorization_header_is_sent_when_there_is_no_key():
    keyword._http = _REAL_HTTP
    keyword._http.cache_clear()
    keyword.settings = lambda: cfg(elastic_url='http://localhost:9200', elastic_api_key='')
    client = keyword._http()
    assert 'authorization' not in {k.lower() for k in client.headers}
    keyword._http.cache_clear()
    keyword.settings = lambda: cfg()
    assert keyword._http().headers['authorization'] == 'ApiKey SECRET-KEY-123'
    keyword._http.cache_clear()



def test_the_api_key_is_sent_as_an_authorization_header_only():
    fake = use_es({('GET', '/'): (200, {'version': {'number': '9.1.0', 'build_flavor': 'serverless'}}),
                   ('GET', '/atlas-chunks/_count'): (200, {'count': 7})})
    msg = keyword.check_connection()
    assert fake.requests[0]['headers']['authorization'] == 'ApiKey SECRET-KEY-123'
    assert '9.1.0' in msg and '7 chunks' in msg and 'SECRET' not in msg


# ---------------- index creation ----------------
def test_index_is_created_once_with_bm25_text_fields():
    fake = use_es({('HEAD', '/atlas-chunks'): (404, {}), ('PUT', '/atlas-chunks'): (200, {'acknowledged': True})})
    keyword.ensure_index()
    keyword.ensure_index()                                   # second call: no more requests
    puts = fake.bodies('PUT', '/atlas-chunks')
    assert len(puts) == 1 and len([r for r in fake.requests if r['method'] == 'HEAD']) == 1
    props = puts[0]['mappings']['properties']
    assert props['text']['analyzer'] == 'english' and props['text']['fields']['plain']['analyzer'] == 'standard'
    assert props['tenant_id']['type'] == 'keyword' and props['allowed_groups']['type'] == 'keyword'
    assert 'settings' not in puts[0]                          # no shard settings: works on serverless and hosted


def test_existing_index_is_left_alone():
    fake = use_es({('HEAD', '/atlas-chunks'): (200, {})})
    keyword.ensure_index()
    assert [r['method'] for r in fake.requests] == ['HEAD']


# ---------------- indexing ----------------
def test_bulk_indexing_uses_the_pinecone_ids_and_waits_for_visibility():
    fake = use_es({('HEAD', '/atlas-chunks'): (200, {}), ('POST', '/_bulk'): (200, {'errors': False, 'items': []})})
    n = keyword.upsert_chunks('tenantA', 'doc7', 'Handbook', ['staff'], ['first chunk', 'second chunk'],
                              version=2, owner_sub='me', pages=[1, 4])
    assert n == 2
    req = next(r for r in fake.requests if r['path'] == '/_bulk')
    assert 'refresh=wait_for' in req['query'] and req['headers']['content-type'] == 'application/x-ndjson'
    lines = [json.loads(l) for l in req['body'].strip().split('\n')]
    assert lines[0] == {'index': {'_index': 'atlas-chunks', '_id': 'doc7:2:0'}}
    assert lines[1] == {'tenant_id': 'tenantA', 'doc_id': 'doc7', 'title': 'Handbook', 'text': 'first chunk', 'page': 1,
                        'chunk': 0, 'version': 2, 'allowed_groups': ['staff'], 'owner_sub': 'me'}
    assert lines[2]['index']['_id'] == 'doc7:2:1' and lines[3]['page'] == 4
    assert req['body'].endswith('\n')                          # the bulk format requires a final newline


def test_bulk_is_split_into_batches():
    fake = use_es({('HEAD', '/atlas-chunks'): (200, {}), ('POST', '/_bulk'): (200, {'errors': False, 'items': []})})
    keyword.upsert_chunks('t', 'd', 'T', ['g'], [f'c{i}' for i in range(keyword.BULK_BATCH + 10)])
    assert len([r for r in fake.requests if r['path'] == '/_bulk']) == 2


def test_bulk_item_errors_are_raised_with_the_reason():
    use_es({('HEAD', '/atlas-chunks'): (200, {}),
            ('POST', '/_bulk'): (200, {'errors': True, 'items': [{'index': {'error': {'type': 'x', 'reason': 'mapper_parsing_exception'}}}]})})
    try:
        keyword.upsert_chunks('t', 'd', 'T', ['g'], ['c'])
        raise AssertionError('should have raised')
    except RuntimeError as e:
        assert 'mapper_parsing_exception' in str(e)


def test_indexing_nothing_makes_no_request():
    fake = use_es()
    assert keyword.index_records([]) == 0 and fake.requests == []


# ---------------- search ----------------
def search_body(fake):
    return fake.bodies('POST', '/atlas-chunks/_search')[0]


def test_search_filters_by_tenant_and_permissions():
    fake = use_es({('POST', '/atlas-chunks/_search'): (200, {'hits': {'hits': []}})})
    keyword.search('tenantA', 'annual leave', ['staff', 'hr'], owner_sub='me', top_k=7)
    body = search_body(fake)
    assert body['size'] == 7
    flt = body['query']['bool']['filter']
    assert {'term': {'tenant_id': 'tenantA'}} in flt
    acl = next(f for f in flt if 'bool' in f)['bool']
    assert acl['minimum_should_match'] == 1
    assert {'terms': {'allowed_groups': ['staff', 'hr']}} in acl['should'] and {'term': {'owner_sub': 'me'}} in acl['should']
    mm = body['query']['bool']['must'][0]['multi_match']
    assert mm['query'] == 'annual leave' and mm['type'] == 'most_fields' and 'text.plain^0.7' in mm['fields']


def test_search_with_no_groups_and_no_owner_or_blank_query_asks_nothing():
    fake = use_es()
    assert keyword.search('t', 'x', [], owner_sub='') == []
    assert keyword.search('t', '   ', ['g']) == []
    assert fake.requests == []                                 # nobody may read anything: do not even call Elasticsearch


def test_weak_keyword_matches_are_dropped():
    hits = [es_hit('a', 12.0), es_hit('b', 5.0), es_hit('c', 3.0), es_hit('d', 0.8)]
    use_es({('POST', '/atlas-chunks/_search'): (200, {'hits': {'hits': hits}})}, elastic_min_score=1.0, elastic_min_ratio=0.3)
    out = keyword.search('t', 'q', ['g'])
    assert [h['id'] for h in out] == ['a', 'b']                # 3.0 < 0.3 x 12 = 3.6; 0.8 < 1.0
    assert out[0]['bm25'] == 12.0 and out[0]['title'] == 'T' and out[0]['page'] == 1


def test_search_returns_nothing_when_nothing_matches():
    use_es({('POST', '/atlas-chunks/_search'): (200, {'hits': {'hits': []}})})
    assert keyword.search('t', 'q', ['g']) == []


# ---------------- delete and count ----------------
def test_delete_removes_one_document_of_one_tenant():
    fake = use_es({('POST', '/atlas-chunks/_delete_by_query'): (200, {'deleted': 3})})
    assert keyword.delete_document('tenantA', 'doc7') == 3
    body = fake.bodies('POST', '/atlas-chunks/_delete_by_query')[0]
    assert body == {'query': {'bool': {'filter': [{'term': {'tenant_id': 'tenantA'}}, {'term': {'doc_id': 'doc7'}}]}}}
    assert 'refresh=true' in fake.requests[0]['query']


def test_delete_when_the_index_does_not_exist_yet_is_a_no_op():
    use_es()                                                    # every route answers 404
    assert keyword.delete_document('t', 'd') == 0
    assert keyword.count() == 0


def test_count_can_be_limited_to_one_tenant():
    fake = use_es({('POST', '/atlas-chunks/_count'): (200, {'count': 5})})
    assert keyword.count('tenantA') == 5
    assert fake.bodies('POST', '/atlas-chunks/_count')[0] == {'query': {'term': {'tenant_id': 'tenantA'}}}


# ---------------- errors ----------------
def test_errors_explain_the_problem_without_leaking_the_key():
    for status, words in ((401, 'API key is wrong'), (403, 'lacks permission')):
        use_es({('GET', '/'): (status, {'error': {'type': 'security_exception', 'reason': 'unable to authenticate'}})})
        try:
            keyword.check_connection()
            raise AssertionError('should have raised')
        except RuntimeError as e:
            assert words in str(e) and 'unable to authenticate' in str(e) and 'SECRET-KEY' not in str(e)


# ---------------- rank fusion ----------------
def dh(id_, score, text='d'):
    return {'id': id_, 'score': score, 'doc_id': 'doc', 'title': 'T', 'chunk': 0, 'page': 1, 'text': text}


def kh(id_, bm25, text='k'):
    return {'id': id_, 'bm25': bm25, 'doc_id': 'doc', 'title': 'T', 'chunk': 0, 'page': 2, 'text': text}


def test_fusion_puts_chunks_found_by_both_lists_first():
    out = fusion.fuse_rrf([dh('a', .9), dh('b', .8), dh('c', .7)], [kh('c', 20), kh('d', 9), kh('a', 5)], top_k=4)
    ids = [h['id'] for h in out]
    assert ids[:2] == ['a', 'c'] and set(ids) == {'a', 'b', 'c', 'd'}
    via = {h['id']: h['via'] for h in out}
    assert via == {'a': 'both', 'c': 'both', 'b': 'dense', 'd': 'keyword'}


def test_fusion_scores_use_ranks_not_raw_scores():
    out = fusion.fuse_rrf([dh('a', .99)], [kh('b', 1000.0)], top_k=2, k=60)
    assert abs(out[0]['score'] - out[1]['score']) < 1e-12      # both are rank 1 in their own list
    assert out[0]['id'] == 'a'                                  # tie broken by the dense score


def test_fusion_weights_let_one_list_count_more():
    dense, kw = [dh('d1', .9)], [kh('k1', 20)]               # each list has a different favourite
    assert fusion.fuse_rrf(dense, kw, top_k=2)[0]['id'] == 'd1'                      # equal say: tie goes to the dense score
    assert fusion.fuse_rrf(dense, kw, top_k=2, keyword_weight=1.3)[0]['id'] == 'k1'  # keywords count more: their favourite wins
    assert fusion.fuse_rrf(dense, kw, top_k=2, dense_weight=1.3)[0]['id'] == 'd1'
    both = fusion.fuse_rrf([dh('d1', .9), dh('x', .8)], [kh('k1', 20), kh('x', 9)], top_k=3)
    assert both[0]['id'] == 'x'                                                      # found by both lists still wins


def test_the_configured_weights_reach_the_merge():
    from app import graph
    seen = {}
    graph.keyword = types.SimpleNamespace(enabled=lambda: True, search=lambda *a, **k: [kh('b', 9.0)])
    graph.embed = lambda texts: [[1.0]]
    graph.search = lambda *a, **k: [dh('a', .8)]
    graph.settings = lambda: types.SimpleNamespace(rrf_dense_weight=0.5, rrf_keyword_weight=2.0)
    real = graph.fuse_rrf
    graph.fuse_rrf = lambda d, k, **kw: seen.update(kw) or real(d, k, **kw)
    graph.retrieve({'principal': principal(), 'question': 'q', 'history': []})
    graph.fuse_rrf = real
    assert seen == {'top_k': 5, 'dense_weight': 0.5, 'keyword_weight': 2.0}


def test_fusion_keeps_both_raw_scores_and_the_dense_text():
    out = fusion.fuse_rrf([dh('a', .5, 'dense text')], [kh('a', 7.0, 'keyword text')])
    h = out[0]
    assert h['dense_score'] == .5 and h['bm25_score'] == 7.0 and h['text'] == 'dense text' and h['page'] == 1


def test_fusion_keyword_only_hit_keeps_its_own_fields():
    out = fusion.fuse_rrf([], [kh('x', 4.0, 'only keywords')])
    assert out[0]['id'] == 'x' and out[0]['text'] == 'only keywords' and out[0]['via'] == 'keyword' and out[0]['dense_score'] is None


def test_fusion_respects_top_k_and_handles_empty_lists():
    out = fusion.fuse_rrf([dh(str(i), 1 - i / 100) for i in range(10)], [kh(str(i + 5), 10 - i) for i in range(10)], top_k=5)
    assert len(out) == 5
    assert fusion.fuse_rrf([], []) == []


# ---------------- retrieval with Elasticsearch ----------------
def principal():
    return types.SimpleNamespace(tenant='t', groups=('g',), subject='me')


def patch_graph(dense, kw, kw_fails=False):
    from app import graph
    calls = {}
    graph.keyword = types.SimpleNamespace(enabled=lambda: True, search=lambda *a, **k: calls.setdefault('kw', k) and (_ for _ in ()).throw(RuntimeError('es down')) if kw_fails else (calls.update(kw=k) or kw))
    graph.embed = lambda texts: [[1.0]]
    graph.search = lambda *a, **k: calls.update(dense=k) or dense
    graph.settings = lambda: types.SimpleNamespace(rrf_dense_weight=1.0, rrf_keyword_weight=1.0)   # equal say, whatever .env says
    return graph, calls


def test_retrieval_merges_pinecone_and_elasticsearch():
    graph, calls = patch_graph([dh('a', .8), dh('b', .6)], [kh('b', 9.0), kh('c', 4.0)])
    out = graph.retrieve({'principal': principal(), 'question': 'annual leave', 'history': []})
    assert [p['id'] for p in out['passages']][:2] == ['b', 'a']      # found by both -> first
    assert calls['dense']['top_k'] == 10 and calls['kw']['top_k'] == 10
    assert calls['kw']['owner_sub'] == 'me'
    assert {s['index'] for s in out['sources']} == {1, 2, 3}


def test_retrieval_still_answers_when_elasticsearch_is_down():
    graph, calls = patch_graph([dh('a', .8)], [], kw_fails=True)
    out = graph.retrieve({'principal': principal(), 'question': 'annual leave', 'history': []})
    assert [p['id'] for p in out['passages']] == ['a']                # meaning-only, not an error


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  ok    {name}')
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'  FAIL  {name}: {type(e).__name__}: {e}')
    print(f'{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)
