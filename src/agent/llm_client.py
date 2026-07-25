"""OpenAI-compatible client for Groq (primary) / Cerebras (fallback), per
CLAUDE.md: FALLBACK_API_KEY may be blank, in which case only Groq is tried.

The watchdog is the client's own request timeout (no threads/signals needed) --
LLM_TIMEOUT_SECONDS from .env, default 15s, matching CLAUDE.md's ~15s figure.
"""
import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "15"))
# gpt-oss-120b burns completion tokens on hidden reasoning before `content` --
# too-tight max_tokens truncates mid-reasoning and leaves content=None (Phase 0
# finding, see scripts/llm_smoke.py).
MAX_TOKENS = 1000

# EnergyPlus steps through simulated hours far faster than real time, so 168
# hourly decisions can fire within seconds of each other -- straight into free-
# tier RPM caps (observed: Groq 429 "Requests per minute limit exceeded" during
# a 2-day test run). This is a real-time floor between calls to the SAME
# provider, not a decision-cadence change (that stays hourly-simulated per
# CLAUDE.md). Default keeps well under a 30 RPM tier.
# ponytail: single global interval, not a true token-bucket / per-provider RPM
# config -- fine for one building's hourly cadence; revisit if providers expose
# different limits worth tuning independently.
MIN_CALL_INTERVAL_SECONDS = float(os.environ.get("LLM_MIN_CALL_INTERVAL_SECONDS", "2.5"))
_last_call_time: dict[str, float] = {}


def _throttle(provider_name: str) -> None:
    last = _last_call_time.get(provider_name)
    if last is not None:
        wait = MIN_CALL_INTERVAL_SECONDS - (time.perf_counter() - last)
        if wait > 0:
            time.sleep(wait)
    _last_call_time[provider_name] = time.perf_counter()


class LLMUnavailable(Exception):
    """Raised when no provider is configured, or every configured provider failed."""


@dataclass
class Provider:
    name: str
    base_url: str | None
    api_key: str
    model: str


def _load_providers() -> list[Provider]:
    providers = []
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        providers.append(
            Provider(
                name="groq",
                base_url="https://api.groq.com/openai/v1",
                api_key=groq_key,
                model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            )
        )
    fallback_key = os.environ.get("FALLBACK_API_KEY", "").strip()
    if fallback_key:
        providers.append(
            Provider(
                name="cerebras",
                base_url=os.environ.get("FALLBACK_BASE_URL", "https://api.cerebras.ai/v1"),
                api_key=fallback_key,
                model=os.environ.get("FALLBACK_MODEL", "gpt-oss-120b"),
            )
        )
    return providers


def complete(messages: list[dict], timeout: float = TIMEOUT_SECONDS) -> tuple[str, str, float]:
    """Tries each configured provider in order. Returns (text, provider_name,
    latency_ms) from the first that succeeds. Raises LLMUnavailable if none are
    configured or all fail -- the caller (src/agent/safety.py) treats that exactly
    like a timeout and falls back to the rule-based controller."""
    providers = _load_providers()
    if not providers:
        raise LLMUnavailable("no LLM provider configured (GROQ_API_KEY and FALLBACK_API_KEY both blank)")

    last_error: Exception | None = None
    for provider in providers:
        _throttle(provider.name)
        client = OpenAI(api_key=provider.api_key, base_url=provider.base_url, timeout=timeout, max_retries=0)
        start = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=provider.model,
                messages=messages,
                max_tokens=MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            latency_ms = (time.perf_counter() - start) * 1000
            text = resp.choices[0].message.content
            if not text:
                raise ValueError("empty response content (reasoning likely truncated max_tokens)")
            return text, provider.name, latency_ms
        except Exception as exc:  # noqa: BLE001 -- any provider failure just moves to the next one
            last_error = exc
            continue

    raise LLMUnavailable(f"all providers failed; last error: {last_error}")


if __name__ == "__main__":
    text, provider, latency_ms = complete(
        [{"role": "user", "content": 'Reply with exactly this json: {"ok": true}'}]
    )
    print(f"provider={provider} latency={latency_ms:.0f}ms reply={text!r}")
