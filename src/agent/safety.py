"""The safety supervisor wrapping the LLM (this is the 30% System Integration
criterion, per CLAUDE.md): schema-validate the LLM's JSON, clamp to hard ranges,
retry once on invalid output, and fall back to the deterministic rule-based
controller on ANY failure (timeout, error, invalid-after-retry) so the simulation
never dies because of the agent. Every decision is logged to
results/decision_log.jsonl.
"""
import json
import os
import time
from typing import Optional

from pydantic import BaseModel, ValidationError

from src.agent.fallback import clamp_fan_available, clamp_setpoints, fallback_controller
from src.agent.llm_client import complete
from src.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from src.tools.building_tools import (
    MAX_ERROR_CHARS,
    get_building_state,
    get_forecast_context,
    get_recent_errors,
    is_occupied_hour,
)


class SetpointDecision(BaseModel):
    heating_setpoint_c: float
    cooling_setpoint_c: float
    fan_off: bool = False  # request only -- clamp_fan_available decides if it's honored
    reason: str = ""


# Anti-thrash: cap hour-to-hour setpoint movement so the LLM can't oscillate (or
# alternate with a fallback hour landing on a very different value). Exempted
# across an occupied<->unoccupied transition, where a real step is the point.
MAX_SETPOINT_STEP_C = 1.5

# Occupied cooling above this measured out of band: across a 7-day run, PMV
# breached +0.5 in 7 of 40 zone-hours spent above 25.5 C, versus 1 of 205 at
# 25.5 C. clamp_setpoints' 28 C ceiling is the safe *equipment* range; this is
# the tighter *comfort* bound the prompt already asks for, enforced rather than
# suggested.
COMFORT_MAX_COOLING_C = 25.5


def _apply_comfort_guard(heating_c: float, cooling_c: float, occupied: bool) -> tuple[float, float, bool]:
    """Caps occupied cooling at COMFORT_MAX_COOLING_C, re-clamping to keep the
    heating<=cooling-1 invariant. Returns (heating_c, cooling_c, was_capped).
    No-op unoccupied (30 C ceiling there is already comfort-irrelevant) or
    already inside the band."""
    if not occupied or cooling_c <= COMFORT_MAX_COOLING_C:
        return heating_c, cooling_c, False
    heating_c, cooling_c = clamp_setpoints(heating_c, COMFORT_MAX_COOLING_C, occupied)
    return heating_c, cooling_c, True


def _validate(raw_reply: str) -> SetpointDecision:
    """Raises ValueError/pydantic.ValidationError on any invalid JSON or schema
    violation -- the caller decides whether to retry or fall back."""
    parsed = json.loads(raw_reply)  # json.JSONDecodeError is a ValueError subclass
    return SetpointDecision.model_validate(parsed)


def _ask_llm(state: dict, forecast: dict, recent_errors: list[str]) -> tuple[SetpointDecision, dict]:
    """One call, one retry on invalid output (with the validation error fed back),
    per CLAUDE.md. Raises on final failure -- caller falls back."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(state, forecast, recent_errors)},
    ]
    meta = {"provider": None, "latency_ms": None, "raw_reply": None, "retried": False}

    for attempt in range(2):
        text, provider, latency_ms = complete(messages)
        meta.update(provider=provider, latency_ms=latency_ms, raw_reply=text)
        try:
            return _validate(text), meta
        except (json.JSONDecodeError, ValidationError) as exc:
            if attempt == 0:
                meta["retried"] = True
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {"role": "user", "content": f"Invalid: {exc}. Reply with ONLY the corrected JSON object."}
                )
                continue
            raise

    raise AssertionError("unreachable")  # loop always returns or raises by attempt 1


def make_llm_controller(log_path: Optional[str] = None, run_dir: Optional[str] = None):
    """Returns a Controller closure (see src/simulation/runner.py's Controller
    type) that logs every decision to `log_path` (default
    results/decision_log.jsonl)."""
    log_path = log_path or os.path.join(os.path.dirname(__file__), "..", "..", "results", "decision_log.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    # Mutable closure state: last setpoints actually applied + the occupancy
    # state they were applied under (anti-thrash rate limit), plus a running
    # peak-demand max (get_building_state only ever sees one row, so it can't
    # track "so far" itself -- see that function's peak_kw_so_far docstring).
    last = {"heating": None, "cooling": None, "occupied": None, "peak_kw_so_far": 0.0}

    def controller(row: Optional[dict], day_of_year: int, hour: int, day_of_week: int) -> tuple[float, float, float]:
        # Predicted for the hour being decided FOR, not read reactively off the
        # last completed row -- see fallback.fallback_controller's docstring for
        # the bug this fixes (occupancy onset hour got the unoccupied clamp).
        occupied = is_occupied_hour(hour, day_of_week)
        if row is not None:
            last["peak_kw_so_far"] = max(last["peak_kw_so_far"], float(row.get("electricity_kwh_this_hour", 0.0)))
        state = get_building_state(row, peak_kw_so_far=last["peak_kw_so_far"] if row is not None else None)
        forecast = get_forecast_context(day_of_year, hour, day_of_week)
        recent_errors = get_recent_errors(run_dir=run_dir)

        entry = {
            "day_of_year": day_of_year, "hour": hour, "occupied": occupied,
            "provider": None, "latency_ms": None, "raw_reply": None, "retried": False,
            "requested_heating_c": None, "requested_cooling_c": None,
            "heating_c": None, "cooling_c": None, "fan_available": None,
            "was_clamped": None, "was_rate_limited": False, "was_comfort_capped": False,
            "fallback_used": False, "error": None,
        }

        try:
            decision, meta = _ask_llm(state, forecast, recent_errors)
            entry.update(provider=meta["provider"], latency_ms=meta["latency_ms"],
                         raw_reply=meta["raw_reply"], retried=meta["retried"], reason=decision.reason,
                         requested_heating_c=decision.heating_setpoint_c,
                         requested_cooling_c=decision.cooling_setpoint_c)
            heating_c, cooling_c = clamp_setpoints(decision.heating_setpoint_c, decision.cooling_setpoint_c, occupied)
            entry["was_clamped"] = (heating_c, cooling_c) != (decision.heating_setpoint_c, decision.cooling_setpoint_c)
            fan_available = clamp_fan_available(
                decision.fan_off, hour, day_of_week, state.get("max_zone_temp_c"), cooling_c
            )
        except Exception as exc:  # noqa: BLE001 -- any failure (timeout, invalid-after-retry, etc.) falls back
            entry["fallback_used"] = True
            # Truncated to MAX_ERROR_CHARS -- raw provider error bodies (a 429's
            # message includes the account's org id) go straight to a logged
            # file here, not just back into a prompt like get_recent_errors'
            # callers; same constant, same reason to clip.
            raw = f"{type(exc).__name__}: {exc}"
            entry["error"] = raw if len(raw) <= MAX_ERROR_CHARS else raw[: MAX_ERROR_CHARS - 3] + "..."
            heating_c, cooling_c, fan_available = fallback_controller(row, day_of_year, hour, day_of_week)

        # Anti-thrash rate limit, skipped across an occupancy transition (a real
        # step is the point there). Re-clamp afterward -- capping movement can't
        # itself produce an out-of-range or deadband-violating pair, but it's a
        # cheap guarantee to keep rather than assume.
        #
        # Logged as its OWN flag, not folded into was_clamped: this stage runs
        # after the clamp, so a value the clamp passed through untouched can
        # still be moved here. Reporting that as was_clamped=False with a
        # changed setpoint made the audit trail read as if the LLM's number was
        # applied verbatim when it wasn't (24 such entries in one 7-day run
        # before this was split out). requested_* records what the LLM actually
        # asked for, so the log shows the full ask -> clamp -> rate-limit chain.
        pre_rate_limit = (heating_c, cooling_c)
        if last["heating"] is not None and last["occupied"] == occupied:
            heating_c = min(max(heating_c, last["heating"] - MAX_SETPOINT_STEP_C), last["heating"] + MAX_SETPOINT_STEP_C)
            cooling_c = min(max(cooling_c, last["cooling"] - MAX_SETPOINT_STEP_C), last["cooling"] + MAX_SETPOINT_STEP_C)
            heating_c, cooling_c = clamp_setpoints(heating_c, cooling_c, occupied)
        entry["was_rate_limited"] = (heating_c, cooling_c) != pre_rate_limit
        last.update(heating=heating_c, cooling=cooling_c, occupied=occupied)

        # Comfort guard: last stage, after clamp and rate-limit, so it's
        # authoritative rather than something the rate limiter could drag back
        # out of band on the way down from a high prior setpoint.
        heating_c, cooling_c, entry["was_comfort_capped"] = _apply_comfort_guard(heating_c, cooling_c, occupied)

        entry["heating_c"], entry["cooling_c"], entry["fan_available"] = heating_c, cooling_c, fan_available
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        return heating_c, cooling_c, fan_available

    return controller


def demo() -> None:
    """Offline self-check: feeds fake replies through validate+clamp. No network."""
    # Valid reply.
    d = _validate('{"heating_setpoint_c": 21.0, "cooling_setpoint_c": 24.5, "reason": "occupied"}')
    assert (d.heating_setpoint_c, d.cooling_setpoint_c) == (21.0, 24.5)

    # Malformed JSON.
    try:
        _validate("not json")
        raise AssertionError("expected failure")
    except json.JSONDecodeError:
        pass

    # Missing required field.
    try:
        _validate('{"heating_setpoint_c": 21.0}')
        raise AssertionError("expected failure")
    except ValidationError:
        pass

    # Out-of-range pair still validates (schema doesn't enforce range) but clamps.
    d = _validate('{"heating_setpoint_c": 10.0, "cooling_setpoint_c": 40.0}')
    h, c = clamp_setpoints(d.heating_setpoint_c, d.cooling_setpoint_c, occupied=True)
    assert (h, c) == (18.0, 28.0)

    # Inverted pair clamps to a legal deadband.
    d = _validate('{"heating_setpoint_c": 23.0, "cooling_setpoint_c": 24.0}')
    h, c = clamp_setpoints(d.heating_setpoint_c, d.cooling_setpoint_c, occupied=True)
    assert h <= c - 1.0

    # fan_off defaults False when the LLM omits it, parses True when present.
    d = _validate('{"heating_setpoint_c": 18.0, "cooling_setpoint_c": 29.0}')
    assert d.fan_off is False
    d = _validate('{"heating_setpoint_c": 18.0, "cooling_setpoint_c": 29.0, "fan_off": true}')
    assert d.fan_off is True

    # clamp_fan_available: requested off, both hours unoccupied, zone temps cool
    # enough -> honored. Without that margin (no max_zone_temp_c known here), it
    # isn't -- see fallback.py's demo() for the full occupancy/temp matrix.
    assert clamp_fan_available(True, hour=2, day_of_week=3, max_zone_temp_c=20.0, cooling_setpoint_c=29.0) == 0.0
    assert clamp_fan_available(True, hour=2, day_of_week=3, max_zone_temp_c=None, cooling_setpoint_c=29.0) == 1.0

    # Comfort guard: occupied cooling above the cap is capped; at/below it, and
    # unoccupied hours (wider legal range), are untouched.
    h, c, capped = _apply_comfort_guard(21.0, 28.0, occupied=True)
    assert (h, c, capped) == (21.0, 25.5, True)
    h, c, capped = _apply_comfort_guard(21.0, 25.0, occupied=True)
    assert (h, c, capped) == (21.0, 25.0, False)
    h, c, capped = _apply_comfort_guard(18.0, 29.5, occupied=False)
    assert (h, c, capped) == (18.0, 29.5, False)

    print("safety.py: all assertions passed.")


if __name__ == "__main__":
    demo()
