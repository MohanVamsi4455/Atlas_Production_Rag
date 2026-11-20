// Simple single-workspace UI. No login, tokens, or user accounts.
// All dynamic text is added with textContent / DOM nodes, never untrusted innerHTML.
const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

// crypto.randomUUID() and navigator.clipboard exist only on HTTPS or localhost. Atlas is also served over plain
// HTTP (for example http://<server-ip>:8000), so use fallbacks that work everywhere.
const newId = () => {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16);
  if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(b); else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map(x => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
};
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return; }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0'; document.body.append(ta); ta.select();
  const ok = document.execCommand('copy'); ta.remove();
  if (!ok) throw Error('copy failed');
}

// Single-workspace conversation identifier, not an authentication token.
let conversationId = sessionStorage.getItem('atlas_conversation_id') || newId();
sessionStorage.setItem('atlas_conversation_id', conversationId);
let busy = false;

const toast = msg => { const t = $('toast'); t.textContent = msg; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 4000); };
const api = async (path, options = {}) => {
  const res = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Error(data.detail || 'Request failed');
  return data;
};

function view(name) {
  ['chat', 'docs', 'approval', 'admin'].forEach(x => { $(x + 'View').classList.toggle('hidden', x !== name); $(x + 'Tab').classList.toggle('active', x === name); });
  $('heading').textContent = { chat: "Ask your organization's knowledge", docs: 'Document management', approval: 'Approval requests', admin: 'Operational overview' }[name];
  if (name === 'docs') loadDocs();
  if (name === 'admin') loadMetrics();
  if (name === 'approval') loadApprovals();
}
$('chatTab').onclick = () => view('chat'); $('docsTab').onclick = () => view('docs');
$('adminTab').onclick = () => view('admin'); $('approvalTab').onclick = () => view('approval');
$('refreshApprovals').onclick = loadApprovals;

/* ---------- Theme (light / dark) ---------- */
function applyTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  $('themeIcon').setAttribute('href', name === 'dark' ? '#i-sun' : '#i-moon');
  try { localStorage.setItem('atlas_theme', name); } catch (e) { /* optional */ }
}
$('themeBtn').onclick = () => applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
$('themeIcon').setAttribute('href', document.documentElement.getAttribute('data-theme') === 'dark' ? '#i-sun' : '#i-moon');

/* ---------- Chat rendering ---------- */
const scrollDown = () => { const s = $('scroller'); s.scrollTo({ top: s.scrollHeight, behavior: 'smooth' }); };
const clock = d => (d ? new Date(d) : new Date()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const updateHero = () => $('hero').classList.toggle('hidden', $('messages').children.length > 0);

// Add inline formatting (**bold**, `code`, and [1] / 【1】 citations) to a parent node.
function inline(parent, text, rowId) {
  const re = /(\[[A-Z]+ REDACTED\]|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`|[\[【]\s*\d+(?:\s*[†‡][^\]】]*)?[\]】])/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    const tok = m[0];
    if (/ REDACTED\]$/.test(tok)) parent.append(el('span', 'redacted', tok.slice(1, -1).toLowerCase()));
    else if (tok.startsWith('**')) parent.append(el('strong', null, tok.slice(2, -2)));
    else if (tok.startsWith('*')) parent.append(el('em', null, tok.slice(1, -1)));
    else if (tok.startsWith('`')) parent.append(el('code', null, tok.slice(1, -1)));
    else {
      const n = (tok.match(/\d+/) || [''])[0];
      const s = (rowSources[rowId] || []).find(x => String(x.index) === n);
      let c;
      if (s && s.document_id && !s.restricted) {
        // A normal link: opens the original file at the cited page in a new tab.
        c = el('a', 'cite', n); c.href = docLink(s.document_id, s.page); c.target = '_blank'; c.rel = 'noopener';
        c.title = `Open ${s.title || 'document'} · page ${s.page || 1}`;
        attachViewer(c, s.document_id, s.title, s.page, () => openSource(rowId, n));
      } else {
        c = el('span', 'cite', n); c.title = 'Show source ' + n; c.onclick = () => openSource(rowId, n);
      }
      parent.append(c);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

// Minimal markdown: paragraphs, "- " / "* " bullets, "1. " lists.
function renderText(container, text, rowId) {
  const blocks = text.replace(/\r/g, '').split(/\n{2,}/);
  for (const block of blocks) {
    const lines = block.split('\n').filter(l => l.trim() !== '');
    if (!lines.length) continue;
    if (lines.every(l => /^\s*[-*•]\s+/.test(l))) {
      const ul = el('ul'); lines.forEach(l => { const li = el('li'); inline(li, l.replace(/^\s*[-*•]\s+/, ''), rowId); ul.append(li); }); container.append(ul);
    } else if (lines.every(l => /^\s*\d+[.)]\s+/.test(l))) {
      const ol = el('ol'); lines.forEach(l => { const li = el('li'); inline(li, l.replace(/^\s*\d+[.)]\s+/, ''), rowId); ol.append(li); }); container.append(ol);
    } else {
      const p = el('p');
      lines.forEach((l, i) => { if (i) p.append(el('br')); inline(p, l, rowId); });
      container.append(p);
    }
  }
}

function openSource(rowId, n) {
  const d = document.querySelector(`[data-row="${rowId}"] [data-src="${n}"]`);
  if (!d) return;
  const box = d.closest('.sources-box'); if (box) box.open = true;
  d.open = true; d.classList.add('flash'); d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  setTimeout(() => d.classList.remove('flash'), 1500);
}

// Link that opens the original file (PDFs jump to the cited page). The server checks access and redirects.
const docLink = (documentId, page) => `/api/documents/${encodeURIComponent(documentId)}/open?page=${page || 1}`;
const rowSources = {};

// In-app reader: a slide-in panel beside the chat. PDF/TXT/MD show inline; DOCX can't be previewed,
// so those links keep their normal behaviour (download in a new tab).
const viewable = title => /\.(pdf|txt|md)$/i.test(title || '');
function openViewer(documentId, title, page) {
  const url = docLink(documentId, page), frame = $('viewerFrame'), loading = document.querySelector('.viewer-loading');
  $('viewerTitle').textContent = title || 'Document';
  $('viewerPage').textContent = /\.pdf$/i.test(title || '') ? `Page ${page || 1}` : '';
  $('viewerNewTab').href = url;
  loading.classList.remove('done'); frame.onload = () => loading.classList.add('done');
  frame.src = url;
  restoreViewerBox(); $('viewer').classList.add('open'); $('viewer').setAttribute('aria-hidden', 'false');
}
function closeViewer() {
  $('viewer').classList.remove('open'); $('viewer').setAttribute('aria-hidden', 'true');
  $('viewerFrame').src = 'about:blank';
}
$('viewerClose').onclick = closeViewer;
// Floating window: drag by the title bar, resize from the bottom-right corner. Position is remembered.
const VBOX = 'atlas_viewer_box';
const mobile = () => window.matchMedia('(max-width:760px)').matches;
function clampViewer() {
  const v = $('viewer'), r = v.getBoundingClientRect();
  const w = Math.min(Math.max(r.width, 320), window.innerWidth), h = Math.min(Math.max(r.height, 280), window.innerHeight);
  v.style.width = w + 'px'; v.style.height = h + 'px';
  v.style.left = Math.min(Math.max(r.left, 0), window.innerWidth - w) + 'px';
  v.style.top = Math.min(Math.max(r.top, 0), window.innerHeight - 48) + 'px';
  v.style.right = 'auto'; v.style.bottom = 'auto';
}
function saveViewerBox() {
  try { const r = $('viewer').getBoundingClientRect(); localStorage.setItem(VBOX, JSON.stringify({ l: r.left, t: r.top, w: r.width, h: r.height })); } catch (e) { /* optional */ }
}
function restoreViewerBox() {
  if (mobile()) return;
  try {
    const b = JSON.parse(localStorage.getItem(VBOX) || 'null'); if (!b) return;
    const v = $('viewer'); v.style.width = b.w + 'px'; v.style.height = b.h + 'px';
    v.style.left = b.l + 'px'; v.style.top = b.t + 'px'; v.style.right = 'auto'; v.style.bottom = 'auto';
    clampViewer();
  } catch (e) { /* use the default position */ }
}
function pointerDrag(handle, cls, onMove) {
  handle.addEventListener('pointerdown', e => {
    if (mobile() || e.button !== 0 || e.target.closest('button,a')) return;
    e.preventDefault();
    const v = $('viewer'), r = v.getBoundingClientRect();
    const start = { x: e.clientX, y: e.clientY, l: r.left, t: r.top, w: r.width, h: r.height };
    v.style.left = r.left + 'px'; v.style.top = r.top + 'px'; v.style.right = 'auto'; v.style.bottom = 'auto';
    v.classList.add(cls); handle.setPointerCapture(e.pointerId);
    const move = ev => onMove(v, start, ev.clientX - start.x, ev.clientY - start.y);
    const up = () => {
      v.classList.remove(cls);
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up);
      clampViewer(); saveViewerBox();
    };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
  });
}
pointerDrag(document.querySelector('.viewer-head'), 'dragging', (v, s, dx, dy) => {
  v.style.left = Math.min(Math.max(s.l + dx, 0), window.innerWidth - 120) + 'px';
  v.style.top = Math.min(Math.max(s.t + dy, 0), window.innerHeight - 48) + 'px';
});
pointerDrag($('viewerResize'), 'resizing', (v, s, dx, dy) => {
  v.style.width = Math.min(Math.max(s.w + dx, 320), window.innerWidth - s.l) + 'px';
  v.style.height = Math.min(Math.max(s.h + dy, 280), window.innerHeight - s.t) + 'px';
});
window.addEventListener('resize', () => { if (!mobile() && $('viewer').classList.contains('open') && $('viewer').style.left) clampViewer(); });

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeViewer(); });
// Make a link open in the panel on a normal click (ctrl/shift/middle-click still open a new tab).
function attachViewer(a, documentId, title, page, extra) {
  a.addEventListener('click', e => {
    if (extra) extra();
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || !viewable(title)) return;
    e.preventDefault(); openViewer(documentId, title, page);
  });
}

let rowCounter = 0;
// Visual state for answers that are not a normal grounded reply.
const NOTICES = {
  guardrail_blocked: { cls: 'guard', icon: 'i-shield', label: 'Blocked by guardrail' },
  declined: { cls: 'declined', icon: 'i-info', label: 'Request declined' },
  no_evidence: { cls: 'noev', icon: 'i-info', label: 'No supporting evidence found' },
  pending_approval: { cls: 'pending', icon: 'i-check', label: 'Awaiting approval' },
};
const GUARD_REASONS = {
  prompt_injection_or_secret_request: 'Prompt injection / secret request',
  harmful_request: 'Harmful request',
  pii_in_question: 'Sensitive ID in the question',
  secret_in_question: 'Password / PIN typed in the question',
  empty_input: 'Empty question',
};
function svgIcon(id) {
  const ns = 'http://www.w3.org/2000/svg', s = document.createElementNS(ns, 'svg'), u = document.createElementNS(ns, 'use');
  s.setAttribute('class', 'ic'); u.setAttribute('href', '#' + id); s.append(u); return s;
}

const REFUSAL_RX = /\b(?:can['\u2019]?t|cannot|can\s+not|won['\u2019]?t|will\s+not|unable\s+to)\s+(?:help|assist|comply|do\s+that|provide\s+(?:that|this))\b/i;
const NOTFOUND_RX = /\b(?:could\s?n['\u2019]?t|could\s+not|can\s?not|can['\u2019]?t|unable\s+to)\s+find\b|\bno\s+(?:supporting\s+)?(?:information|evidence)\b/i;
const citedNumbers = text => new Set([...text.matchAll(/[\[\u3010]\s*(\d+)/g)].map(m => m[1]));
// A refusal or "not found" answer has no sources; otherwise keep only the passages the answer cites.
function usedSources(content, sources) {
  if (!sources.length) return sources;
  if (REFUSAL_RX.test(content.slice(0, 300))) return [];
  const cited = citedNumbers(content);
  return sources.filter(s => cited.has(String(s.index)));   // none cited -> none shown
}
function inferStatus(content) {
  if (REFUSAL_RX.test(content.slice(0, 300))) return 'declined';
  return !citedNumbers(content).size && NOTFOUND_RX.test(content) ? 'no_evidence' : null;
}

function addMessage(content, kind, opts = {}) {
  const { when = null, error = false, reason = null, piiMasked = false, piiNote = null } = opts;
  const sources = kind === 'assistant' ? usedSources(content, opts.sources || []) : [];
  const status = opts.status || (kind === 'assistant' ? inferStatus(content) : null);
  const notice = kind === 'assistant' && !error ? NOTICES[status] : null;
  const rowId = ++rowCounter;
  rowSources[rowId] = sources;
  const row = el('div', 'row ' + kind + (error ? ' error' : '')); row.dataset.row = rowId;
  row.append(el('div', 'avatar', kind === 'user' ? 'You' : 'A'));
  const bubble = el('div', 'bubble');
  if (notice) {
    const n = el('div', 'notice ' + notice.cls); n.append(svgIcon(notice.icon), el('span', null, notice.label));
    if (reason && GUARD_REASONS[reason]) n.append(el('span', 'why', GUARD_REASONS[reason]));
    bubble.append(n);
  }
  const text = el('div', 'text' + (notice ? ' t-' + notice.cls : ''));
  if (kind === 'user') text.textContent = content; else renderText(text, content, rowId);
  bubble.append(text);

  if (kind === 'assistant' && sources.length) {
    const box = el('details', 'sources-box'); const bsum = el('summary');
    bsum.append(svgIcon('i-file'), el('span', null, `Sources (${sources.length})`)); box.append(bsum);
    const wrap = el('div', 'sources');
    sources.forEach(s => {
      const d = el('details', 'source'); d.dataset.src = s.index;
      const sum = el('summary');
      sum.append(el('span', 'n', s.index), el('span', 't', s.title || 'Document'), el('span', 'p', s.restricted ? '🔒 restricted' : 'page ' + (s.page || 1)));
      d.append(sum, el('p', null, s.restricted ? 'This document contains personal data, so its text and original file are not shown or opened here.' : (s.excerpt || '')));
      if (s.document_id && !s.restricted) {
        const open = el('a', 'open-doc', `Open original · page ${s.page || 1} ↗`);
        open.href = docLink(s.document_id, s.page); open.target = '_blank'; open.rel = 'noopener';
        attachViewer(open, s.document_id, s.title, s.page);
        d.append(open);
      }
      wrap.append(d);
    });
    box.append(wrap); bubble.append(box);
  }

  if (kind === 'assistant' && !error) {
    const actions = el('div', 'actions');
    const copy = el('button', null, '⧉ Copy');
    if (status === 'guardrail_blocked') copy.classList.add('hidden');
    copy.onclick = async () => { try { await copyText(content); toast('Copied'); } catch { toast('Copy failed'); } };
    actions.append(copy);
    if (sources.length) {
      const rate = (label, rating) => {
        const b = el('button', null, label);
        b.onclick = async () => {
          try { await api('/api/feedback', { method: 'POST', body: JSON.stringify({ rating }) }); actions.querySelectorAll('button').forEach(x => x.classList.remove('on')); b.classList.add('on'); toast('Feedback saved'); }
          catch (e) { toast(e.message); }
        };
        return b;
      };
      actions.append(rate('👍', 1), rate('👎', -1));
    }
    if (status === 'pending_approval') {
      const go = el('button', 'review-btn', 'Review approval request →'); go.onclick = () => view('approval'); actions.append(go);
    }
    bubble.append(actions);
  }

  const meta = el('div', 'meta', (kind === 'user' ? 'You' : 'Atlas') + ' · ' + clock(when));
  if (piiMasked) { const c = el('span', 'chip-pii'); c.append(svgIcon('i-shield'), 'PII masked'); c.title = piiNote || 'Personal data in the documents was hidden from this answer'; meta.append(c); }
  bubble.append(meta);
  row.append(bubble);
  $('messages').append(row);
  updateHero(); scrollDown();
  return row;
}

function addTyping() {
  const row = el('div', 'row assistant');
  row.append(el('div', 'avatar', 'A'));
  const bubble = el('div', 'bubble'), text = el('div', 'text');
  const dots = el('span', 'typing'); dots.append(el('i'), el('i'), el('i'));
  text.append(dots, el('span', 'status-note', 'Searching your documents…'));
  bubble.append(text); row.append(bubble);
  $('messages').append(row); updateHero(); scrollDown();
  return row;
}

async function send(q) {
  if (busy || !q) return;
  busy = true; $('sendBtn').disabled = true;
  addMessage(q, 'user');
  const typing = addTyping();
  try {
    const r = await api('/api/chat', { method: 'POST', body: JSON.stringify({ question: q, conversation_id: conversationId }) });
    typing.remove();
    addMessage(r.answer, 'assistant', { sources: r.sources || [], status: r.status, reason: r.reason, piiMasked: r.pii_masked,
      piiNote: r.question_masked ? 'Personal data in your message was masked before it was processed' : null });
    loadThreads();
  } catch (err) {
    typing.remove();
    addMessage(err.message, 'assistant', { error: true });
  } finally {
    busy = false; updateSend(); $('question').focus();
  }
}

const box = $('question');
const updateSend = () => { $('sendBtn').disabled = busy || !box.value.trim(); };
const grow = () => { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight, 160) + 'px'; updateSend(); };
box.addEventListener('input', grow);
updateSend();
box.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('chatForm').requestSubmit(); } });
$('chatForm').onsubmit = e => { e.preventDefault(); const q = box.value.trim(); if (!q) return; box.value = ''; grow(); send(q); };
document.querySelectorAll('[data-q]').forEach(b => b.onclick = () => { box.value = b.dataset.q; grow(); box.focus(); });

/* ---------- Conversation threads ---------- */
let threads = [];
const ago = d => {
  const s = (Date.now() - new Date(d).getTime()) / 1000;
  if (s < 60) return 'just now'; if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago'; if (s < 172800) return 'yesterday';
  return new Date(d).toLocaleDateString([], { day: 'numeric', month: 'short' });
};
const shorten = (s, n = 42) => { s = (s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

function renderThreads() {
  const box = $('threads'); box.replaceChildren();
  const list = threads.slice();
  // The current conversation has no saved messages yet, so show it as a placeholder at the top.
  if (!list.some(x => x.id === conversationId)) list.unshift({ id: conversationId, title: 'New conversation', updated_at: null, draft: true });
  const active = list.find(x => x.id === conversationId);
  document.querySelector('.chat-title').textContent = active ? shorten(active.title, 60) : 'Conversation';
  for (const th of list) {
    const row = el('div', 'thread' + (th.id === conversationId ? ' active' : ''));
    const main = el('button', 'main'); main.type = 'button'; main.title = th.title || '';
    main.append(el('span', 'ttl', shorten(th.title) || 'Conversation'), el('span', 'when', th.updated_at ? ago(th.updated_at) : 'Not saved yet'));
    main.onclick = () => switchThread(th.id);
    row.append(main);
    if (!th.draft) {
      const del = el('button', 'del', '✕'); del.type = 'button'; del.title = 'Delete this conversation';
      del.onclick = async e => {
        e.stopPropagation();
        if (!confirm('Delete this conversation and its messages? This cannot be undone.')) return;
        try {
          await api('/api/conversations/' + encodeURIComponent(th.id), { method: 'DELETE' });
          if (th.id === conversationId) startThread(); else await loadThreads();
          toast('Conversation deleted');
        } catch (err) { toast(err.message); }
      };
      row.append(del);
    }
    box.append(row);
  }
}
async function loadThreads() {
  try { threads = (await api('/api/conversations')).conversations || []; } catch (e) { /* keep the current list */ }
  renderThreads();
}
async function loadConversation() {
  $('messages').replaceChildren();
  try {
    const r = await api(`/api/conversations/${conversationId}/messages`);
    (r.messages || []).forEach(m => addMessage(m.content, m.role === 'user' ? 'user' : 'assistant', { sources: m.sources || [], when: m.created_at }));
  } catch (e) { /* a new conversation has no history yet */ }
  updateHero(); scrollDown();
}
function setConversation(id) { conversationId = id; sessionStorage.setItem('atlas_conversation_id', id); }
async function switchThread(id) {
  if (busy) { toast('Wait for the current answer to finish'); return; }
  view('chat');
  if (id === conversationId) return;
  setConversation(id); renderThreads(); await loadConversation(); box.focus();
}
function startThread() {
  if (busy) { toast('Wait for the current answer to finish'); return; }
  view('chat'); setConversation(newId());
  $('messages').replaceChildren(); updateHero(); loadThreads(); box.focus();
}
$('newChat').onclick = startThread;
$('newThread').onclick = startThread;

(async function init() {
  const m = /(?:^|[#&])c=([0-9a-f-]{36})/i.exec(location.hash || '');
  if (m) setConversation(m[1]);
  renderThreads(); await Promise.all([loadConversation(), loadThreads()]);
})();

/* ---------- Documents ---------- */
const fileExt = name => ((name.split('.').pop() || 'file').toLowerCase());
async function loadDocs() {
  try {
    const r = await api('/api/documents');
    if (!r.documents.length) { $('docs').replaceChildren(el('div', 'empty', 'No documents yet. Upload one above.')); return; }
    $('docs').replaceChildren(...r.documents.map(d => {
      const row = el('div', 'doc');
      const info = el('div', 'info');
      let nameEl;
      if (d.contains_pii) { nameEl = el('div', 'name', d.title); nameEl.title = 'Contains personal data, so it cannot be opened here'; }
      else {
        nameEl = el('a', 'name link', d.title); nameEl.title = 'Open the original file';
        nameEl.href = docLink(d.id, 1); nameEl.target = '_blank'; nameEl.rel = 'noopener';
        attachViewer(nameEl, d.id, d.title, 1);
      }
      info.append(nameEl,el('div', 'sub', `v${d.current_version} · ${new Date(d.created_at).toLocaleString()}`));
      row.append(el('div', 'ico ft-' + fileExt(d.title), fileExt(d.title).toUpperCase().slice(0, 4)), info, ...(d.contains_pii ? [el('span', 'badge pii', '🔒 personal data')] : []), el('span', 'badge ' + d.status, d.status));
      return row;
    }));
  } catch (e) { $('docs').textContent = e.message; }
}
const REASONS = {
  prompt_injection_or_secret_request: 'Prompt injection / secret request',
  empty_input: 'Empty question',
  sensitive_data_in_answer: 'Sensitive data in the answer',
  harmful_request: 'Harmful / hacking request',
  pii_in_question: 'Sensitive ID typed in the question',
  secret_in_question: 'Password / PIN typed in the question',
  pii_redacted: 'PII masked in evidence / answer',
  model_declined: 'Model declined to help',
};
const tile = (num, label) => { const t = el('div', 'tile'); t.append(el('div', 'num', String(num)), el('div', 'lbl', label)); return t; };
async function loadMetrics() {
  try {
    const m = await api('/api/admin/metrics'), g = await api('/api/admin/guardrails');
    const tiles = $('metrics'); tiles.replaceChildren();
    if (!m.documents.length) tiles.append(tile(0, 'documents'));
    m.documents.forEach(d => tiles.append(tile(d.total, d.status === 'retired' ? 'deleted documents' : d.status + ' documents')));
    const up = m.feedback.filter(f => f.rating > 0).reduce((n, f) => n + f.total, 0), down = m.feedback.filter(f => f.rating < 0).reduce((n, f) => n + f.total, 0);
    tiles.append(tile('👍 ' + up, 'helpful'), tile('👎 ' + down, 'not helpful'));
    g.events.forEach(e => tiles.append(tile(e.total, { guardrail_input_blocked: 'questions blocked', guardrail_output_blocked: 'answers blocked', guardrail_model_refused: 'declined by the model', guardrail_pii_redacted: 'PII masked' }[e.action] || e.action)));

    const root = $('guardrailMetrics'); root.replaceChildren();
    if (!g.recent.length) { root.append(el('p', null, 'No requests have been blocked.')); return; }
    const table = el('table', 'events'), head = el('tr');
    ['When', 'Type', 'Reason', 'Message'].forEach(h => head.append(el('th', null, h)));
    table.append(head);
    g.recent.forEach(ev => {
      const d = ev.detail || {}, row = el('tr');
      const kind = { guardrail_output_blocked: ['Answer blocked', ' out'], guardrail_model_refused: ['Model declined', ' out'], guardrail_pii_redacted: ['PII masked', ' out'] }[ev.action] || ['Question blocked', ''];
      const type = el('td'); type.append(el('span', 'chip' + kind[1], kind[0]));
      row.append(el('td', null, new Date(ev.created_at).toLocaleString()), type,
                 el('td', null, REASONS[d.reason] || d.reason || '–'),
                 el('td', (d.preview || d.types) ? 'msg' : 'na', (d.preview || '') + (d.types ? (d.preview ? '  ·  ' : '') + 'masked: ' + Object.entries(d.types).map(([k, v]) => `${k} ×${v}`).join(', ') : '') || 'not recorded (logged before previews were added)'));
      table.append(row);
    });
    root.append(table);
  } catch (e) { $('metrics').textContent = e.message; }
}
// Drop zone: show the chosen file name and accept drag-and-drop.
const dropDefault = { title: 'Drop a file here, or click to browse', sub: 'PDF, DOCX, TXT or MD · up to 10 MB' };
function showPicked() {
  const f = $('file').files[0];
  $('dropTitle').textContent = f ? f.name : dropDefault.title;
  $('dropSub').textContent = f ? (f.size / 1048576).toFixed(2) + ' MB · ready to upload' : dropDefault.sub;
}
$('file').addEventListener('change', showPicked);
['dragenter', 'dragover'].forEach(ev => $('uploadForm').addEventListener(ev, e => { e.preventDefault(); $('uploadForm').classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => $('uploadForm').addEventListener(ev, e => { e.preventDefault(); $('uploadForm').classList.remove('over'); }));
$('uploadForm').addEventListener('drop', e => { if (e.dataTransfer.files.length) { $('file').files = e.dataTransfer.files; showPicked(); } });
$('uploadForm').addEventListener('reset', () => setTimeout(showPicked));
$('uploadForm').onsubmit = async e => {
  e.preventDefault();
  const f = $('file').files[0]; if (!f) return;
  if (f.size > 10485760) { toast('Maximum file size is 10 MB'); return; }
  const btn = $('uploadBtn'); btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const r = await api('/api/documents/uploads', { method: 'POST', body: JSON.stringify({ filename: f.name }) });
    // The file goes to Atlas (same address as this page), which stores it in S3: no S3 CORS setup is needed.
    const up = await fetch(r.upload_path, { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: f });
    if (!up.ok) { const d = await up.json().catch(() => ({})); throw Error(d.detail || 'Upload failed (' + up.status + ')'); }
    btn.textContent = 'Indexing…'; toast('Uploaded. Processing document now…');
    loadDocs();
    await api('/api/documents/complete', { method: 'POST', body: JSON.stringify({ document_id: r.document_id }) });
    toast('Document indexed. You can ask questions about it now.');
    $('uploadForm').reset();
  } catch (err) { toast(err.message); }
  finally { btn.disabled = false; btn.textContent = 'Upload document'; loadDocs(); }
};

/* ---------- Approvals (admin workflow) ---------- */
async function loadApprovals() {
  const root = $('approvalList'); root.textContent = 'Loading approvals...';
  try {
    const response = await api('/api/approvals');
    if (!Array.isArray(response.approvals)) throw Error('Unexpected API response: approvals list missing');
    const approvals = response.approvals;
    const pending = approvals.filter(a => String(a.status).trim().toLowerCase() === 'pending').length;
    $('approvalSummary').textContent = `${pending} pending · ${approvals.length} total (most recent 100)`;
    root.replaceChildren();
    if (!approvals.length) { root.textContent = 'No approval requests yet. Try asking the chatbot to delete a document.'; return; }
    for (const a of approvals) {
      const card = el('article', 'approval-card ' + String(a.status).trim().toLowerCase());
      const header = el('div', 'approval-header');
      header.append(el('strong', null, a.question), el('span', 'approval-status ' + a.status, a.status));
      card.append(header, el('p', null, `Requested by ${a.requester} · ${new Date(a.created_at).toLocaleString()}`));
      if (a.action === 'delete') card.append(el('p', 'target', a.document_title ? `Will permanently delete: ${a.document_title}` : 'Delete request: no document identified, so nothing can be deleted'));
      if (String(a.status).trim().toLowerCase() === 'pending') {
        const actions = el('div', 'approval-actions');
        for (const [label, approve] of [['Approve', true], ['Reject', false]]) {
          const button = el('button', approve ? 'approve-button' : 'reject-button', label);
          button.onclick = async () => {
            const deletes = approve && a.action === 'delete' && a.document_id;
            const warn = deletes ? `Approve and permanently delete "${a.document_title}"? This removes the file and its search data and cannot be undone.`
              : `Are you sure you want to ${approve ? 'approve' : 'reject'} this request? This records a decision only and does not execute the requested action.`;
            if (!confirm(warn)) return;
            button.disabled = true;
            try { const r = await api(`/api/approvals/${encodeURIComponent(a.id)}/decision`, { method: 'POST', body: JSON.stringify({ approve }) }); toast(r.executed ? `Approved. "${a.document_title}" was deleted.` : `Request ${r.status}. No action executed.`); await loadApprovals(); }
            catch (e) { toast(e.message); button.disabled = false; }
          };
          actions.append(button);
        }
        card.append(actions);
      } else {
        card.append(el('p', null, `Reviewed by ${a.reviewer || 'unknown'} · ${a.reviewed_at ? new Date(a.reviewed_at).toLocaleString() : ''} · ${a.executed ? 'Document deleted' : 'No action executed'}`));
      }
      root.append(card);
    }
  } catch (e) { root.textContent = `Unable to load approvals: ${e.message}. Check /api/approvals and your Uvicorn terminal.`; console.error('Approvals loading failed:', e); }
}

{ const h = (location.hash || '').slice(1); const map = { documents: 'docs', docs: 'docs', approvals: 'approval', metrics: 'admin' }; if (map[h]) view(map[h]); }
