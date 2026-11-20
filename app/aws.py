"""Amazon S3 helpers: document storage (upload, download, delete, presigned URL)."""
import re
import uuid
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings
from app.observability import trace


@lru_cache
def s3_client():
    """One shared S3 client. Empty keys in .env -> boto3's default credential chain."""
    s = settings()
    return boto3.client(
        's3',
        region_name=s.aws_region,
        aws_access_key_id=s.aws_access_key_id or None,
        aws_secret_access_key=s.aws_secret_access_key or None,
        config=Config(signature_version='s3v4', retries={'max_attempts': 3, 'mode': 'standard'}),
    )


def _bucket() -> str:
    bucket = settings().s3_bucket
    if not bucket:
        raise RuntimeError('S3_BUCKET is not set in .env')
    return bucket


def make_key(tenant_id: str, filename: str) -> str:
    """Build a unique, safe object key: <tenant>/<uuid>/<filename> (stored in documents.s3_key)."""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', filename).strip('._') or 'file'
    return f'{tenant_id}/{uuid.uuid4()}/{safe}'


@trace('s3.upload', kind='tool')
def upload_bytes(key: str, data: bytes, content_type: str = 'application/octet-stream') -> str:
    """Store bytes at key (server-side encrypted). Returns the key."""
    s3_client().put_object(
        Bucket=_bucket(), Key=key, Body=data, ContentType=content_type,
        ServerSideEncryption='AES256',
    )
    return key


@trace('s3.download', kind='tool')
def download_bytes(key: str) -> bytes:
    return s3_client().get_object(Bucket=_bucket(), Key=key)['Body'].read()


@trace('s3.delete', kind='tool')
def delete_object(key: str) -> None:
    s3_client().delete_object(Bucket=_bucket(), Key=key)


def presigned_put_url(key: str, expires: int = 300) -> str:
    """Temporary upload link so the browser can PUT the file straight to S3."""
    return s3_client().generate_presigned_url(
        'put_object', Params={'Bucket': _bucket(), 'Key': key, 'ContentType': 'application/octet-stream'},
        ExpiresIn=expires)


def get_object_bytes(key: str, max_bytes: int) -> bytes:
    """Download an object, refusing anything larger than max_bytes."""
    obj = s3_client().get_object(Bucket=_bucket(), Key=key)
    if obj['ContentLength'] > max_bytes:
        raise ValueError('Upload too large')
    data = obj['Body'].read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('Upload too large')
    return data


_VIEW_TYPES = {'.pdf': 'application/pdf', '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8'}


def presigned_url(key: str, expires: int = 900, filename: str | None = None) -> str:
    """Temporary link to view a stored file (default 15 minutes).

    PDF/TXT/MD open inline in the browser (uploads are stored as octet-stream, so the type is set here);
    other types download under their original filename.
    """
    from pathlib import Path
    from urllib.parse import quote
    name = filename or Path(key).name
    suffix = Path(name).suffix.lower()
    params = {'Bucket': _bucket(), 'Key': key}
    inline = suffix in _VIEW_TYPES
    params['ResponseContentDisposition'] = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}"
    if inline:
        params['ResponseContentType'] = _VIEW_TYPES[suffix]
    return s3_client().generate_presigned_url('get_object', Params=params, ExpiresIn=expires)


def check_connection() -> None:
    """Raise if the bucket is unreachable or the credentials are wrong."""
    try:
        s3_client().head_bucket(Bucket=_bucket())
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '')
        raise RuntimeError(f'S3 check failed for bucket {_bucket()!r}: {code}') from exc
