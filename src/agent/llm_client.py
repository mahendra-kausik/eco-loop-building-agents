"""OpenAI-compatible client for Cerebras (preferred) / Groq, round-robined per
call. Either key may be blank; with both blank, complete() raises
LLMUnavailable and the safety supervisor falls straight through to the
rule-based schedule -- so the project still runs on either provider alone.

Provider ordering changed in Phase 4 (user-approved 2026-07-25): originally
"Groq primary, Cerebras fallback" on the assumption Groq had the better free
tier; measuring the published gpt-oss-120b quotas showed the opposite on the
limit that actually binds here -- Cerebras carries 1M tokens/day against
Groq's 200K, i.e. ~2.7 full 7-day runs/day vs ~0.5. See
_PROVIDER_MIN_INTERVAL below and ARCHITECTURE.md's "Rate limiting" section.

The watchdog is the client's own request timeout (no threads/signals needed) --
LLM_TIMEOUT_SECONDS from .env, default 15s.
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
# tier caps. This is a real-time floor between calls to the SAME provider, not
# a decision-cadence change (that stays once per simulated hour).
#
# Phase 4 correction: the original 2.5s default was sized for a requests-per-
# minute cap ("30 RPM tier"). A live run's 429s turned out to be a *tokens*-
# per-minute cap instead -- Groq's error body reads "tokens per minute (TPM):
# Limit 8000" for this model/org, and our requested budget (prompt + MAX_TOKENS
# reservation) runs ~2200-2700 tokens/call. 8000 / ~2500 ~= 3.2 calls/min
# sustainable -> ~19s between calls, not 2.5s. Raised the default accordingly;
# 2.5s was silently guaranteeing most calls would 429 once a TPM window filled.
# The two providers are bound by *different* limits, so one global interval
# necessarily over-throttles whichever is less constrained. Measured against
# each provider's published free-tier quota for gpt-oss-120b at this project's
# ~2.2K tokens/call:
#
#   groq      30 RPM (2.0s) |  8K TPM (16.5s)  -> TPM-bound, ~17s
#   cerebras   5 RPM (12.0s)| 30K TPM ( 4.4s)  -> RPM-bound, ~12s
#
# Note they invert: Groq has 6x the request headroom but a quarter of the token
# headroom. Daily budgets differ even more (Groq 200K TPD ~= 0.5 full 7-day
# runs; Cerebras 1M TPD ~= 2.7), which is why round-robin leans on Cerebras in
# practice. See ARCHITECTURE.md's "Rate limiting" section.
# ponytail: static per-provider floors, not a real token-bucket tracking actual
# usage -- fine while the prompt size is stable; a bucket only earns its keep if
# per-call token cost starts varying a lot.
_PROVIDER_MIN_INTERVAL = {"groq": 17.0, "cerebras": 12.0}
_DEFAULT_MIN_INTERVAL = 19.0  # unknown/self-hosted provider: assume the tightest

# Explicit override wins for every provider (set it for a paid tier with more
# headroom, or to force a uniform conservative floor). Unset -> per-provider.
_INTERVAL_OVERRIDE = os.environ.get("LLM_MIN_CALL_INTERVAL_SECONDS", "").strip()
MIN_CALL_INTERVAL_SECONDS = float(_INTERVAL_OVERRIDE) if _INTERVAL_OVERRIDE else None
_last_call_time: dict[str, float] = {}


def _min_interval(provider_name: str) -> float:
    if MIN_CALL_INTERVAL_SECONDS is not None:
        return MIN_CALL_INTERVAL_SECONDS
    return _PROVIDER_MIN_INTERVAL.get(provider_name, _DEFAULT_MIN_INTERVAL)

# Phase 4 (user decision, 2026-07-25): round-robin the starting provider each call
# instead of always trying Groq first and treating Cerebras as pure failover. A
# live 7-day run showed 95/168 decisions (57%) hitting Groq's free-tier RPM cap
# and falling back to the rule-based controller -- the LLM was only actually
# deciding 43% of the time. Alternating roughly doubles effective throughput at
# the same per-provider MIN_CALL_INTERVAL_SECONDS. Still fails over to the other
# configured provider within the same call if the rotated-to one errors, and
# still reduces to Groq-only rotation (a no-op) when FALLBACK_API_KEY is blank --
# the "works fully on one provider alone" guarantee is unchanged.
_rr_index = 0


def _throttle(provider_name: str) -> None:
    last = _last_call_time.get(provider_name)
    if last is not None:
        wait = _min_interval(provider_name) - (time.perf_counter() - last)
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
    """Cerebras first (5x Groq's daily token budget -- see module docstring).
    Order matters only as the round-robin starting point and the within-call
    failover order; either provider alone is a complete configuration.

    CEREBRAS_API_KEY is the preferred name now that this provider is no longer
    the "fallback"; FALLBACK_API_KEY is still honored so existing .env files
    keep working unchanged."""
    providers = []
    cerebras_key = (
        os.environ.get("CEREBRAS_API_KEY", "").strip()
        or os.environ.get("FALLBACK_API_KEY", "").strip()
    )
    if cerebras_key:
        providers.append(
            Provider(
                name="cerebras",
                base_url=os.environ.get("FALLBACK_BASE_URL", "https://api.cerebras.ai/v1"),
                api_key=cerebras_key,
                model=os.environ.get("FALLBACK_MODEL", "gpt-oss-120b"),
            )
        )
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
    return providers


def complete(messages: list[dict], timeout: float = TIMEOUT_SECONDS) -> tuple[str, str, float]:
    """Tries each configured provider in order. Returns (text, provider_name,
    latency_ms) from the first that succeeds. Raises LLMUnavailable if none are
    configured or all fail -- the caller (src/agent/safety.py) treats that exactly
    like a timeout and falls back to the rule-based controller."""
    providers = _load_providers()
    if not providers:
        raise LLMUnavailable("no LLM provider configured (GROQ_API_KEY and FALLBACK_API_KEY both blank)")

    global _rr_index
    rotate = _rr_index % len(providers)
    ordered_providers = providers[rotate:] + providers[:rotate]
    _rr_index += 1

    last_error: Exception | None = None
    for provider in ordered_providers:
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
