"""Compare meaning-only, hybrid and keyword-only search on your own documents, to choose HYBRID_ALPHA.

alpha = 1.0 -> dense only (meaning)      alpha = 0.0 -> sparse only (keywords)      in between -> hybrid.
Each question lists a phrase that MUST appear in the right chunk; the tool reports how often it is the top result.

Usage (project root, venv active):
    python scripts/compare_search.py --index atlas-hybrid-index
    python scripts/compare_search.py --index atlas-hybrid-index --questions my_questions.json
        (a JSON list like [{"q": "How many leave days?", "expect": "24 days"}])
    add --show to print the top results for every question.
Reads only; it changes nothing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--index', required=True)
ap.add_argument('--questions')
ap.add_argument('--tenant', default='demo-workspace')
ap.add_argument('--groups', default='workspace')
ap.add_argument('--owner', default='demo-operator')
ap.add_argument('--alphas', default='1.0,0.8,0.6,0.4,0.2,0.0')
ap.add_argument('--show', action='store_true')
args = ap.parse_args()
os.environ['PINECONE_INDEX_NAME'] = args.index
os.environ['MIN_RELEVANCE'] = '0'          # show every score, even low ones

import warnings  # noqa: E402
warnings.filterwarnings('ignore')
from app import llm, vector  # noqa: E402

DEFAULT = [
    {'q': 'How many days of annual leave do I get?', 'expect': '24 days'},
    {'q': 'What happens to unused leave at the end of the year?', 'expect': 'carried forward'},
    {'q': 'How long is parental leave for the second caregiver?', 'expect': '4 weeks'},
    {'q': 'time off after having a baby', 'expect': 'parental leave'},
    {'q': 'how long to report phishing', 'expect': '1 hour'},
    {'q': 'minimum password length', 'expect': '12 characters'},
    {'q': 'VPN on public wifi', 'expect': 'VPN'},
    {'q': 'USB', 'expect': 'USB'},
    {'q': '30-60-90', 'expect': '30-60-90'},
    {'q': 'expense claims deadline', 'expect': '30 days'},
    {'q': 'Does Mohan know LangGraph?', 'expect': 'LangGraph'},
    {'q': 'FAISS', 'expect': 'FAISS'},
]
questions = json.loads(Path(args.questions).read_text(encoding='utf-8')) if args.questions else DEFAULT
alphas = [float(a) for a in args.alphas.split(',')]
groups = [g for g in args.groups.split(',') if g]

if not vector.is_hybrid():
    print('This index is not hybrid (cosine). Run scripts/migrate_to_hybrid.py first.')
    sys.exit(1)

stats = {a: {'hit1': 0, 'hit3': 0, 'top': [], 'rel': []} for a in alphas}
for item in questions:
    dq = llm.embed([item['q']])[0]
    sq = llm.sparse_embed([item['q']], 'query')[0]
    if args.show:
        print(f"\nQ: {item['q']}   (must contain: {item['expect']!r})")
    for a in alphas:
        hits = vector.search(args.tenant, dq, groups, top_k=3, owner_sub=args.owner, sparse_query=sq, alpha=a)
        ok = [item['expect'].lower() in (h['text'] or '').lower() for h in hits]
        stats[a]['hit1'] += bool(ok[:1] and ok[0])
        stats[a]['hit3'] += any(ok)
        if hits:
            stats[a]['top'].append(hits[0]['score'])
            if ok[0]:
                stats[a]['rel'].append(hits[0]['score'])
        if args.show:
            h = hits[0] if hits else None
            print(f"   alpha {a:.1f}: {'HIT ' if ok[:1] and ok[0] else 'miss'}  "
                  + (f"{h['score']:.3f}  {h['title']} p{h['page']}: {(h['text'] or '')[:55]!r}" if h else 'no result'))

n = len(questions)
print(f'\n{n} questions on index {args.index!r}')
print('alpha   top-1 correct   top-3 correct   avg top score   avg score when top-1 is right')
for a in alphas:
    s = stats[a]
    label = {1.0: ' (dense only)', 0.0: ' (keywords only)'}.get(a, '')
    avg = lambda xs: sum(xs) / len(xs) if xs else float('nan')  # noqa: E731
    print(f'{a:.1f}     {s["hit1"]:>2}/{n} ({100 * s["hit1"] // n:>3}%)   {s["hit3"]:>2}/{n} ({100 * s["hit3"] // n:>3}%)'
          f'     {avg(s["top"]):.3f}           {avg(s["rel"]):.3f}{label}')
best = max(alphas, key=lambda a: (stats[a]['hit1'], stats[a]['hit3'], -abs(a - 0.6)))
print(f'\nbest top-1 accuracy at alpha = {best}')
