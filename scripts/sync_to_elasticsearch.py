"""Fill Elasticsearch with the chunks that already exist in Pinecone (first-time load, or a repair).

Pinecone stores each chunk's text in its metadata, so nothing is re-uploaded or re-embedded: this reads every chunk
from the Pinecone index named in PINECONE_INDEX_NAME and indexes the same text in Elasticsearch under the same id.
Safe to rerun: documents are overwritten by id. It never deletes or changes anything in Pinecone.

Needs in .env: PINECONE_API_KEY, PINECONE_INDEX_NAME, ELASTIC_URL (and ELASTIC_API_KEY for Elastic Cloud; a local
http:// URL needs no key), and optionally ELASTIC_INDEX.

Usage (project root, venv active):
    python scripts/sync_to_elasticsearch.py --dry-run     # count only, writes nothing
    python scripts/sync_to_elasticsearch.py               # do it
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pinecone import Pinecone  # noqa: E402

from app import keyword  # noqa: E402
from app.config import settings  # noqa: E402

FETCH_BATCH = 100


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    s = settings()
    if not keyword.enabled():
        print('ERROR: Elasticsearch is not configured. Set ELASTIC_URL (and ELASTIC_API_KEY for Elastic Cloud) in .env,')
        print('       see docs/ELASTICSEARCH_SETUP.md. A local http:// URL needs no key.')
        return 1
    try:
        print('Elasticsearch :', keyword.check_connection())
    except Exception as e:
        print('ERROR: cannot use Elasticsearch:', e)
        return 1

    pc = Pinecone(api_key=s.pinecone_api_key)
    desc = pc.describe_index(s.pinecone_index_name)
    src = pc.Index(host=desc.host)
    namespaces = {ns: i['vector_count'] for ns, i in src.describe_index_stats()['namespaces'].items() if i['vector_count']}
    total = sum(namespaces.values())
    print(f'Pinecone      : {s.pinecone_index_name} -> {total} chunks in {len(namespaces)} namespace(s)')
    for ns, n in namespaces.items():
        print(f'                namespace {ns!r}: {n} chunks (in Elasticsearch now: {keyword.count(ns)})')
    if args.dry_run:
        print('\nDRY RUN: nothing was written.')
        return 0

    sent = 0
    for ns in namespaces:
        ids = [i for page in src.list(namespace=ns) for i in page]
        for start in range(0, len(ids), FETCH_BATCH):
            batch = ids[start:start + FETCH_BATCH]
            vectors = src.fetch(ids=batch, namespace=ns).vectors
            records = []
            for rid in batch:
                v = vectors.get(rid)
                m = dict(v.metadata or {}) if v else {}
                if not m.get('text'):
                    continue
                records.append({
                    'id': rid, 'tenant_id': ns, 'doc_id': m.get('doc_id', ''), 'title': m.get('title', ''), 'text': m['text'],
                    'page': int(m.get('page', 1)), 'chunk': int(m.get('chunk', 0)), 'version': int(m.get('version', 1)),
                    'allowed_groups': list(m.get('allowed_groups') or []), 'owner_sub': m.get('owner_sub', ''),
                })
            sent += keyword.index_records(records)
            print(f'  namespace {ns!r}: {min(start + FETCH_BATCH, len(ids))}/{len(ids)}')
    print(f'\nindexed {sent} chunks. Elasticsearch now holds:')
    ok = True
    for ns, n in namespaces.items():
        got = keyword.count(ns)
        ok &= got >= n
        print(f'  namespace {ns!r}: Pinecone {n}   Elasticsearch {got}   {"OK" if got >= n else "MISSING"}')
    print('\nNext: python scripts/compare_keyword.py   (see how keyword search changes the results)')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
