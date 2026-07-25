"""Agent run ("Run B"): same 7-day horizon and IDF as run_baseline.py, but
EnergyPlusRunner is given a controller -- for Phase 2 that's the deterministic
src/agent/fallback.py. Phase 3 changes exactly one line here (the controller
passed in) to swap in the LLM + safety supervisor.

Prints the headline comparison against results/raw/baseline/state.csv: total
kWh, % saved, occupied-hours PMV-in-band %, injection count, controller errors.

Run: python scripts/run_baseline.py   (first, to produce the comparison point)
     python scripts/run_agent.py
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent.fallback import fallback_controller  # noqa: E402
from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner, ZONES  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "agent")
BASELINE_CSV = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "baseline", "state.csv")

PMV_BAND = (-0.5, 0.5)


def comfort_in_band_pct(rows: list[dict]) -> float:
    occupied = [r for r in rows if float(r["occupancy_frac"]) > 0]
    if not occupied:
        return 0.0
    total = len(occupied) * len(ZONES)
    in_band = sum(
        1
        for r in occupied
        for z in ZONES
        if PMV_BAND[0] <= float(r[f"{z}_pmv"]) <= PMV_BAND[1]
    )
    return 100.0 * in_band / total


def main() -> None:
    idf_path = build_baseline_idf()
    epw_path = os.path.join(
        ENERGYPLUS_DIR, "WeatherData", "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    )
    runner = EnergyPlusRunner(idf_path, epw_path, OUTPUT_DIR, controller=fallback_controller)
    exit_code = runner.run()

    print(f"\nEnergyPlus exit code: {exit_code}")
    print(f"Rows collected: {len(runner.rows)}")

    assert len(runner.rows) == 168, f"expected 168 hourly rows for 7 days, got {len(runner.rows)}"
    assert runner.controller_errors == [], f"controller raised errors: {runner.controller_errors}"
    assert runner.injections == 168, f"expected 168 setpoint injections, got {runner.injections}"

    agent_kwh = runner.rows[-1]["cumulative_electricity_kwh"]
    agent_comfort = comfort_in_band_pct(runner.rows)
    print(f"\nAgent run: {agent_kwh:.1f} kWh electricity, {agent_comfort:.1f}% occupied PMV in-band, "
          f"{runner.injections} injections, {len(runner.controller_errors)} controller errors.")

    if os.path.exists(BASELINE_CSV):
        with open(BASELINE_CSV) as f:
            baseline_rows = list(csv.DictReader(f))
        baseline_kwh = float(baseline_rows[-1]["cumulative_electricity_kwh"])
        baseline_comfort = comfort_in_band_pct(baseline_rows)
        pct_saved = 100.0 * (baseline_kwh - agent_kwh) / baseline_kwh
        print(f"Baseline run: {baseline_kwh:.1f} kWh electricity, {baseline_comfort:.1f}% occupied PMV in-band.")
        print(f"Savings: {pct_saved:+.1f}% kWh vs baseline; comfort delta: {agent_comfort - baseline_comfort:+.1f} pts.")
    else:
        print(f"No baseline found at {BASELINE_CSV} -- run scripts/run_baseline.py first for a comparison.")


if __name__ == "__main__":
    main()
