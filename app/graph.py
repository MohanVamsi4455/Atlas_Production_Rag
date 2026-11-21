"""LangGraph RAG pipeline: guard -> retrieve -> (generate -> validate | no_evidence)."""
import re
from typing import Any, TypedDict

import warnings

try:
    # langgraph announces a future change to its checkpoint serializer. Atlas does not use checkpointing,
    # so hide that notice. langchain_core installs its own warning rules when it loads, so load it first.
    from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
    warnings.filterwarnings('ignore', category=LangChainPendingDeprecationWarning)
except ImportError:
    pass
from langgraph.graph import END, START, StateGraph

from app.guardrails import check_input, check_output, is_refusal
import logging

from app.llm import answer, embed, sparse_embed
from app.observability import trace
from app.pii import merge, redact
from app.vector import is_hybrid, search

log = logging.getLogger('atlas.graph')


class State(TypedDict, total=False):
    question: str
    principal: Any
    passages: list
    answer: str
    sources: list
    status: str
    history: list
    pii: dict


@trace('guard')
def guard(s):
    return {'question': check_input(s['question'])}


@trace('retrieve', kind='retriever')
def retrieve(s):
    p = s['principal']
    # Include the most recent user question for short follow-ups such as 'summarize that'.
    prior_questions = [m['content'] for m in s.get('history', []) if m['role'] == 'user']
    query = (prior_questions[-1] + '\nFollow-up: ' + s['question']) if prior_questions else s['question']
    sparse_q = None
    if is_hybrid():
        try:
            sparse_q = sparse_embed([query], 'query')[0]
        except Exception:  # keyword model unavailable: answer with meaning-only search instead of failing
            log.warning('Sparse query embedding failed; using dense-only search', exc_info=True)
    hits = search(p.tenant, embed([query])[0], list(p.groups), top_k=5, owner_sub=p.subject, sparse_query=sparse_q)
    pii = {}
    for h in hits:  # mask PII before the text reaches the LLM or the screen
        h['text'], found = redact(h['text'] or '')
        merge(pii, found)
    sources = [{'index': i + 1, 'document_id': h['doc_id'] or '', 'title': h['title'] or '',
                'page': h['page'] or 1, 'excerpt': (h['text'] or '')[:320]} for i, h in enumerate(hits)]
    return {'passages': hits, 'sources': sources, 'pii': pii}


def route(s):
    return 'generate' if s['passages'] else 'no_evidence'


@trace('no_evidence')
def no_evidence(s):
    return {'answer': 'I could not find supporting evidence in documents you are authorized to access.',
            'status': 'no_evidence'}


@trace('generate')
def generate(s):
    evidence = '\n\n'.join(
        f'[{i + 1}] {h["title"] or "Document"}, page {h["page"] or 1}: {h["text"] or ""}'
        for i, h in enumerate(s['passages']))
    return {'answer': answer(s['question'], evidence, s.get('history', [])), 'status': 'answered'}


_NOT_FOUND = re.compile(r"\b(?:could\s?n['\u2019]?t|could\s+not|can\s?not|can['\u2019]?t|unable\s+to)\s+find\b|\bno\s+(?:supporting\s+)?(?:information|evidence)\b", re.I)


@trace('validate')
def validate(s):
    answer, found = redact(check_output(s['answer']))
    out = {'answer': answer, 'pii': merge(dict(s.get('pii') or {}), found)}
    cited = {int(n) for n in re.findall(r'\[(\d+)\]', answer)}
    if is_refusal(answer):                      # the model declined: its passages are irrelevant
        out.update(status='declined', sources=[])
    else:                                       # show only the passages the answer cites (none if it cites nothing)
        out['sources'] = [x for x in s.get('sources', []) if x['index'] in cited]
        if not cited and _NOT_FOUND.search(answer):  # "could not find ...": nothing supports it
            out['status'] = 'no_evidence'
    return out


def make_graph():
    g = StateGraph(State)
    for name, fn in [('guard', guard), ('retrieve', retrieve), ('no_evidence', no_evidence),
                     ('generate', generate), ('validate', validate)]:
        g.add_node(name, fn)
    g.add_edge(START, 'guard')
    g.add_edge('guard', 'retrieve')
    g.add_conditional_edges('retrieve', route, {'generate': 'generate', 'no_evidence': 'no_evidence'})
    g.add_edge('generate', 'validate')
    g.add_edge('validate', END)
    g.add_edge('no_evidence', END)
    return g.compile()


graph = make_graph()


@trace('chat')
def chat(p, question, history=None):
    r = graph.invoke({'principal': p, 'question': question, 'history': history or []})
    return {'answer': r['answer'], 'sources': r.get('sources', []), 'status': r.get('status', 'answered'), 'pii': r.get('pii', {})}
