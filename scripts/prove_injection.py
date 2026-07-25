"""Phase 2 gate proof: setpoints injected via set_actuator_value() actually
change what EnergyPlus does, not just what our own CSV reports back.

Controller holds baseline-like setpoints (21.0/23.9) for days 1-2, then steps
cooling to 28.0 from day 3 onward. Asserts:
  (a) the logged cooling_setpoint_c reads back 28.0 after the step -- proves the
      actuator override took (vs. e.g. a wrong handle silently no-op'ing);
  (b) mean zone temp is measurably higher in days 3-7 than days 1-2 -- proves
      the override didn't just change what we read, it changed the simulation;
  (c) days 3-7 daily electricity kWh is lower than days 1-2's -- the energy
      consequence of (b).

Run: python scripts/prove_injection.py
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner, ZONES  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "prove_injection")

STEP_DAY_OF_YEAR = 196 + 2  # Jul 15 = day-of-year 196 in a non-leap year; day 3 = 198


def step_controller(row):
    day = row["day_of_year"] if row else 196
    if day < STEP_DAY_OF_YEAR:
        return 21.0, 23.9
    return 21.0, 28.0


def main() -> None:
    idf_path = build_baseline_idf()
    epw_path = os.path.join(
        ENERGYPLUS_DIR, "WeatherData", "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    )
    runner = EnergyPlusRunner(idf_path, epw_path, OUTPUT_DIR, controller=step_controller)
    exit_code = runner.run()
    assert exit_code == 0, f"EnergyPlus exited {exit_code}"

    # One-hour decision lag: the controller for day N hour 0 runs before hour 0 starts,
    # seeing only day N-1's last row -- so day N hour 0 still reflects the pre-step
    # setpoint. The step is fully in effect starting day N hour 1.
    step_idx = next(
        i for i, r in enumerate(runner.rows)
        if r["day_of_year"] == STEP_DAY_OF_YEAR and r["hour"] == 1
    )
    before = runner.rows[:step_idx]
    after = runner.rows[step_idx:]
    assert before and after, "expected rows on both sides of the step"

    # (a) actuator override read back correctly, not silently ignored.
    stepped_setpoints = {r["cooling_setpoint_c"] for r in after}
    assert stepped_setpoints == {28.0}, f"expected cooling_setpoint_c==28.0 after the step, got {stepped_setpoints}"

    # (b) zone temps actually rose in response -- the override changed E+'s own physics.
    mean_temp = lambda rows: statistics.mean(r[f"{z}_temp_c"] for r in rows for z in ZONES)
    temp_before, temp_after = mean_temp(before), mean_temp(after)
    assert temp_after > temp_before, f"expected mean zone temp to rise after step, got {temp_before:.2f} -> {temp_after:.2f}"

    # (c) higher setpoint -> less cooling load -> lower average hourly kWh.
    mean_kwh = lambda rows: statistics.mean(r["electricity_kwh_this_hour"] for r in rows)
    kwh_before, kwh_after = mean_kwh(before), mean_kwh(after)
    assert kwh_after < kwh_before, f"expected mean hourly kWh to drop after step, got {kwh_before:.3f} -> {kwh_after:.3f}"

    print("Injection proof passed:")
    print(f"  cooling_setpoint_c after step: {stepped_setpoints} (expected {{28.0}})")
    print(f"  mean zone temp:  before={temp_before:.2f}C  after={temp_after:.2f}C")
    print(f"  mean hourly kWh: before={kwh_before:.3f}  after={kwh_after:.3f}")
    print(f"  injections={runner.injections}  controller_errors={runner.controller_errors}")


if __name__ == "__main__":
    main()
