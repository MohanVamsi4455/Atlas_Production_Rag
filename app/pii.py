"""PII detection and masking (deterministic, no external service).

Detects: credit/debit cards (Luhn), Aadhaar (Verhoeff), PAN, US SSN, e-mail addresses, phone numbers.
Checksums keep ordinary numbers (years, amounts, order ids) from being flagged.

Used to (1) block questions that contain high-risk IDs, (2) mask PII in the evidence sent to the LLM,
in source excerpts and in answers, and (3) keep raw values out of the audit log.
Set PII_MODE=off in .env to disable everything here.
"""
import re

from app.config import settings

HIGH_RISK = {'CARD', 'AADHAAR', 'PAN', 'SSN'}  # a question containing one of these is blocked

# ---------------- checksums ----------------
def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if flip:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        flip = not flip
    return total % 10 == 0

_D = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],
      [5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],[8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_P = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
      [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]

def _verhoeff(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0

def _digits(s: str) -> str:
    return re.sub(r'\D', '', s)

# ---------------- detectors (checked in this order) ----------------
# (type, regex, validator or None)
_DETECTORS = [
    ('CARD',    re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)'), lambda s: 13 <= len(_digits(s)) <= 19 and _luhn(_digits(s))),
    ('AADHAAR', re.compile(r'(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)'), lambda s: _verhoeff(_digits(s))),
    ('PAN',     re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b'), None),
    ('SSN',     re.compile(r'\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b'), None),
    ('EMAIL',   re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'), None),
    ('PHONE',   re.compile(r'(?<![\w+])(?:(?:\+|00)91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)'), None),
    ('PHONE',   re.compile(r'(?<![\w+])\+\d{1,3}[ -]?\(?\d{1,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}(?!\d)'), None),
    # any other 10-12 digit run that starts like a mobile number (6-9), however it is grouped or mistyped
    ('PHONE',   re.compile(r'(?<![\w.+-])[6-9]\d{9,11}(?![\w])'), None),
]


def enabled() -> bool:
    return settings().pii_mode.lower() != 'off'


def scan(text: str) -> list[tuple[str, int, int]]:
    """Non-overlapping PII matches as (type, start, end), in text order."""
    found, taken = [], []
    for kind, rx, ok in _DETECTORS:
        for m in rx.finditer(text or ''):
            s, e = m.span()
            if any(s < b and a < e for a, b in taken):
                continue
            if ok and not ok(m.group()):
                continue
            found.append((kind, s, e)); taken.append((s, e))
    return sorted(found, key=lambda x: x[1])


def redact(text: str) -> tuple[str, dict]:
    """Return (text with PII replaced by [TYPE REDACTED], {type: count}). A no-op when PII_MODE=off."""
    if not text or not enabled():
        return text, {}
    out, counts = text, {}
    for kind, s, e in reversed(scan(text)):
        out = out[:s] + f'[{kind} REDACTED]' + out[e:]
        counts[kind.lower()] = counts.get(kind.lower(), 0) + 1
    return out, counts


def high_risk(text: str) -> list[str]:
    """High-risk ID types present in the text (card, Aadhaar, PAN, SSN)."""
    if not enabled():
        return []
    return sorted({k for k, _, _ in scan(text) if k in HIGH_RISK})


def merge(into: dict, extra: dict) -> dict:
    for k, v in (extra or {}).items():
        into[k] = into.get(k, 0) + v
    return into


def preview(text: str, limit: int = 200) -> str:
    """Safe text for the audit log: PII masked, then shortened."""
    return redact(text or '')[0][:limit]
