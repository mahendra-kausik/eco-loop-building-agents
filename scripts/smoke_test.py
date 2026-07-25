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
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent import fallback, safety  # noqa: E402
from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "smoke")
SMOKE_IDF = os.path.join(OUTPUT_DIR, "smoke.idf")


def main() -> None:
    fallback.demo()  # raises on failure -- clamp logic checked before spending E+ time
    safety.demo()  # raises on failure -- LLM JSON validation checked, no network needed

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

    # Phase 4: the controller now predicts occupancy for the hour it's deciding FOR
    # (fallback.is_occupied_hour(hour, day_of_week)) instead of reading the last
    # completed row reactively, so row[i]'s setpoints are checked against row[i]'s
    # own occupancy_frac directly -- no more one-hour lag bookkeeping. is_occupied_hour
    # is built to mirror OCCUPY-1 exactly (occupancy_frac > 0 for hours 8-18 weekdays),
    # confirmed against the IDF's schedule fractions.
    for row in runner.rows:
        h, c = row["heating_setpoint_c"], row["cooling_setpoint_c"]
        occupied = row["occupancy_frac"] > 0
        lo_h, hi_h = fallback.OCCUPIED_HEATING_RANGE if occupied else fallback.UNOCCUPIED_HEATING_RANGE
        lo_c, hi_c = fallback.OCCUPIED_COOLING_RANGE if occupied else fallback.UNOCCUPIED_COOLING_RANGE
        assert lo_h <= h <= hi_h, f"heating {h} outside clamp {lo_h}-{hi_h} at day {row['day_of_year']} hr {row['hour']}"
        assert lo_c <= c <= hi_c, f"cooling {c} outside clamp {lo_c}-{hi_c} at day {row['day_of_year']} hr {row['hour']}"
        assert h <= c - fallback.MIN_DEADBAND_C, f"deadband violated: h={h} c={c}"
        fan = row["fan_available"]
        assert fan in (0.0, 1.0), f"fan_available {fan} not a legal 0/1 value at day {row['day_of_year']} hr {row['hour']}"
        if occupied:
            assert fan == 1.0, f"fan off during an occupied hour: day {row['day_of_year']} hr {row['hour']}"

    # Regression check for the Phase 4 metering bug: python-accumulated electricity
    # must reconcile against EnergyPlus's own eplusmtr.csv within 0.5%, or the
    # runner is mis-binning energy into the wrong hour again (see runner.py's
    # module docstring "CORRECTION" note).
    mtr_path = os.path.join(OUTPUT_DIR, "eplusmtr.csv")
    with open(mtr_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = [header.index(f"Electricity:{m} [J](Hourly)") for m in ("Building", "HVAC", "Plant")]
        meter_kwh = sum(sum(float(row[i]) for i in cols) for row in reader) / 3.6e6
    python_kwh = runner.rows[-1]["cumulative_electricity_kwh"]
    pct_off = abs(python_kwh - meter_kwh) / meter_kwh * 100
    assert pct_off < 0.5, (
        f"python-accumulated electricity ({python_kwh:.2f} kWh) diverges from "
        f"eplusmtr.csv ({meter_kwh:.2f} kWh) by {pct_off:.2f}% -- metering regression"
    )

    print(f"smoke_test.py passed: exit={exit_code}, rows={len(runner.rows)}, "
          f"injections={runner.injections}, controller_errors=0, all setpoints in clamp, "
          f"kWh reconciled with eplusmtr.csv ({pct_off:.2f}% off).")


if __name__ == "__main__":
    main()
