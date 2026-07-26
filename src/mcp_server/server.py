"""FastMCP server exposing src/tools' 5 functions -- "built once,
exposed twice": the control loop calls them directly (src/agent/safety.py) for
reliability; this server exposes the same functions over MCP for spec
compliance + demo, so an external MCP client (Claude Desktop, scripts/mcp_demo.py)
can inspect and drive a running simulation.

get_building_state reads the most recently written row of the newest
results/raw/*/state.csv. EnergyPlusRunner writes that CSV once at the end of a
run (see runner.py's _write_csv), so this server sees the last *completed*
run's final state, not a mid-run live tail -- accurate enough for the demo and
for driving the next run's decisions. inject_setpoints writes
results/pending_setpoints.json, which run_agent.py's "mcp" controller mode
polls once per sim hour and actuates into the live EnergyPlus instance -- this
is the file-based transport described in the Phase 3 plan, upgradeable later to
a socket without changing these tool signatures.
"""
import csv
import glob
import os
import sys
from datetime import date, timedelta

# Launched as a subprocess (`python server.py`, see scripts/mcp_demo.py's
# StdioServerParameters) -- not run as `-m src.mcp_server.server`, so the repo
# root isn't on sys.path by default. Same pattern as every scripts/*.py entrypoint.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src.tools import building_tools  # noqa: E402

mcp = FastMCP("eco-loop-building-agents")

RESULTS_RAW_DIR = os.path.join(building_tools.ROOT_DIR, "results", "raw")

# ponytail: RunPeriod's actual calendar year isn't tracked anywhere reachable from
# here (models/baseline.idf leaves "Begin Year" blank), so day-of-week is
# approximated from day-of-year against a fixed reference year -- fine for the
# MCP demo's illustrative forecast, not for exact calendar accuracy. Upgrade path:
# have EnergyPlusRunner write day_of_week into state.csv alongside day_of_year.
_REFERENCE_YEAR = 2026


def _approx_day_of_week(day_of_year: int) -> int:
    """1=Sunday..7=Saturday, matching pyenergyplus.exchange.day_of_week()."""
    d = date(_REFERENCE_YEAR, 1, 1) + timedelta(days=day_of_year - 1)
    return (d.isoweekday() % 7) + 1  # isoweekday: 1=Monday..7=Sunday -> shift to 1=Sunday


def _latest_row() -> dict | None:
    csvs = sorted(glob.glob(os.path.join(RESULTS_RAW_DIR, "*", "state.csv")), key=os.path.getmtime)
    if not csvs:
        return None
    with open(csvs[-1]) as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


@mcp.tool()
def get_building_state() -> dict:
    """Compact digest of the most recently completed hourly reading from the
    newest run under results/raw/."""
    return building_tools.get_building_state(_latest_row())


@mcp.tool()
def get_forecast_context(horizon: int = 6) -> dict:
    """Next `horizon` hours of occupancy + grid carbon/tariff, anchored on the
    latest known simulated hour."""
    row = _latest_row()
    if row is None:
        return {"available": False}
    day_of_year, hour = int(row["day_of_year"]), int(row["hour"])
    return building_tools.get_forecast_context(day_of_year, hour, _approx_day_of_week(day_of_year), horizon)


@mcp.tool()
def propose_setpoints() -> dict:
    """Asks the configured LLM for a setpoint recommendation given the latest
    state and forecast. Unvalidated -- call inject_setpoints to clamp+apply."""
    row = _latest_row()
    state = building_tools.get_building_state(row)
    if row is None:
        forecast = {"available": False}
    else:
        day_of_year, hour = int(row["day_of_year"]), int(row["hour"])
        forecast = building_tools.get_forecast_context(day_of_year, hour, _approx_day_of_week(day_of_year))
    return building_tools.propose_setpoints(state, forecast, get_recent_errors())


@mcp.tool()
def inject_setpoints(heating_c: float, cooling_c: float, occupied: bool) -> dict:
    """Clamps to the hard safety range and writes results/pending_setpoints.json,
    which a running `python scripts/run_agent.py --controller mcp` picks up on
    its next hourly poll and actuates into the live EnergyPlus instance.

    Phase 4 added a third actuator (AHU fan optimal-stop) to the core loop's
    LLM/fallback controllers, but not to this MCP tool -- the mcp controller
    mode always leaves the fan on (see run_agent.py's _make_mcp_controller).
    Widening this tool's signature is the upgrade path if the MCP demo needs it."""
    return building_tools.inject_setpoints(heating_c, cooling_c, occupied, write_pending=True)


@mcp.tool()
def get_recent_errors(n: int = 5) -> list[str]:
    """Tails the newest run's E+ .err file for Severe/Warning lines, plus the
    last N failed decisions from results/decision_log.jsonl."""
    csvs = sorted(glob.glob(os.path.join(RESULTS_RAW_DIR, "*")), key=os.path.getmtime)
    run_dir = csvs[-1] if csvs else None
    return building_tools.get_recent_errors(run_dir=run_dir, n=n)


if __name__ == "__main__":
    mcp.run()
