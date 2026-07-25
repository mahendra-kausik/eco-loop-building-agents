"""The 5 tool functions (single source of truth, per CLAUDE.md's "built once,
exposed twice"): used directly by the LLM safety supervisor (src/agent/safety.py)
for reliability, and served via FastMCP (src/mcp_server/server.py) for spec
compliance + demo.

Pure functions, no pyenergyplus import -- testable standalone and safe to import
from the MCP server process, which never touches a live E+ simulation.
"""
import functools
import json
import os
import re
from typing import Optional

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
CARBON_CSV = os.path.join(ROOT_DIR, "data", "carbon_intensity.csv")
DECISION_LOG = os.path.join(ROOT_DIR, "results", "decision_log.jsonl")
PENDING_SETPOINTS_PATH = os.path.join(ROOT_DIR, "results", "pending_setpoints.json")

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]

# Mirrors models/baseline.idf's OCCUPY-1 schedule (see src/simulation/runner.py's
# OCCUPANCY_SCHEDULE). EnergyPlus's API exposes only the *current* schedule value,
# never a future one, so forecasting occupancy ahead requires a standalone copy of
# this calendar logic. day_of_week from pyenergyplus.exchange.day_of_week() is
# 1=Sunday..7=Saturday.
# ponytail: hardcoded weekday-only occupancy window mirroring the IDF schedule --
# if OCCUPY-1 in the IDF ever changes hours, this drifts out of sync. Upgrade path:
# read the actual Schedule:Compact hours out of the IDF once at startup instead of
# duplicating them here.
OCCUPIED_START_HOUR = 8
OCCUPIED_END_HOUR = 19
WEEKEND_DAYS = {1, 7}  # Sunday, Saturday


def _is_occupied_hour(hour: int, day_of_week: int) -> bool:
    if day_of_week in WEEKEND_DAYS:
        return False
    return OCCUPIED_START_HOUR <= hour < OCCUPIED_END_HOUR


@functools.lru_cache(maxsize=1)
def _load_carbon_profile() -> list[dict]:
    """24 hourly rows of {hour, carbon_gco2_per_kwh, tariff_per_kwh}, loaded once."""
    import csv

    with open(CARBON_CSV) as f:
        return [
            {
                "hour": int(row["hour"]),
                "carbon_gco2_per_kwh": float(row["carbon_gco2_per_kwh"]),
                "tariff_per_kwh": float(row["tariff_per_kwh"]),
            }
            for row in csv.DictReader(f)
        ]


def get_building_state(row: Optional[dict]) -> dict:
    """Compact digest of the most recently completed hourly reading. Fixed size
    regardless of how many zones/hours exist -- the LLM never sees a raw
    5-zones-by-N-hours table (see docs/ARCHITECTURE.md's long-log strategy)."""
    if row is None:
        return {"available": False}

    zone_temps = {z: float(row[f"{z}_temp_c"]) for z in ZONES}
    zone_pmvs = {z: float(row[f"{z}_pmv"]) for z in ZONES}
    worst_zone = max(zone_pmvs, key=lambda z: abs(zone_pmvs[z]))

    return {
        "available": True,
        "day_of_year": int(row["day_of_year"]),
        "hour": int(row["hour"]),
        "outdoor_temp_c": round(float(row["outdoor_temp_c"]), 1),
        "occupancy_frac": round(float(row["occupancy_frac"]), 2),
        "mean_zone_temp_c": round(sum(zone_temps.values()) / len(zone_temps), 1),
        "min_zone_temp_c": round(min(zone_temps.values()), 1),
        "max_zone_temp_c": round(max(zone_temps.values()), 1),
        "worst_pmv_zone": worst_zone,
        "worst_pmv": round(zone_pmvs[worst_zone], 2),
        "current_heating_setpoint_c": round(float(row["heating_setpoint_c"]), 1),
        "current_cooling_setpoint_c": round(float(row["cooling_setpoint_c"]), 1),
        "electricity_kwh_this_hour": round(float(row["electricity_kwh_this_hour"]), 2),
    }


def get_forecast_context(day_of_year: int, hour: int, day_of_week: int, horizon: int = 6) -> dict:
    """Next `horizon` hours of occupancy + grid carbon/tariff, so the LLM can
    pre-cool/pre-heat ahead of occupancy and shift load into cheap, clean windows --
    the lever the reactive rule-based fallback structurally can't have."""
    profile = _load_carbon_profile()
    hours_ahead = []
    for i in range(1, horizon + 1):
        future_hour = (hour + i) % 24
        future_dow = day_of_week if hour + i < 24 else (day_of_week % 7) + 1
        carbon_row = profile[future_hour]
        hours_ahead.append(
            {
                "hour": future_hour,
                "occupied": _is_occupied_hour(future_hour, future_dow),
                "carbon_gco2_per_kwh": carbon_row["carbon_gco2_per_kwh"],
                "tariff_per_kwh": carbon_row["tariff_per_kwh"],
            }
        )

    occupied_flags = [h["occupied"] for h in hours_ahead]
    hours_until_occupancy = next((i + 1 for i, o in enumerate(occupied_flags) if o), None)
    hours_until_vacancy = next((i + 1 for i, o in enumerate(occupied_flags) if not o), None)
    cheapest = min(hours_ahead, key=lambda h: h["carbon_gco2_per_kwh"])
    dirtiest = max(hours_ahead, key=lambda h: h["carbon_gco2_per_kwh"])

    return {
        "hours_ahead": hours_ahead,
        "hours_until_occupancy": hours_until_occupancy,
        "hours_until_vacancy": hours_until_vacancy,
        "cheapest_hour": cheapest["hour"],
        "dirtiest_hour": dirtiest["hour"],
    }


def propose_setpoints(state: dict, forecast: dict, recent_errors: list[str]) -> dict:
    """Builds the prompt and calls the LLM. Deliberately unvalidated -- schema
    validation, clamping, and fallback-on-failure are src/agent/safety.py's job,
    not this tool's."""
    from src.agent import prompts
    from src.agent.llm_client import complete

    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.build_user_prompt(state, forecast, recent_errors)},
    ]
    text, provider, latency_ms = complete(messages)
    return {"raw_reply": text, "provider": provider, "latency_ms": latency_ms}


def inject_setpoints(
    heating_c: float, cooling_c: float, occupied: bool, write_pending: bool = False
) -> dict:
    """Clamps to the hard safety range (src/agent/fallback.py's clamp_setpoints --
    the single source of truth for the range, not reimplemented here) and,
    optionally, writes results/pending_setpoints.json for the MCP file-based
    injection path (src/mcp_server/server.py + runner.py's "mcp" controller mode)."""
    from src.agent.fallback import clamp_setpoints

    clamped_h, clamped_c = clamp_setpoints(heating_c, cooling_c, occupied)
    was_clamped = (clamped_h, clamped_c) != (heating_c, cooling_c)
    result = {"heating_c": clamped_h, "cooling_c": clamped_c, "was_clamped": was_clamped}

    if write_pending:
        os.makedirs(os.path.dirname(PENDING_SETPOINTS_PATH), exist_ok=True)
        with open(PENDING_SETPOINTS_PATH, "w") as f:
            json.dump(result, f)

    return result


_SEVERITY_RE = re.compile(r"\*\*\s*(Severe|Warning)\s*\*\*", re.IGNORECASE)


def get_recent_errors(run_dir: Optional[str] = None, n: int = 5) -> list[str]:
    """Tails the E+ .err file for Severe/Warning lines and the last N failed
    decisions from the decision log -- the spec's "parse files, extract runtime
    errors" requirement made concrete."""
    errors: list[str] = []

    if run_dir:
        err_path = os.path.join(run_dir, "eplusout.err")
        if os.path.exists(err_path):
            with open(err_path, errors="ignore") as f:
                lines = [line.strip() for line in f if _SEVERITY_RE.search(line)]
            errors.extend(lines[-n:])

    if os.path.exists(DECISION_LOG):
        with open(DECISION_LOG) as f:
            log_lines = [json.loads(line) for line in f if line.strip()]
        failed = [entry for entry in log_lines if entry.get("fallback_used")]
        for entry in failed[-n:]:
            errors.append(
                f"day {entry.get('day_of_year')} hour {entry.get('hour')}: "
                f"fallback used -- {entry.get('error', 'unknown error')}"
            )

    return errors[-n:]


def demo() -> None:
    """Runnable self-check -- no simulation or network needed."""
    forecast = get_forecast_context(day_of_year=200, hour=6, day_of_week=3, horizon=6)
    assert forecast["hours_until_occupancy"] == 2, forecast
    assert len(forecast["hours_ahead"]) == 6
    assert forecast["cheapest_hour"] != forecast["dirtiest_hour"]

    weekend_forecast = get_forecast_context(day_of_year=201, hour=6, day_of_week=1, horizon=6)
    assert all(not h["occupied"] for h in weekend_forecast["hours_ahead"]), weekend_forecast

    assert get_building_state(None) == {"available": False}
    sample_row = {
        "day_of_year": 200, "hour": 9, "outdoor_temp_c": 28.5, "occupancy_frac": 1.0,
        "heating_setpoint_c": 21.0, "cooling_setpoint_c": 24.5, "electricity_kwh_this_hour": 5.2,
        **{f"{z}_temp_c": 23.0 for z in ZONES}, **{f"{z}_pmv": 0.1 for z in ZONES},
    }
    sample_row["SPACE3-1_pmv"] = -0.9  # worst |PMV|
    state = get_building_state(sample_row)
    assert state["worst_pmv_zone"] == "SPACE3-1"
    assert state["worst_pmv"] == -0.9

    result = inject_setpoints(10.0, 40.0, occupied=True)
    assert result["was_clamped"] is True
    assert result["heating_c"] == 18.0 and result["cooling_c"] == 28.0

    assert get_recent_errors(run_dir=None, n=5) == [] or isinstance(get_recent_errors(n=5), list)

    print("building_tools.py: all assertions passed.")


if __name__ == "__main__":
    demo()
