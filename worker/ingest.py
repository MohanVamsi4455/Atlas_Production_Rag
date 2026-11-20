"""Ingestion: S3 object -> text -> chunks -> Gemini embeddings -> Pinecone.

Not run automatically: app/main.py calls process_s3_key() when the browser reports that the upload finished.
"""
import io
import logging
from pathlib import Path

from docx import Document
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from app.aws import get_object_bytes
from app.config import settings
from app.db import audit, connection
from app.llm import embed
from app.observability import initialize, trace
from app.pii import merge, redact
from app.vector import upsert_chunks

initialize()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('atlas.worker')

EMBED_BATCH = 50  # texts per Gemini embedding request


def parse(blob: bytes, suffix: str):
    """Return [(page_number, text)]."""
    if suffix == '.pdf':
        reader = PdfReader(io.BytesIO(blob))
        return [(i + 1, page.extract_text() or '') for i, page in enumerate(reader.pages)]
    if suffix == '.docx':
        doc = Document(io.BytesIO(blob))
        return [(1, '\n'.join(p.text for p in doc.paragraphs))]
    return [(1, blob.decode('utf-8'))]


def chunks(text, size=1200, overlap=180):
    """Split into ~size-character chunks with overlap, cutting at a space, never mid-word."""
    text = ' '.join(text.split())
    pos = 0
    while pos < len(text):
        end = min(pos + size, len(text))
        if end < len(text):
            cut = text.rfind(' ', pos + size // 2, end)
            end = cut if cut != -1 else end
        yield text[pos:end]
        if end >= len(text):
            break
        pos = max(end - overlap, pos + 1)


@trace('process_s3_key')
def process_s3_key(key):
    s = settings()
    with connection() as conn:
        doc = conn.execute('SELECT * FROM documents WHERE s3_key=%s FOR UPDATE', (key,)).fetchone()
        if not doc or doc['status'] == 'ready':
            return
        if doc['status'] not in ('pending', 'failed'):
            return
        conn.execute("UPDATE documents SET status='processing' WHERE id=%s", (doc['id'],))
    try:
        suffix = Path(key).suffix.lower()
        blob = get_object_bytes(key, s.max_upload_bytes)
        if suffix == '.pdf' and not blob.startswith(b'%PDF'):
            raise ValueError('Invalid PDF')
        pages = parse(blob, suffix)
        parts = [(page, part) for page, text in pages for part in chunks(text)]
        if not parts:
            raise ValueError('No extractable text (scanned PDFs need OCR)')
        texts = [part for _, part in parts]
        pii = {}
        for part in texts:
            merge(pii, redact(part)[1])
        vectors = []
        for i in range(0, len(texts), EMBED_BATCH):
            vectors.extend(embed(texts[i:i + EMBED_BATCH]))
        upsert_chunks(doc['tenant_id'], str(doc['id']), doc['title'], list(doc['allowed_groups']),
                      texts, vectors, version=doc['current_version'], owner_sub=doc['owner_sub'],
                      pages=[page for page, _ in parts])
        with connection() as conn:
            conn.execute("UPDATE documents SET status='ready', pii=%s WHERE id=%s", (Jsonb(pii), doc['id']))
            audit(conn, doc['owner_sub'], doc['tenant_id'], 'ingest_ready',
                  {'document_id': str(doc['id']), 'chunks': len(parts), 'pii_found': pii})
        log.info('Ingested document %s chunks=%d', doc['id'], len(parts))
    except Exception:
        log.exception('Ingestion failed for key %s', key)
        with connection() as conn:
            conn.execute("UPDATE documents SET status='failed' WHERE id=%s", (doc['id'],))
        raise
