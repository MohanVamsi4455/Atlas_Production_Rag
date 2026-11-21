"""Compare meaning-only, keyword-only (Elasticsearch BM25) and the merged result on your own documents.

For each question a phrase must appear in the right chunk. The tool reports how often the right chunk is the top
result (1st) and among the top three, for: meaning only, keywords only, and meaning + keywords merged.

Usage (project root, venv active; needs ELASTIC_URL / ELASTIC_API_KEY and a synced index):
    python scripts/compare_keyword.py
    python scripts/compare_keyword.py --questions my_questions.json --show
        (a JSON list like [{"q": "How many leave days?", "expect": "24 days"}])
Reads only; it changes nothing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--questions')
ap.add_argument('--tenant', default='demo-workspace')
ap.add_argument('--groups', default='workspace')
ap.add_argument('--owner', default='demo-operator')
ap.add_argument('--show', action='store_true')
args = ap.parse_args()
os.environ['MIN_RELEVANCE'] = '0.25'

import warnings  # noqa: E402
warnings.filterwarnings('ignore')
from app import keyword, llm, vector  # noqa: E402
from app.config import settings  # noqa: E402
from app.fusion import fuse_rrf  # noqa: E402
from sample_questions import QUESTIONS  # noqa: E402

cfg = settings()
if not keyword.enabled():
    print('Elasticsearch is not configured (ELASTIC_URL / ELASTIC_API_KEY in .env).')
    sys.exit(1)
questions = json.loads(Path(args.questions).read_text(encoding='utf-8')) if args.questions else QUESTIONS
groups = [g for g in args.groups.split(',') if g]
modes = ['meaning only', 'keywords only', 'merged']
stats = {m: {'hit1': 0, 'hit3': 0, 'none': 0} for m in modes}

for item in questions:
    dense = vector.search(args.tenant, llm.embed([item['q']])[0], groups, top_k=10, owner_sub=args.owner)
    kw = keyword.search(args.tenant, item['q'], groups, owner_sub=args.owner, top_k=10)
    results = {'meaning only': dense[:3], 'keywords only': kw[:3], 'merged': fuse_rrf(dense, kw, top_k=3, dense_weight=cfg.rrf_dense_weight, keyword_weight=cfg.rrf_keyword_weight)}
    if args.show:
        print(f"\nQ: {item['q']}   (must contain: {item['expect']!r})")
    for mode, hits in results.items():
        ok = [item['expect'].lower() in (h.get('text') or '').lower() for h in hits]
        stats[mode]['hit1'] += bool(ok[:1] and ok[0])
        stats[mode]['hit3'] += any(ok)
        stats[mode]['none'] += not hits
        if args.show:
            h = hits[0] if hits else None
            print(f"   {mode:14}: {'HIT ' if ok[:1] and ok[0] else 'miss'}  "
                  + (f"{h.get('title')} p{h.get('page')}: {(h.get('text') or '')[:60]!r}" if h else 'no result'))

n = len(questions)
print(f'\n{n} questions   (tenant {args.tenant!r})')
print('mode             top-1 correct    top-3 correct    returned nothing')
for mode in modes:
    s = stats[mode]
    print(f'{mode:14}   {s["hit1"]:>2}/{n} ({100 * s["hit1"] // n:>3}%)    {s["hit3"]:>2}/{n} ({100 * s["hit3"] // n:>3}%)    {s["none"]}')
