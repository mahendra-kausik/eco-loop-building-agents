"""Phase 0 smoke test: one chat call to each configured provider, timed.

Both providers speak the OpenAI-compatible chat completions API, so a single client
class handles both -- only base_url/api_key/model change. Run:
    python scripts/llm_smoke.py
"""
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

PROMPT = "Reply with exactly one word: OK"

# gpt-oss-120b is a reasoning model: it burns completion tokens on a hidden `reasoning`
# field before the final `content`. A tight max_tokens truncates mid-reasoning and
# leaves content=None -- give it real headroom, and always read only `.content`.
MAX_TOKENS = 300


def call(label: str, base_url: str | None, api_key: str | None, model: str) -> None:
    if not api_key:
        print(f"[{label}] SKIPPED — no API key set")
        return
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    start = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROMPT}],
            max_tokens=MAX_TOKENS,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        reply = resp.choices[0].message.content
        usage = resp.usage
        reasoning_tok = getattr(usage.completion_tokens_details, "reasoning_tokens", None) if usage else None
        print(
            f"[{label}] model={model} latency={latency_ms:.0f}ms reply={reply!r} "
            f"completion_tokens={getattr(usage, 'completion_tokens', '?')} reasoning_tokens={reasoning_tok}"
        )
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000
        print(f"[{label}] FAILED after {latency_ms:.0f}ms — {type(e).__name__}: {e}")


if __name__ == "__main__":
    call(
        "Groq",
        "https://api.groq.com/openai/v1",
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    )
    call(
        "Cerebras (preferred)",
        os.environ.get("FALLBACK_BASE_URL", "https://api.cerebras.ai/v1"),
        os.environ.get("CEREBRAS_API_KEY") or os.environ.get("FALLBACK_API_KEY"),
        os.environ.get("FALLBACK_MODEL", "gpt-oss-120b"),
    )
