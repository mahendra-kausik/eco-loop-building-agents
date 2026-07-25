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

from src.agent.fallback import clamp_setpoints, fallback_controller
from src.agent.llm_client import complete
from src.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from src.tools.building_tools import get_building_state, get_forecast_context, get_recent_errors


class SetpointDecision(BaseModel):
    heating_setpoint_c: float
    cooling_setpoint_c: float
    reason: str = ""


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

    def controller(row: Optional[dict], day_of_year: int, hour: int, day_of_week: int) -> tuple[float, float]:
        occupied = row is not None and row.get("occupancy_frac", 0.0) > 0.0
        state = get_building_state(row)
        forecast = get_forecast_context(day_of_year, hour, day_of_week)
        recent_errors = get_recent_errors(run_dir=run_dir)

        entry = {
            "day_of_year": day_of_year, "hour": hour, "occupied": occupied,
            "provider": None, "latency_ms": None, "raw_reply": None, "retried": False,
            "heating_c": None, "cooling_c": None, "was_clamped": None,
            "fallback_used": False, "error": None,
        }

        try:
            decision, meta = _ask_llm(state, forecast, recent_errors)
            entry.update(provider=meta["provider"], latency_ms=meta["latency_ms"],
                         raw_reply=meta["raw_reply"], retried=meta["retried"], reason=decision.reason)
            heating_c, cooling_c = clamp_setpoints(decision.heating_setpoint_c, decision.cooling_setpoint_c, occupied)
            entry["was_clamped"] = (heating_c, cooling_c) != (decision.heating_setpoint_c, decision.cooling_setpoint_c)
        except Exception as exc:  # noqa: BLE001 -- any failure (timeout, invalid-after-retry, etc.) falls back
            entry["fallback_used"] = True
            entry["error"] = f"{type(exc).__name__}: {exc}"
            heating_c, cooling_c = fallback_controller(row, day_of_year, hour, day_of_week)

        entry["heating_c"], entry["cooling_c"] = heating_c, cooling_c
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        return heating_c, cooling_c

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

    print("safety.py: all assertions passed.")


if __name__ == "__main__":
    demo()
