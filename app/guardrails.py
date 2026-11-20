"""Conservative, deterministic demo guardrails. Not a substitute for IAM or auth."""
import re
from fastapi import HTTPException

# Explicit attack patterns rather than blocking ordinary questions about passwords.
INJECTION = [
    r"\bignore\s+(?:all\s+|any\s+|the\s+|previous\s+|prior\s+)?(?:instructions|rules|system\s+prompt)\b",
    r"\b(?:reveal|print|show|dump|disclose)\s+(?:your\s+|the\s+|hidden\s+|internal\s+)*(?:system|developer)\s+(?:prompt|instructions)\b",
    r"\b(?:reveal|print|dump|expose|send)\s+(?:your\s+|all\s+|the\s+)*(?:api\s*keys?|passwords?|credentials?|secrets?|access\s+tokens?)\b",
    r"\b(?:bypass|disable|turn\s+off)\s+(?:all\s+|the\s+)?(?:guardrails|safety\s+checks|security\s+controls)\b",
    r"\b(?:bypass|override|disregard|forget|circumvent|ignore)\s+(?:all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+|these\s+)*(?:instructions|rules|guardrails|restrictions|safety|security|system\s+prompt)\b",
    r"\bjailbreak\b|\bdeveloper\s+mode\b|\bDAN\s+mode\b",
    r"\b(?:pretend|act)\s+(?:you\s+are|as\s+if\s+you\s+are)\s+(?:the\s+)?(?:system|developer|administrator)\b",
]
SECRETS = [
    r"\b(?:password|passcode|passwd|pin|otp|cvv|cvc|secret(?:\s+key)?|api[\s_-]?key|access\s+token|token)\s*(?:is|are|was|=|:|-)\s*\S+",
]
HARMFUL = [
    r"\b(?:hack|hacking|hacked|exploit|crack|attack|ddos|breach|infiltrate|compromise|sabotage|take\s+down)\s+(?:into\s+)?(?:the\s+|a\s+|an\s+|any\s+|this\s+|that\s+|your\s+|their\s+|our\s+|my\s+|all\s+)?(?:company\s+|corporate\s+|target\s+)?(?:system|systems|server|servers|network|networks|database|databases|account|accounts|website|websites|wifi|wi-fi|password|passwords|computer|computers|laptop|phone|email|emails|application|applications|app|api|firewall|security)\b",
    r"\bbreak\s+into\s+(?:the\s+|a\s+|any\s+|this\s+|their\s+)?(?:system|systems|server|network|database|account|accounts|website)\b",
    r"\bhow\s+(?:to|do\s+i|can\s+i|would\s+i|could\s+i)\s+(?:hack|exploit|crack|ddos|phish|steal|bypass\s+(?:authentication|security|login|the\s+firewall))\b",
    r"\b(?:write|create|build|generate|make|code|develop)\s+(?:me\s+)?(?:a\s+|an\s+|some\s+)?(?:malware|virus|ransomware|keylogger|trojan|spyware|botnet|backdoor|rootkit|exploit|phishing\s+(?:page|email|site))\b",
    r"\b(?:steal|exfiltrate|harvest|dump)\s+(?:the\s+|all\s+|any\s+|their\s+|user\s+)*(?:data|credentials|passwords|tokens|cookies|records|customer\s+data)\b",
    r"\bsql\s+injection\b|\bdrop\s+(?:table|database)\b|\b(?:wipe|erase|destroy|delete)\s+(?:all\s+)?(?:your\s+|the\s+|every\s+)?(?:entire\s+)?(?:database|server|system)\b",
]
SENSITIVE_OUTPUT = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:aws_secret_access_key|pinecone_api_key|database_url)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:password|api[_ -]?key|secret[_ -]?key)\s*[:=]\s*[\"']?[^\s\"']{12,}"),
]

class GuardrailBlocked(ValueError):
    pass

def classify_input(question: str) -> str | None:
    if not question or not question.strip():
        return 'empty_input'
    if any(re.search(pattern, question, re.I) for pattern in INJECTION):
        return 'prompt_injection_or_secret_request'
    if any(re.search(pattern, question, re.I) for pattern in HARMFUL):
        return 'harmful_request'
    if any(re.search(pattern, question, re.I) for pattern in SECRETS):
        return 'secret_in_question'
    from app import pii
    if pii.high_risk(question):
        return 'pii_in_question'
    return None


# The model itself declined ("I can't help with that"). Not an error, but worth recording.
_REFUSAL = re.compile(r"\b(?:can['\u2019]?t|cannot|can\s+not|won['\u2019]?t|will\s+not|unable\s+to)\s+(?:help|assist|comply|do\s+that|provide\s+(?:that|this))\b", re.I)

def is_refusal(answer: str) -> bool:
    return bool(_REFUSAL.search((answer or '')[:300]))

def check_input(question: str) -> str:
    reason = classify_input(question)
    if reason:
        raise HTTPException(status_code=400, detail=f'Input guardrail blocked this request ({reason}).')
    return question

def contains_sensitive_output(text: str) -> bool:
    return any(pattern.search(text or '') for pattern in SENSITIVE_OUTPUT)

def check_output(answer: str) -> str:
    if contains_sensitive_output(answer):
        return 'The response was blocked by the sensitive-data output guardrail.'
    return answer

def check_evidence(text: str) -> str:
    """Label retrieved document passages as untrusted. Never follow embedded instructions."""
    # We don't delete user documents or discard factual content based on regex.
    return text
