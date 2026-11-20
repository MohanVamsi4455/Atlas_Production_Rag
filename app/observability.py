"""Privacy-first GlassFlow Rius tracing.

* No telemetry is sent unless RIUS_API_KEY is set and RIUS_DISABLED is false.
* Prompts, documents and answers are NOT recorded unless RIUS_CAPTURE_CONTENT=true.
* Tracing can never break the app: every failure is logged and ignored.

Call initialize() once at startup (e.g. in app/main.py) before handling requests.
"""
import logging
from contextlib import nullcontext

from app.config import settings

log = logging.getLogger(__name__)
_enabled = False
_client = None


def initialize(**init_kwargs) -> bool:
    """Start Rius. Returns True if tracing is active. Extra kwargs go to rius.init (e.g. span_exporter)."""
    global _enabled, _client
    if _enabled:
        return True
    s = settings()
    if s.rius_disabled or not (s.rius_api_key or init_kwargs.get('span_exporter')):
        log.info('Rius observability disabled (no key or kill switch)')
        return False
    try:
        import rius
        _client = rius.init(api_key=s.rius_api_key or None, service_name=s.rius_service_name,
                  capture_content=s.rius_capture_content, **init_kwargs)
        _enabled = True
        log.info('Rius observability enabled (capture_content=%s)', s.rius_capture_content)
    except Exception:
        log.exception('Rius initialization failed; continuing without telemetry')
    return _enabled


def enabled() -> bool:
    return _enabled


def flush() -> None:
    """Send buffered spans now (call on app shutdown so the last traces are not lost)."""
    if _client:
        try:
            _client.flush()
        except Exception:
            log.exception('Rius flush failed')


def trace(name: str, kind: str = 'chain'):
    """Decorate a sync/async function so each call is a span.

    kind: chain | tool | retriever | embedding | agent | reranker.
    Arguments and return values are exported only if RIUS_CAPTURE_CONTENT=true.
    """
    def decorate(fn):
        try:
            import rius
            capture = settings().rius_capture_content
            return rius.observe(name=name, kind=rius.SpanKind[kind.upper()],
                                capture_input=capture, capture_output=capture)(fn)
        except Exception:
            return fn
    return decorate


def generation(provider: str, model: str):
    """Context manager: an LLM span for one model call (no-op when tracing is off)."""
    if not _enabled:
        return nullcontext()
    try:
        import rius
        return rius.start_as_current_generation(name=f'{provider}/{model}', model=model, provider=provider)
    except Exception:
        return nullcontext()
