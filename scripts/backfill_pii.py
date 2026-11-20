"""Scan documents uploaded before PII detection existed, and record which PII types each contains.

Usage (from project root): python scripts/backfill_pii.py
Safe to rerun: it only touches documents whose `pii` column is still empty (NULL).
Only counts are stored (e.g. {"email": 1, "phone": 1}), never the values themselves.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg.types.json import Jsonb  # noqa: E402

from app.aws import get_object_bytes  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import connection  # noqa: E402
from app.pii import merge, redact  # noqa: E402
from worker.ingest import parse  # noqa: E402


def main() -> int:
    with connection() as conn:
        docs = conn.execute("SELECT id,title,s3_key FROM documents WHERE pii IS NULL AND status='ready'").fetchall()
    print(f'{len(docs)} document(s) to scan')
    for d in docs:
        try:
            blob = get_object_bytes(d['s3_key'], settings().max_upload_bytes)
            found = {}
            for _, text in parse(blob, Path(d['s3_key']).suffix.lower()):
                merge(found, redact(text)[1])
        except Exception as exc:
            print(f"  SKIP {d['title']}: {type(exc).__name__}: {exc}")
            continue
        with connection() as conn:
            conn.execute('UPDATE documents SET pii=%s WHERE id=%s', (Jsonb(found), d['id']))
        print(f"  {d['title']}: {found or 'no personal data found'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
