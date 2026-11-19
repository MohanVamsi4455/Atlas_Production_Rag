"""Test chat models (Groq first, Gemini as failover) and Gemini embeddings.

Run from the project root:
    python test/test.py              # test the failover chat + embeddings
    python test/test.py --all        # test EVERY chat model one by one
    python test/test.py --ask        # type your own questions: shows embedding + chat answer

Needs in .env: GROQ_API_KEY, GROQ_CHAT_MODELS, CHAT_PROVIDER_ORDER,
               GEMINI_API_KEY, GEMINI_CHAT_MODELS, GEMINI_EMBED_MODEL, EMBED_DIM
"""
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Windows consoles default to cp1252 and crash on characters models like to use (e.g. non-breaking hyphens).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_KEY = os.environ.get("GEMINI_API_KEY")  # used for embeddings (and Gemini chat failover)
GROQ_KEY = os.environ.get("GROQ_API_KEY")
EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))
TIMEOUT_MS = 45_000  # give up on a slow model after 45 s and move to the next one


def _models(var: str) -> list[str]:
    return [m.strip() for m in os.getenv(var, "").split(",") if m.strip()]


# Chat plan: providers in CHAT_PROVIDER_ORDER (groq first), each with its own model list.
PROVIDER_MODELS = {"groq": _models("GROQ_CHAT_MODELS"), "gemini": _models("GEMINI_CHAT_MODELS")}
PROVIDER_ORDER = [p.strip() for p in os.getenv("CHAT_PROVIDER_ORDER", "groq,gemini").split(",") if p.strip()]
PLAN = [
    (p, m)
    for p in PROVIDER_ORDER
    if p in PROVIDER_MODELS and (p != "groq" or GROQ_KEY) and (p != "gemini" or API_KEY)
    for m in PROVIDER_MODELS[p]
]

if not API_KEY:
    sys.exit("GEMINI_API_KEY is missing from .env (needed for embeddings)")
if not PLAN:
    sys.exit("No chat models to try: check GROQ_API_KEY / GROQ_CHAT_MODELS / GEMINI_CHAT_MODELS in .env")

from openai import OpenAI  # noqa: E402  (Groq exposes an OpenAI-compatible API)

groq = OpenAI(api_key=GROQ_KEY or "unused", base_url="https://api.groq.com/openai/v1", timeout=30, max_retries=0)

# One attempt per model: our own loop does the failover, so no hidden SDK retries.
client = genai.Client(
    api_key=API_KEY,
    http_options=types.HttpOptions(
        timeout=TIMEOUT_MS,
        retry_options=types.HttpRetryOptions(attempts=1),
    ),
)


def _call(provider: str, model: str, prompt: str, system: str) -> str:
    """One chat call to one model. Returns the reply text (may be empty)."""
    if provider == "groq":
        r = groq.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return (r.choices[0].message.content or "").strip()
    r = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=1024),
    )
    return (r.text or "").strip()


def chat(prompt: str, system: str = "You are a helpful assistant.") -> tuple[str, str]:
    """Try every model in PLAN in order (Groq first, then Gemini); return (reply, 'provider/model')."""
    errors = []
    for provider, model in PLAN:
        try:
            text = _call(provider, model, prompt, system)
            if text:
                return text, f"{provider}/{model}"
            errors.append(f"{provider}/{model}: empty reply")
        except Exception as e:  # quota, 429, 503, 504, 404 ...
            errors.append(f"{provider}/{model}: {str(e)[:70]}".replace("\n", " "))
    raise RuntimeError("All chat models failed:\n  " + "\n  ".join(errors))


def embed(texts: list[str]) -> list[list[float]]:
    """Embed several texts in ONE request (saves your 1,000/day request quota)."""
    resp = client.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
    )
    return [e.values for e in resp.embeddings]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb)


def test_each_model() -> None:
    print("== Every chat model ==")
    for provider, model in PLAN:
        name = f"{provider}/{model}"
        t = time.time()
        try:
            text = _call(provider, model, "Reply with the single word: ok", "You are a helpful assistant.")
            print(f"  OK    {name:36} {time.time() - t:4.1f}s  {text[:20]!r}")
        except Exception as e:
            print(f"  FAIL  {name:36} {time.time() - t:4.1f}s  {str(e)[:60]}".replace("\n", " "))


def test_chat() -> None:
    print("== Chat (failover) ==")
    t = time.time()
    reply, used = chat("In one sentence, what is retrieval-augmented generation?")
    print(f"  model used: {used}  ({time.time() - t:.1f}s)")
    print(f"  reply: {reply}")


def test_embeddings() -> None:
    print("== Embeddings ==")
    texts = ["annual leave policy", "how many vacation days do employees get", "best pizza in town"]
    t = time.time()
    vecs = embed(texts)
    print(f"  model: {EMBED_MODEL}  vectors: {len(vecs)}  dimension: {len(vecs[0])}  ({time.time() - t:.1f}s)")
    assert len(vecs[0]) == EMBED_DIM, f"expected {EMBED_DIM} dimensions"
    print(f"  similarity leave vs vacation: {cosine(vecs[0], vecs[1]):.3f}  (should be HIGH)")
    print(f"  similarity leave vs pizza:    {cosine(vecs[0], vecs[2]):.3f}  (should be LOWER)")


def ask_loop() -> None:
    """Type your own questions: shows the embedding and the chat answer for each one."""
    print("Type a question and press Enter (empty line or 'exit' to quit).\n")
    history = []  # embeddings of earlier questions, to compare similarity
    while True:
        try:
            q = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in {"exit", "quit"}:
            break

        try:
            t = time.time()
            vec = embed([q])[0]
            print(f"  [embedding] {EMBED_MODEL}: {len(vec)} dims, first 5 = "
                  f"{[round(v, 4) for v in vec[:5]]}  ({time.time() - t:.1f}s)")
            if history:
                best = max(history, key=lambda h: cosine(vec, h[1]))
                print(f"  [embedding] most similar earlier question: {best[0]!r} "
                      f"(similarity {cosine(vec, best[1]):.3f})")
            history.append((q, vec))
        except Exception as e:
            print(f"  [embedding] FAILED: {str(e)[:120]}")

        try:
            t = time.time()
            reply, used = chat(q)
            print(f"  [chat] {used} ({time.time() - t:.1f}s)")
            print(f"\nAI: {reply}\n")
        except Exception as e:
            print(f"  [chat] FAILED: {str(e)[:400]}\n")


if __name__ == "__main__":
    if "--ask" in sys.argv:
        ask_loop()
        sys.exit(0)
    if "--all" in sys.argv:
        test_each_model()
    test_chat()
    test_embeddings()
    print("\nAll tests passed.")
