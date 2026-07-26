"""The 5 tool functions (single source of truth -- "built once,
exposed twice"): used directly by the LLM safety supervisor (src/agent/safety.py)
for reliability, and served via FastMCP (src/mcp_server/server.py) for spec
compliance + demo.

Pure functions, no pyenergyplus import -- testable standalone and safe to import
from the MCP server process, which never touches a live E+ simulation.
"""
import datetime
import functools
import json
import os
import re
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
CARBON_CSV = os.path.join(ROOT_DIR, "data", "carbon_intensity.csv")
DECISION_LOG = os.path.join(ROOT_DIR, "results", "decision_log.jsonl")
PENDING_SETPOINTS_PATH = os.path.join(ROOT_DIR, "results", "pending_setpoints.json")

# Outdoor-temp lookahead: EnergyPlus's Python API only exposes the *current*
# timestep's value, never a future one (same limitation noted for occupancy
# above), so pre-cooling decisions need an independent read of the weather
# file. Deliberately NOT routed through src/simulation/eplus_path.py -- that
# module raises if ENERGYPLUS_DIR/pyenergyplus aren't importable, which would
# force a live-E+-capable environment onto the MCP server process too (its own
# docstring: "never touches a live E+ simulation"). Read the env var directly
# instead and degrade to no-forecast (None) rather than crash if it's unset.
_ENERGYPLUS_DIR = os.environ.get("ENERGYPLUS_DIR", "").strip()
EPW_PATH = (
    os.path.join(_ENERGYPLUS_DIR, "WeatherData", "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw")
    if _ENERGYPLUS_DIR
    else None
)
_EPW_HEADER_LINES = 8
_EPW_REFERENCE_YEAR = 2026  # non-leap, matches src/mcp_server/server.py's day_of_week approximation

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

# Spec ("evaluates this data against predefined targets like ... peak demand
# thresholds"). Measured, not guessed: src/analysis/metrics.py's peak_demand_kw
# on the baseline 7-day run is 19.9 kW (building+HVAC electricity, same hourly
# total as electricity_kwh_this_hour below) -- set just under it so there's a
# genuine peak-shaving incentive rather than a ceiling the agent clears by
# default.
PEAK_DEMAND_THRESHOLD_KW = 19.0

# Spec ("indoor air quality"). ASHRAE 62.1's commonly-cited indoor CO2 guidance
# is roughly 1000 ppm as the point where perceived air quality starts to
# degrade for a typical office population (700 ppm above the ~400 ppm outdoor
# baseline this IDF's ZoneAirContaminantBalance schedule uses -- see
# idf_prep.py). Used as a hard constraint on fan shutoff, not merely advisory:
# ventilation must never be turned off if it would let CO2 drift up unchecked.
CO2_COMFORT_THRESHOLD_PPM = 1000.0


def is_occupied_hour(hour: int, day_of_week: int) -> bool:
    if day_of_week in WEEKEND_DAYS:
        return False
    return OCCUPIED_START_HOUR <= hour < OCCUPIED_END_HOUR


@functools.lru_cache(maxsize=1)
def _load_outdoor_temp_forecast() -> dict:
    """{(day_of_year, hour): drybulb_temp_c}, parsed once from the EPW file.
    Empty dict (not an error) if EPW_PATH is unset or unreadable -- callers
    treat a lookup miss as "forecast unavailable", not a failure.

    EPW hour is 1-24 (hour-ending, e.g. hour=1 covers 00:00-01:00); this
    runner's hour is 0-23 (see runner.py's row schema), so epw_hour - 1 is the
    matching key. Year is ignored -- TMY3 files splice different historical
    years per month, but day_of_year here is purely month/day math, matching
    how the IDF's RunPeriod has no "Begin Year" (see mcp_server/server.py)."""
    if not EPW_PATH or not os.path.exists(EPW_PATH):
        return {}

    lookup = {}
    with open(EPW_PATH, encoding="latin-1") as f:
        for _ in range(_EPW_HEADER_LINES):
            next(f, None)
        for line in f:
            fields = line.split(",")
            if len(fields) < 7:
                continue
            month, day, epw_hour = int(fields[1]), int(fields[2]), int(fields[3])
            drybulb_c = float(fields[6])
            day_of_year = (
                datetime.date(_EPW_REFERENCE_YEAR, month, day) - datetime.date(_EPW_REFERENCE_YEAR, 1, 1)
            ).days + 1
            lookup[(day_of_year, epw_hour - 1)] = drybulb_c
    return lookup


@functools.lru_cache(maxsize=1)
def load_carbon_profile() -> list[dict]:
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


def get_building_state(row: Optional[dict], peak_kw_so_far: Optional[float] = None) -> dict:
    """Compact digest of the most recently completed hourly reading. Fixed size
    regardless of how many zones/hours exist -- the LLM never sees a raw
    5-zones-by-N-hours table (see ARCHITECTURE.md's long-log strategy).

    peak_kw_so_far: the caller's running max of electricity_kwh_this_hour across
    the run (this function only sees one row, so it can't track a running max
    itself) -- None if the caller isn't tracking one (e.g. a one-off MCP query).
    """
    if row is None:
        return {"available": False}

    zone_temps = {z: float(row[f"{z}_temp_c"]) for z in ZONES}
    zone_pmvs = {z: float(row[f"{z}_pmv"]) for z in ZONES}
    zone_rh = {z: float(row[f"{z}_rh_pct"]) for z in ZONES}
    worst_zone = max(zone_pmvs, key=lambda z: abs(zone_pmvs[z]))

    # CO2 is IAQ, not comfort -- a row from before this field existed (an old
    # state.csv) just omits it, same fallback style as hvac_kwh_this_hour below.
    zone_co2 = {z: float(row[f"{z}_co2_ppm"]) for z in ZONES if f"{z}_co2_ppm" in row}
    max_co2_ppm = max(zone_co2.values()) if zone_co2 else None

    # hvac_kwh_this_hour falls back to the total for state.csv rows written before
    # the Phase 4 metering fix added the split (older results/raw/*/state.csv).
    hvac_kwh = row.get("hvac_kwh_this_hour", row["electricity_kwh_this_hour"])

    return {
        "available": True,
        "day_of_year": int(row["day_of_year"]),
        "hour": int(row["hour"]),
        "outdoor_temp_c": round(float(row["outdoor_temp_c"]), 1),
        "occupancy_frac": round(float(row["occupancy_frac"]), 2),
        "mean_zone_temp_c": round(sum(zone_temps.values()) / len(zone_temps), 1),
        "min_zone_temp_c": round(min(zone_temps.values()), 1),
        "max_zone_temp_c": round(max(zone_temps.values()), 1),
        "mean_zone_rh_pct": round(sum(zone_rh.values()) / len(zone_rh), 1),
        "worst_pmv_zone": worst_zone,
        "worst_pmv": round(zone_pmvs[worst_zone], 2),
        "current_heating_setpoint_c": round(float(row["heating_setpoint_c"]), 1),
        "current_cooling_setpoint_c": round(float(row["cooling_setpoint_c"]), 1),
        "fan_available": bool(float(row.get("fan_available", 1.0))),
        # Total electricity is mostly lighting/plug load this agent can't touch
        # (~62% of facility total, see ARCHITECTURE.md) -- hvac_kwh_this_hour
        # is the number that actually reflects setpoint/fan decisions.
        "electricity_kwh_this_hour": round(float(row["electricity_kwh_this_hour"]), 2),
        "hvac_kwh_this_hour": round(float(hvac_kwh), 2),
        "peak_kw_so_far": round(peak_kw_so_far, 1) if peak_kw_so_far is not None else None,
        "peak_demand_threshold_kw": PEAK_DEMAND_THRESHOLD_KW,
        "max_zone_co2_ppm": round(max_co2_ppm, 0) if max_co2_ppm is not None else None,
        "co2_comfort_threshold_ppm": CO2_COMFORT_THRESHOLD_PPM,
    }


def get_forecast_context(day_of_year: int, hour: int, day_of_week: int, horizon: int = 6) -> dict:
    """Next `horizon` hours of occupancy + grid carbon/tariff + outdoor temp, so
    the LLM can pre-cool/pre-heat ahead of occupancy and shift load into cheap,
    clean windows -- the lever the reactive rule-based fallback structurally
    can't have. outdoor_temp_c comes from the EPW weather file (see
    _load_outdoor_temp_forecast) and is None for an hour if that file isn't
    reachable -- callers must handle a missing forecast, not assume one.
    cheapest/dirtiest rank by carbon, cheapest/priciest_tariff by cost --
    tracked separately since they don't always agree (see ARCHITECTURE.md)."""
    profile = load_carbon_profile()
    outdoor_forecast = _load_outdoor_temp_forecast()
    hours_ahead = []
    for i in range(1, horizon + 1):
        future_hour = (hour + i) % 24
        future_day_of_year = day_of_year + (hour + i) // 24
        future_dow = day_of_week if hour + i < 24 else (day_of_week % 7) + 1
        carbon_row = profile[future_hour]
        hours_ahead.append(
            {
                "hour": future_hour,
                "occupied": is_occupied_hour(future_hour, future_dow),
                "carbon_gco2_per_kwh": carbon_row["carbon_gco2_per_kwh"],
                "tariff_per_kwh": carbon_row["tariff_per_kwh"],
                "outdoor_temp_c": outdoor_forecast.get((future_day_of_year, future_hour)),
            }
        )

    occupied_flags = [h["occupied"] for h in hours_ahead]
    hours_until_occupancy = next((i + 1 for i, o in enumerate(occupied_flags) if o), None)
    hours_until_vacancy = next((i + 1 for i, o in enumerate(occupied_flags) if not o), None)
    cheapest_carbon = min(hours_ahead, key=lambda h: h["carbon_gco2_per_kwh"])
    dirtiest_carbon = max(hours_ahead, key=lambda h: h["carbon_gco2_per_kwh"])
    cheapest_tariff = min(hours_ahead, key=lambda h: h["tariff_per_kwh"])
    priciest_tariff = max(hours_ahead, key=lambda h: h["tariff_per_kwh"])

    return {
        "decision_hour": hour,
        "decision_hour_occupied": is_occupied_hour(hour, day_of_week),
        "hours_ahead": hours_ahead,
        "hours_until_occupancy": hours_until_occupancy,
        "hours_until_vacancy": hours_until_vacancy,
        "cheapest_hour": cheapest_carbon["hour"],
        "dirtiest_hour": dirtiest_carbon["hour"],
        "cheapest_tariff_hour": cheapest_tariff["hour"],
        "priciest_tariff_hour": priciest_tariff["hour"],
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

# Each error line is truncated before it reaches the prompt. These strings get
# fed straight back to the LLM by safety.py, and a provider 429 body is huge
# (~250 chars of JSON: org id, tier, exact token counts, upsell copy). Sending
# 5 of them verbatim added ~620 tokens/call -- a 31% prompt inflation that only
# kicks in *once failures start*, i.e. exactly when the token budget is already
# the thing failing. That's a feedback loop: throttled -> fatter prompts ->
# more throttled. The LLM only needs the gist ("rate limited", "invalid JSON"),
# never the org id or the retry-after decimals, so cap it.
MAX_ERROR_CHARS = 160


def get_recent_errors(run_dir: Optional[str] = None, n: int = 5) -> list[str]:
    """Tails the E+ .err file for Severe/Warning lines and the last N failed
    decisions from the decision log -- the spec's "parse files, extract runtime
    errors" requirement made concrete. Each line is truncated to
    MAX_ERROR_CHARS so a burst of verbose provider errors can't inflate the
    prompt (see that constant's comment)."""
    errors: list[str] = []

    def _clip(text: str) -> str:
        text = " ".join(text.split())  # collapse newlines/runs of whitespace
        return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."

    if run_dir:
        err_path = os.path.join(run_dir, "eplusout.err")
        if os.path.exists(err_path):
            with open(err_path, errors="ignore") as f:
                lines = [line.strip() for line in f if _SEVERITY_RE.search(line)]
            errors.extend(_clip(line) for line in lines[-n:])

    if os.path.exists(DECISION_LOG):
        with open(DECISION_LOG) as f:
            log_lines = [json.loads(line) for line in f if line.strip()]
        failed = [entry for entry in log_lines if entry.get("fallback_used")]
        for entry in failed[-n:]:
            errors.append(
                _clip(
                    f"day {entry.get('day_of_year')} hour {entry.get('hour')}: "
                    f"fallback used -- {entry.get('error', 'unknown error')}"
                )
            )

    return errors[-n:]


def demo() -> None:
    """Runnable self-check -- no simulation or network needed."""
    forecast = get_forecast_context(day_of_year=200, hour=6, day_of_week=3, horizon=6)
    assert forecast["hours_until_occupancy"] == 2, forecast
    assert len(forecast["hours_ahead"]) == 6
    # decision_hour_occupied names the hour being decided FOR -- current_state is
    # always the PREVIOUS completed hour, so without this the LLM has no signal
    # for the hour it's actually setting (root cause of the hour-19 bug: the model
    # read hour 18's occupied/comfortable state and held comfort setpoints into an
    # empty building). hour=10 weekday is occupied; hour=19 weekday is not, despite
    # hour=18 (the last completed row at that point) being occupied.
    assert get_forecast_context(200, 10, day_of_week=3)["decision_hour_occupied"] is True
    assert get_forecast_context(200, 19, day_of_week=3)["decision_hour_occupied"] is False
    assert forecast["cheapest_hour"] != forecast["dirtiest_hour"]
    assert "cheapest_tariff_hour" in forecast and "priciest_tariff_hour" in forecast
    # outdoor_temp_c is None if EPW_PATH is unreachable, a float otherwise -- either
    # is valid, but the key must always be present so callers never KeyError.
    assert all("outdoor_temp_c" in h for h in forecast["hours_ahead"])

    # Horizon crossing midnight: day_of_year must roll over for the EPW lookup key
    # (only exercised indirectly here -- a wrong day_of_year would just silently
    # miss the lookup and return None, so this mainly guards against a crash).
    rollover_forecast = get_forecast_context(day_of_year=200, hour=22, day_of_week=3, horizon=4)
    assert len(rollover_forecast["hours_ahead"]) == 4

    weekend_forecast = get_forecast_context(day_of_year=201, hour=6, day_of_week=1, horizon=6)
    assert all(not h["occupied"] for h in weekend_forecast["hours_ahead"]), weekend_forecast

    assert get_building_state(None) == {"available": False}
    sample_row = {
        "day_of_year": 200, "hour": 9, "outdoor_temp_c": 28.5, "occupancy_frac": 1.0,
        "heating_setpoint_c": 21.0, "cooling_setpoint_c": 24.5, "electricity_kwh_this_hour": 5.2,
        "hvac_kwh_this_hour": 4.0, "fan_available": 1.0,
        **{f"{z}_temp_c": 23.0 for z in ZONES}, **{f"{z}_pmv": 0.1 for z in ZONES},
        **{f"{z}_rh_pct": 45.0 for z in ZONES}, **{f"{z}_co2_ppm": 650.0 for z in ZONES},
    }
    sample_row["SPACE3-1_pmv"] = -0.9  # worst |PMV|
    state = get_building_state(sample_row)
    assert state["worst_pmv_zone"] == "SPACE3-1"
    assert state["worst_pmv"] == -0.9
    assert state["hvac_kwh_this_hour"] == 4.0
    assert state["fan_available"] is True
    assert state["mean_zone_rh_pct"] == 45.0
    # peak_kw_so_far/peak_demand_threshold_kw: None when the caller isn't
    # tracking a running max (e.g. a one-off MCP query); present when it is.
    assert state["peak_kw_so_far"] is None
    assert get_building_state(sample_row, peak_kw_so_far=22.3)["peak_kw_so_far"] == 22.3
    assert state["peak_demand_threshold_kw"] == PEAK_DEMAND_THRESHOLD_KW
    assert state["max_zone_co2_ppm"] == 650.0
    assert state["co2_comfort_threshold_ppm"] == CO2_COMFORT_THRESHOLD_PPM

    # hvac_kwh_this_hour falls back to the total for pre-Phase-4 rows that lack it.
    old_row = {k: v for k, v in sample_row.items() if k != "hvac_kwh_this_hour"}
    assert get_building_state(old_row)["hvac_kwh_this_hour"] == 5.2

    # max_zone_co2_ppm is None for a row from before this field existed (an old
    # state.csv without any *_co2_ppm columns) -- same graceful-degradation
    # style as hvac_kwh_this_hour above, just returning None instead of a total.
    no_co2_row = {k: v for k, v in sample_row.items() if not k.endswith("_co2_ppm")}
    assert get_building_state(no_co2_row)["max_zone_co2_ppm"] is None

    result = inject_setpoints(10.0, 40.0, occupied=True)
    assert result["was_clamped"] is True
    assert result["heating_c"] == 18.0 and result["cooling_c"] == 28.0

    errs = get_recent_errors(n=5)
    assert isinstance(errs, list) and len(errs) <= 5
    # Every line must be capped -- this is what stops a burst of verbose provider
    # 429 bodies from inflating the very prompts that are already being throttled.
    assert all(len(e) <= MAX_ERROR_CHARS for e in errs), [len(e) for e in errs]

    print("building_tools.py: all assertions passed.")


if __name__ == "__main__":
    demo()
