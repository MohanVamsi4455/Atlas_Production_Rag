"""Check S3, Groq/Gemini chat and Gemini embeddings using the app's own modules.

Usage (from project root, venv active): python scripts/check_connections.py
Never prints keys.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from app import aws, llm, vector  # noqa: E402
from app.config import settings  # noqa: E402


def step(name, fn):
    try:
        print(f'  OK    {name}: {fn()}')
        return True
    except Exception as exc:
        print(f'  FAIL  {name}: {type(exc).__name__}: {str(exc)[:300]}')
        return False


def s3_roundtrip():
    key = aws.make_key('healthcheck', 'ping.txt')
    aws.check_connection()
    try:
        aws.upload_bytes(key, b'ok', 'text/plain')
        assert aws.download_bytes(key) == b'ok'
    finally:
        aws.delete_object(key)
    return f'bucket {settings().s3_bucket!r} upload/download/delete'


def pinecone_roundtrip():
    import time
    info = vector.check_connection()
    tenant, doc = 'healthcheck', 'doc-check'
    vecs = llm.embed(['annual leave policy', 'best pizza in town'])
    try:
        vector.upsert_chunks(tenant, doc, 'Check', ['staff'], ['annual leave policy', 'best pizza in town'], vecs)
        hits = []
        for _ in range(10):  # Pinecone is eventually consistent
            hits = vector.search(tenant, llm.embed(['how many vacation days'])[0], ['staff'], top_k=2)
            if hits:
                break
            time.sleep(2)
        assert hits and hits[0]['text'] == 'annual leave policy', f'unexpected hits: {hits}'
        assert not vector.search(tenant, vecs[0], ['other-group']), 'ACL filter failed'
    finally:
        vector.delete_document(tenant, doc)
    return f'{info}; upsert/search/ACL/delete OK (top score {hits[0]["score"]:.2f})'


def chat_all():
    out = []
    for provider, model in llm.chat_plan():
        try:
            llm._call(provider, model, [{'role': 'user', 'content': 'Reply with the single word: ok'}],
                      'You are a helpful assistant.', 32, 0.0)
            out.append(f'{provider}/{model} OK')
        except Exception as exc:
            out.append(f'{provider}/{model} FAIL ({str(exc)[:50]})'.replace('\n', ' '))
    if not out:
        raise RuntimeError('no chat models configured')
    return '\n          ' + '\n          '.join(out)


if __name__ == '__main__':
    print('== Connections ==')
    results = [
        step('S3', s3_roundtrip),
        step('Chat failover', lambda: '%s -> %r' % (lambda r: (r[1], r[0]))(
            llm.chat([{'role': 'user', 'content': 'Say hello in one sentence.'}]))),
        step('Each chat model', chat_all),
        step('Pinecone', pinecone_roundtrip),
        step('Embeddings', lambda: f'{len(llm.embed(["annual leave policy"])[0])} dims'),
    ]
    sys.exit(0 if all(results) else 1)
