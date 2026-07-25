"""Pre-commit gate for anything touching the loop (per CLAUDE.md). Two checks:

1. src/agent/fallback.py's own assert-based self-check (clamp_setpoints on
   illegal inputs comes back legal) -- fast, no simulation needed.
2. A 2-simulated-day EnergyPlus run with the fallback controller wired in:
   exit code 0, 48 hourly rows, at least one injection, zero controller
   errors, and every logged setpoint pair inside the clamp with
   heating <= cooling - 1.

Writes into results/raw/smoke/ using a 2-day IDF copy (out_path) so this never
touches models/baseline.idf, which is a deliverable.

Run: python scripts/smoke_test.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent import fallback  # noqa: E402
from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "smoke")
SMOKE_IDF = os.path.join(OUTPUT_DIR, "smoke.idf")


def main() -> None:
    fallback.demo()  # raises on failure -- clamp logic checked before spending E+ time

    idf_path = build_baseline_idf(days=2, out_path=SMOKE_IDF)
    epw_path = os.path.join(
        ENERGYPLUS_DIR, "WeatherData", "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    )
    runner = EnergyPlusRunner(idf_path, epw_path, OUTPUT_DIR, controller=fallback.fallback_controller)
    exit_code = runner.run()

    assert exit_code == 0, f"EnergyPlus exited {exit_code}"
    assert len(runner.rows) == 48, f"expected 48 hourly rows for 2 days, got {len(runner.rows)}"
    assert runner.controller_errors == [], f"controller raised errors: {runner.controller_errors}"
    assert runner.injections > 0, "expected at least one setpoint injection"

    # The controller for row[i]'s setpoints runs *before* hour i starts, seeing only
    # row[i-1] (row[0]'s decision saw no row at all -> occupied=False). So the clamp
    # range to check row[i] against is keyed off row[i-1]'s occupancy, not row[i]'s own.
    prior_occupied = False
    for row in runner.rows:
        h, c = row["heating_setpoint_c"], row["cooling_setpoint_c"]
        lo_h, hi_h = fallback.OCCUPIED_HEATING_RANGE if prior_occupied else fallback.UNOCCUPIED_HEATING_RANGE
        lo_c, hi_c = fallback.OCCUPIED_COOLING_RANGE if prior_occupied else fallback.UNOCCUPIED_COOLING_RANGE
        assert lo_h <= h <= hi_h, f"heating {h} outside clamp {lo_h}-{hi_h} at day {row['day_of_year']} hr {row['hour']}"
        assert lo_c <= c <= hi_c, f"cooling {c} outside clamp {lo_c}-{hi_c} at day {row['day_of_year']} hr {row['hour']}"
        assert h <= c - fallback.MIN_DEADBAND_C, f"deadband violated: h={h} c={c}"
        prior_occupied = row["occupancy_frac"] > 0

    print(f"smoke_test.py passed: exit={exit_code}, rows={len(runner.rows)}, "
          f"injections={runner.injections}, controller_errors=0, all setpoints in clamp.")


if __name__ == "__main__":
    main()
