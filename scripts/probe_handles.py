"""Phase 1 guess-killer: confirm every variable/meter/actuator name we plan to use
against the real EnergyPlus install, instead of trusting names copied from docs.

Runs a short sim on the unmodified shipped 5ZoneAirCooled.idf (SF weather, first
week of January -- fast, no need for our Chicago/July baseline yet) with a single
callback that, once the API is ready:
  1. dumps exchange.list_available_api_data_csv() to results/raw/api_data.csv
  2. tries to resolve every handle this project's runner.py will need
  3. prints PASS/FAIL per handle (a -1 handle is a FAIL, per this project's
     "guard; a -1 handle must raise a clear error" rule)
  4. stops the simulation early once everything is checked (we don't need a full run)

Run: python scripts/probe_handles.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.simulation.eplus_path import ENERGYPLUS_DIR, EnergyPlusAPI  # noqa: E402

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]
PEOPLE_OBJECTS = [f"{z} PEOPLE 1" for z in ZONES]

VARIABLES_TO_CHECK = (
    [("Zone Mean Air Temperature", z) for z in ZONES]
    + [("Site Outdoor Air Drybulb Temperature", "Environment")]
    + [("Zone Thermal Comfort Fanger Model PMV", p) for p in PEOPLE_OBJECTS]
    + [("Zone Air Relative Humidity", z) for z in ZONES]
)
# NOTE: 'Electricity:Facility' is listed in list_available_api_data_csv() but
# get_meter_handle() reproducibly returns -1 for it on this EnergyPlus 26.1.0 install
# (confirmed on both a January SF run and the actual July Chicago run/weather this
# project uses -- not a warmup-timing or naming issue). This IDF has no Exterior:*
# electric objects, so Building + HVAC + Plant == Facility total exactly; sum those
# three instead. See src/simulation/runner.py.
METERS_TO_CHECK = ["Electricity:Building", "Electricity:HVAC", "Electricity:Plant", "NaturalGas:Facility"]
ACTUATORS_TO_CHECK = [
    ("Schedule:Compact", "Schedule Value", "Htg-SetP-Sch"),
    ("Schedule:Compact", "Schedule Value", "Clg-SetP-Sch"),
]

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "probe")
os.makedirs(OUT_DIR, exist_ok=True)


def main() -> None:
    api = EnergyPlusAPI()
    state = api.state_manager.new_state()
    exchange = api.exchange
    checked = {"done": False, "ready_calls": 0}

    for var_name, key in VARIABLES_TO_CHECK:
        exchange.request_variable(state, var_name, key)

    def on_timestep(state_arg) -> None:
        if checked["done"]:
            return
        if not exchange.api_data_fully_ready(state_arg) or exchange.warmup_flag(state_arg):
            return
        # Meters build up from component report variables that may not all be
        # registered on the very first post-warmup call -- give it a few ticks.
        checked["ready_calls"] += 1
        if checked["ready_calls"] < 5:
            return
        checked["done"] = True

        csv_path = os.path.join(OUT_DIR, "api_data.csv")
        with open(csv_path, "wb") as f:
            f.write(exchange.list_available_api_data_csv(state_arg))
        print(f"Wrote full API data catalogue -> {csv_path}")

        print("\n--- VARIABLES ---")
        for var_name, key in VARIABLES_TO_CHECK:
            h = exchange.get_variable_handle(state_arg, var_name, key)
            status = "PASS" if h != -1 else "FAIL"
            print(f"[{status}] Variable  {var_name!r} @ {key!r} -> handle={h}")

        print("\n--- METERS ---")
        for meter_name in METERS_TO_CHECK:
            h = exchange.get_meter_handle(state_arg, meter_name)
            status = "PASS" if h != -1 else "FAIL"
            print(f"[{status}] Meter     {meter_name!r} -> handle={h}")

        print("\n--- ACTUATORS ---")
        for comp_type, control_type, comp_name in ACTUATORS_TO_CHECK:
            h = exchange.get_actuator_handle(state_arg, comp_type, control_type, comp_name)
            status = "PASS" if h != -1 else "FAIL"
            print(f"[{status}] Actuator  {comp_type}/{control_type}/{comp_name!r} -> handle={h}")

        api.runtime.stop_simulation(state_arg)

    api.runtime.callback_begin_system_timestep_before_predictor(state, on_timestep)

    idf = os.path.join(ENERGYPLUS_DIR, "ExampleFiles", "5ZoneAirCooled.idf")
    epw = os.path.join(
        ENERGYPLUS_DIR, "WeatherData", "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
    )
    exit_code = api.runtime.run_energyplus(
        state,
        ["-w", epw, "-d", OUT_DIR, "-r", idf],
    )
    if not checked["done"]:
        print("WARNING: simulation ended before handles were checked (exit code "
              f"{exit_code}) -- did warmup/sizing ever complete?")


if __name__ == "__main__":
    main()
