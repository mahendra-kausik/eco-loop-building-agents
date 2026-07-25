"""Baseline run: builds models/baseline.idf (the deliverable, untouched schedules),
runs the 7-day Chicago-July horizon with no controller (controller=None), and prints
sanity-check results. This is the "Run A" fixed-schedule comparison point for Phase 4.

Replaces the old scripts/run_phase1.py -- same script, better name now that Phase 2
gives it a Run B counterpart (scripts/run_agent.py).

Run: python scripts/run_baseline.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "baseline")


def main() -> None:
    idf_path = build_baseline_idf()
    epw_path = os.path.join(
        ENERGYPLUS_DIR, "WeatherData", "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    )
    runner = EnergyPlusRunner(idf_path, epw_path, OUTPUT_DIR, controller=None)
    exit_code = runner.run()

    print(f"\nEnergyPlus exit code: {exit_code}")
    print(f"Rows collected: {len(runner.rows)}")

    assert len(runner.rows) == 168, f"expected 168 hourly rows for 7 days, got {len(runner.rows)}"
    for row in runner.rows:
        for zone in ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]:
            t = row[f"{zone}_temp_c"]
            assert 10 <= t <= 40, f"{zone} temp out of range: {t}"
    assert runner.rows[-1]["cumulative_electricity_kwh"] > 0, "total electricity kWh was not > 0"
    assert runner.injections == 0, "baseline run must not actuate anything (controller=None)"
    print("All sanity assertions passed: 168 rows, zone temps in [10,40]C, cumulative kWh > 0, zero injections.")
    print(f"Total electricity: {runner.rows[-1]['cumulative_electricity_kwh']:.1f} kWh")
    print(f"Total gas: {runner.rows[-1]['cumulative_gas_kwh']:.1f} kWh")


if __name__ == "__main__":
    main()
