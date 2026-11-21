"""Copy an existing dense Pinecone index into a NEW hybrid (dense + sparse) index.

What it does, per chunk:
  * reads the chunk's dense vector and metadata from the SOURCE index (nothing is changed or deleted there),
  * scales the dense vector to length 1 (needed for a dotproduct index),
  * makes the sparse (keyword) vector from the chunk text stored in the metadata, using Pinecone's sparse model,
  * writes both to the TARGET index under the same id and namespace.
No Gemini calls are made, so it uses none of your embedding quota. Safe to rerun: records are overwritten by id.

Usage (from the project root, virtual environment active):
    python scripts/migrate_to_hybrid.py --target atlas-hybrid-index --dry-run     # count only, writes nothing
    python scripts/migrate_to_hybrid.py --target atlas-hybrid-index               # do it
    python scripts/migrate_to_hybrid.py --source other-index --target new-index

Afterwards switch the app by setting PINECONE_INDEX_NAME to the target index name. The old index is untouched,
so switching back is just changing that setting again.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pinecone import Pinecone, ServerlessSpec  # noqa: E402

from app.config import settings  # noqa: E402
from app.llm import _normalize, sparse_embed  # noqa: E402

FETCH_BATCH = 100


def main() -> int:
    s = settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default=s.pinecone_index_name, help='index to read (default: PINECONE_INDEX_NAME)')
    ap.add_argument('--target', required=True, help='NEW hybrid index to create and fill')
    ap.add_argument('--dry-run', action='store_true', help='only count and estimate; write nothing')
    args = ap.parse_args()
    if args.source == args.target:
        print('ERROR: --source and --target must be different indexes.')
        return 1
    if not s.pinecone_api_key:
        print('ERROR: PINECONE_API_KEY is not set.')
        return 1

    pc = Pinecone(api_key=s.pinecone_api_key)
    if not pc.has_index(args.source):
        print(f'ERROR: source index {args.source!r} does not exist.')
        return 1
    sd = pc.describe_index(args.source)
    if sd.dimension != s.embed_dim:
        print(f'ERROR: source dimension {sd.dimension} != EMBED_DIM {s.embed_dim}.')
        return 1
    src = pc.Index(host=sd.host)
    namespaces = {ns: info['vector_count'] for ns, info in src.describe_index_stats()['namespaces'].items()
                  if info['vector_count']}
    total = sum(namespaces.values())
    print(f'source  : {args.source} ({sd.metric}, {sd.dimension} dims)  ->  {total} vectors in {len(namespaces)} namespace(s)')
    for ns, n in namespaces.items():
        print(f'          namespace {ns or "(default)"!r}: {n}')
    print(f'target  : {args.target} (dotproduct, hybrid)')

    if args.dry_run:
        print('\nDRY RUN: nothing was written. The real run would read every chunk, call Pinecone\'s sparse model once '
              f'per {48} chunks, and upsert {total} records.')
        return 0
    if not total:
        print('Nothing to copy.')
        return 0

    if not pc.has_index(args.target):
        print(f'creating target index {args.target!r} ...')
        pc.create_index(name=args.target, dimension=s.embed_dim, metric='dotproduct',
                        spec=ServerlessSpec(cloud=s.pinecone_cloud, region=s.pinecone_region))
    deadline = time.time() + s.pinecone_ready_timeout
    while True:
        td = pc.describe_index(args.target)
        if td.status['ready']:
            break
        if time.time() > deadline:
            print('ERROR: target index was not ready in time.')
            return 1
        time.sleep(3)
    if str(td.metric).lower() != 'dotproduct' or td.dimension != s.embed_dim or str(td.vector_type).lower() != 'dense':
        print(f'ERROR: target {args.target!r} is not a dense dotproduct index of {s.embed_dim} dimensions '
              f'(metric={td.metric}, dimension={td.dimension}, type={td.vector_type}). Choose another --target name.')
        return 1
    tgt = pc.Index(host=td.host)

    copied = no_sparse = 0
    for ns, count in namespaces.items():
        ids = [i for page in src.list(namespace=ns) for i in page]
        print(f'\nnamespace {ns or "(default)"!r}: copying {len(ids)} chunks ...')
        for start in range(0, len(ids), FETCH_BATCH):
            batch_ids = ids[start:start + FETCH_BATCH]
            vectors = src.fetch(ids=batch_ids, namespace=ns).vectors
            rows = [(i, vectors[i]) for i in batch_ids if i in vectors]
            texts = [(v.metadata or {}).get('text') or ' ' for _, v in rows]
            try:
                sparse = sparse_embed(texts, 'passage')
            except Exception as e:  # keep the chunk searchable by meaning even if the keyword model fails
                print(f'  WARNING: sparse model failed for a batch ({type(e).__name__}); copying those chunks dense-only')
                sparse = [None] * len(rows)
            records = []
            for (rid, v), sp in zip(rows, sparse):
                meta = dict(v.metadata or {})
                rec = {'id': rid, 'values': _normalize(list(v.values)), 'metadata': meta}
                if sp:
                    rec['sparse_values'] = sp
                    meta['sparse_model'] = s.sparse_model
                else:
                    no_sparse += 1
                records.append(rec)
            tgt.upsert(vectors=records, namespace=ns)
            copied += len(records)
            print(f'  {copied}/{total}')

    print('\nverifying (Pinecone updates counts a few seconds after writing) ...')
    for _ in range(12):
        got = {ns: i['vector_count'] for ns, i in tgt.describe_index_stats()['namespaces'].items()}
        if all(got.get(ns, 0) >= n for ns, n in namespaces.items()):
            break
        time.sleep(5)
    for ns, n in namespaces.items():
        print(f'  namespace {ns or "(default)"!r}: source {n}  target {got.get(ns, 0)}  {"OK" if got.get(ns, 0) >= n else "MISSING"}')
    print(f'\ncopied {copied} chunks; {no_sparse} without a keyword vector (stored dense-only).')
    print(f'Next: compare quality with  python scripts/compare_search.py --index {args.target}')
    print(f'Then switch by setting PINECONE_INDEX_NAME={args.target} (the old index stays as a fallback).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
