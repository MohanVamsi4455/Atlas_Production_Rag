"""Offline tests for hybrid (dense + sparse) search. No network, no keys.

Run:  python test/test_hybrid.py        (or: python -m pytest test/test_hybrid.py)
"""
import math
import pathlib
import sys
import types
import warnings

warnings.filterwarnings('ignore')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import llm, vector  # noqa: E402
from app.config import settings  # noqa: E402


_REAL_INDEX_INFO = vector._index_info     # other tests swap this out; the detection tests need the real one


class FakeIndex:
    def __init__(self, matches=None):
        self.upserts, self.queries, self.matches = [], [], matches or []

    def upsert(self, vectors, namespace):
        self.upserts.append((namespace, vectors))

    def query(self, **kw):
        self.queries.append(kw)
        return {'matches': self.matches}


def use_index(hybrid: bool, matches=None):
    """Point the vector module at a fake index of the chosen kind."""
    fake = FakeIndex(matches)
    vector._index_info = lambda: (fake, 'dotproduct' if hybrid else 'cosine')
    return fake


def match(score, text='t'):
    return {'id': 'd:1:0', 'score': score, 'metadata': {'doc_id': 'd', 'title': 'T', 'chunk': 0, 'page': 1, 'text': text}}


def norm(v):
    return math.sqrt(sum(x * x for x in v))


# ---------------- normalisation ----------------
def test_dense_vectors_are_normalised():
    v = llm._normalize([3.0, 4.0])
    assert abs(norm(v) - 1) < 1e-9 and v == [0.6, 0.8]
    assert llm._normalize([0.0, 0.0]) == [0.0, 0.0]          # a zero vector is left alone, no division by zero


def test_sparse_vectors_are_normalised():
    s = llm._sparse_norm([7, 9], [3.0, 4.0])
    assert s['indices'] == [7, 9] and abs(norm(s['values']) - 1) < 1e-5
    assert llm._sparse_norm([], []) is None                   # a text with no usable words
    assert llm._sparse_norm([1], [0.0]) is None


# ---------------- sparse_embed ----------------
class FakeInference:
    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def embed(self, model, inputs, parameters):
        self.calls.append((model, list(inputs), parameters))
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError('boom')
        return types.SimpleNamespace(data=[{'sparse_indices': [i + 1], 'sparse_values': [2.0]} for i, _ in enumerate(inputs)])


def use_inference(fail_times=0):
    inf = FakeInference(fail_times)
    llm._pinecone = lambda: types.SimpleNamespace(inference=inf)
    llm.time.sleep = lambda s: None
    return inf


def test_sparse_embed_batches_and_passes_input_type():
    inf = use_inference()
    texts = [f'chunk {i}' for i in range(llm.SPARSE_BATCH + 5)]
    out = llm.sparse_embed(texts, 'passage')
    assert len(out) == len(texts) and len(inf.calls) == 2    # split into two requests
    model, batch, params = inf.calls[0]
    assert model == settings().sparse_model and params['input_type'] == 'passage'
    assert all(abs(norm(o['values']) - 1) < 1e-5 for o in out)
    llm.sparse_embed(['question'], 'query')
    assert inf.calls[-1][2]['input_type'] == 'query'


def test_sparse_embed_retries_once_then_raises():
    use_inference(fail_times=1)
    assert llm.sparse_embed(['x'])[0] is not None             # first call fails, retry works
    use_inference(fail_times=2)
    try:
        llm.sparse_embed(['x'])
        raise AssertionError('should have raised')
    except RuntimeError:
        pass


def test_blank_text_is_not_sent_empty():
    inf = use_inference()
    llm.sparse_embed(['', '   ', 'real'])
    assert inf.calls[0][1] == [' ', ' ', 'real']


# ---------------- upsert ----------------
def test_upsert_stores_both_vectors_on_a_hybrid_index():
    fake = use_index(hybrid=True)
    sp = [{'indices': [5, 6], 'values': [0.6, 0.8]}, None]
    n = vector.upsert_chunks('t', 'doc', 'Title', ['g'], ['a', 'b'], [[1.0], [1.0]], sparse=sp)
    recs = fake.upserts[0][1]
    assert n == 2 and fake.upserts[0][0] == 't'
    assert recs[0]['sparse_values'] == {'indices': [5, 6], 'values': [0.6, 0.8]}
    assert recs[0]['metadata']['sparse_model'] == settings().sparse_model
    assert 'sparse_values' not in recs[1] and 'sparse_model' not in recs[1]['metadata']   # no keywords -> dense-only


def test_upsert_ignores_sparse_on_a_cosine_index():
    fake = use_index(hybrid=False)
    vector.upsert_chunks('t', 'doc', 'T', ['g'], ['a'], [[1.0]], sparse=[{'indices': [1], 'values': [1.0]}])
    assert 'sparse_values' not in fake.upserts[0][1][0]       # a cosine index would reject it


def test_upsert_without_sparse_is_unchanged():
    fake = use_index(hybrid=True)
    vector.upsert_chunks('t', 'doc', 'T', ['g'], ['a'], [[1.0]])
    assert 'sparse_values' not in fake.upserts[0][1][0]


def test_upsert_checks_lengths():
    use_index(hybrid=True)
    for bad in (dict(vectors=[[1.0]], sparse=None), dict(vectors=[[1.0], [1.0]], sparse=[None])):
        try:
            vector.upsert_chunks('t', 'd', 'T', ['g'], ['a', 'b'], **bad)
            raise AssertionError('should have raised')
        except ValueError:
            pass


# ---------------- search ----------------
def test_hybrid_search_scales_each_side_by_alpha():
    fake = use_index(hybrid=True, matches=[match(0.9)])
    vector.search('t', [1.0, 0.0], ['g'], sparse_query={'indices': [3], 'values': [1.0]}, alpha=0.7)
    q = fake.queries[0]
    assert q['vector'] == [0.7, 0.0]
    assert q['sparse_vector']['indices'] == [3] and abs(q['sparse_vector']['values'][0] - 0.3) < 1e-9


def test_alpha_comes_from_settings_by_default():
    fake = use_index(hybrid=True, matches=[match(0.9)])
    vector.search('t', [1.0], ['g'], sparse_query={'indices': [3], 'values': [1.0]})
    assert abs(fake.queries[0]['vector'][0] - settings().hybrid_alpha) < 1e-9


def test_search_falls_back_to_dense_only_without_a_sparse_query():
    fake = use_index(hybrid=True, matches=[match(0.9)])
    vector.search('t', [1.0, 0.0], ['g'])
    assert 'sparse_vector' not in fake.queries[0] and fake.queries[0]['vector'] == [1.0, 0.0]
    vector.search('t', [1.0], ['g'], sparse_query={'indices': [], 'values': []})      # empty keyword vector
    assert 'sparse_vector' not in fake.queries[1]


def test_cosine_index_never_gets_a_sparse_vector():
    fake = use_index(hybrid=False, matches=[match(0.9)])
    vector.search('t', [1.0], ['g'], sparse_query={'indices': [3], 'values': [1.0]})
    assert 'sparse_vector' not in fake.queries[0] and fake.queries[0]['vector'] == [1.0]


def test_search_keeps_the_access_filter_and_the_relevance_floor():
    fake = use_index(hybrid=True, matches=[match(0.9, 'keep'), match(0.01, 'drop')])
    hits = vector.search('t', [1.0], ['staff'], owner_sub='me')
    assert [h['text'] for h in hits] == ['keep']              # below MIN_RELEVANCE is dropped
    assert fake.queries[0]['filter'] == {'$or': [{'allowed_groups': {'$in': ['staff']}}, {'owner_sub': {'$eq': 'me'}}]}
    assert fake.queries[0]['namespace'] == 't'
    assert vector.search('t', [1.0], [], owner_sub='') == []    # no groups and no owner: nothing is readable


# ---------------- index detection ----------------
class FakePC:
    def __init__(self, metric, dimension=1024, vector_type='dense'):
        self.desc = types.SimpleNamespace(status={'ready': True}, dimension=dimension, metric=metric,
                                          vector_type=vector_type, host='h')
        self.created = None

    def has_index(self, name): return True
    def describe_index(self, name): return self.desc
    def Index(self, host): return 'handle'


def detect(**kw):
    vector._index_info = _REAL_INDEX_INFO
    vector._index_info.cache_clear()
    vector._pc = lambda: FakePC(**kw)
    try:
        return vector._index_info()
    finally:
        vector._index_info.cache_clear()


def test_index_type_selects_the_mode():
    assert detect(metric='dotproduct') == ('handle', 'dotproduct')
    assert detect(metric='cosine') == ('handle', 'cosine')


def test_wrong_index_is_rejected_with_a_clear_message():
    for kw in (dict(metric='euclidean'), dict(metric='cosine', dimension=768), dict(metric='dotproduct', vector_type='sparse')):
        try:
            detect(**kw)
            raise AssertionError(f'should reject {kw}')
        except RuntimeError as e:
            assert 'PINECONE_INDEX_NAME' in str(e)


# ---------------- retrieval fallback ----------------
def test_retrieval_still_answers_when_the_keyword_model_is_down():
    from app import graph
    seen = {}
    graph.is_hybrid = lambda: True
    graph.embed = lambda texts: [[1.0]]
    def boom(*a, **k): raise RuntimeError('sparse model down')
    graph.sparse_embed = boom
    graph.search = lambda *a, **k: seen.update(k) or [{'id': 'x', 'score': 0.9, 'doc_id': 'd', 'title': 'T', 'chunk': 0, 'page': 1, 'text': 'hello'}]
    principal = types.SimpleNamespace(tenant='t', groups=('g',), subject='me')
    out = graph.retrieve({'principal': principal, 'question': 'what?', 'history': []})
    assert seen['sparse_query'] is None and len(out['passages']) == 1   # dense-only, not an error


def test_retrieval_sends_the_sparse_query_when_available():
    from app import graph
    seen = {}
    graph.is_hybrid = lambda: True
    graph.embed = lambda texts: [[1.0]]
    graph.sparse_embed = lambda texts, kind: [{'indices': [1], 'values': [1.0]}] if kind == 'query' else None
    graph.search = lambda *a, **k: seen.update(k) or []
    principal = types.SimpleNamespace(tenant='t', groups=('g',), subject='me')
    graph.retrieve({'principal': principal, 'question': 'annual leave', 'history': []})
    assert seen['sparse_query'] == {'indices': [1], 'values': [1.0]}


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
