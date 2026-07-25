"""Phase 1+2: E+ <-> Python closed loop.

EnergyPlusRunner owns one simulation: it resolves variable/meter/actuator handles
once (after warmup, guarding -1 per CLAUDE.md), streams one row per simulated hour
into an in-memory buffer + CSV, and -- if given a `controller` -- writes new
setpoints back in once per simulated hour.

Two callbacks, deliberately different, per CLAUDE.md's "begin system timestep
before predictor" line: that callback is for *actuation* (setpoints must be
pushed before the predictor runs, so they affect that timestep's HVAC calc). For
*reading* variables/meters, the value for a timestep isn't finalized until HVAC
has actually run, so reads happen at `callback_end_system_timestep_after_hvac_reporting`
and writes happen at `callback_begin_system_timestep_before_predictor`.

Meter values are also not cumulative: `get_meter_value` documents itself as
"the instantaneous value ... not the cumulative value" -- i.e. the energy (J) added
during that one system timestep. With Timestep=4 (15-min), that's 4 calls per
simulated hour, so this runner accumulates them in Python and only appends a row
once per hour (on the timestep that lands on minutes==0), converting J -> kWh
with /3.6e6.
"""
import csv
import os
from typing import Callable, Optional

from src.simulation.eplus_path import EnergyPlusAPI

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]
PEOPLE_OBJECTS = [f"{z} PEOPLE 1" for z in ZONES]

TEMP_VARIABLE = "Zone Mean Air Temperature"
OUTDOOR_TEMP_VARIABLE = "Site Outdoor Air Drybulb Temperature"
PMV_VARIABLE = "Zone Thermal Comfort Fanger Model PMV"
HUMIDITY_VARIABLE = "Zone Air Relative Humidity"

# Confirmed via scripts/probe_handles.py: 'Electricity:Facility' is listed in
# list_available_api_data_csv() but reproducibly fails to resolve via
# get_meter_handle() on this EnergyPlus install. This IDF has no Exterior:*
# electric objects, so Building + HVAC + Plant == Facility total exactly.
ELECTRICITY_SUBMETERS = ["Electricity:Building", "Electricity:HVAC", "Electricity:Plant"]
GAS_METER = "NaturalGas:Facility"

# Setpoint schedules -- read-only in Phase 1, actuated in Phase 2.
HEATING_SETPOINT_SCHEDULE = "Htg-SetP-Sch"
COOLING_SETPOINT_SCHEDULE = "Clg-SetP-Sch"

# Shared by all 5 zones (see People/OCCUPY-1 objects in baseline.idf) -- reading
# it directly means the controller/Phase 4 comfort metric never has to reimplement
# weekday/weekend/holiday calendar logic that already lives in the schedule.
OCCUPANCY_SCHEDULE = "OCCUPY-1"

J_PER_KWH = 3.6e6

# Controller signature: takes the last completed hourly row (or None before the
# first hour exists) and returns (heating_setpoint_c, cooling_setpoint_c).
Controller = Callable[[Optional[dict]], tuple[float, float]]


class EnergyPlusRunner:
    def __init__(
        self,
        idf_path: str,
        epw_path: str,
        output_dir: str,
        controller: Optional[Controller] = None,
    ):
        """controller=None reproduces Phase 1 exactly: no actuation, read-only run."""
        self.idf_path = idf_path
        self.epw_path = epw_path
        self.output_dir = output_dir
        self.controller = controller
        os.makedirs(output_dir, exist_ok=True)

        self.api = EnergyPlusAPI()
        self.state = self.api.state_manager.new_state()
        self.exchange = self.api.exchange
        self.runtime = self.api.runtime

        self._handles: dict = {}
        self._handles_resolved = False
        self._hour_electricity_j = 0.0
        self._hour_gas_j = 0.0
        self._cumulative_electricity_kwh = 0.0
        self._cumulative_gas_kwh = 0.0
        self._last_control_hour: Optional[tuple[int, int]] = None
        self.rows: list[dict] = []
        self.injections = 0
        self.controller_errors: list[tuple[int, int, str]] = []

    def _request_variables(self) -> None:
        for var_name, key in self._variable_specs():
            self.exchange.request_variable(self.state, var_name, key)

    @staticmethod
    def _variable_specs():
        specs = [(TEMP_VARIABLE, z) for z in ZONES]
        specs.append((OUTDOOR_TEMP_VARIABLE, "Environment"))
        specs += [(PMV_VARIABLE, p) for p in PEOPLE_OBJECTS]
        specs += [(HUMIDITY_VARIABLE, z) for z in ZONES]
        # 'Schedule Value' as a plain variable gives the schedule's actual current
        # number. get_actuator_value() on the same schedule reads back an override
        # instead (0.0 until something calls set_actuator_value -- confirmed by
        # running Phase 1 read-only and seeing constant 0.0s), so it's the wrong
        # read for a read-only baseline; the actuator handle is still resolved
        # separately below, for Phase 2's set_actuator_value() calls.
        specs += [
            ("Schedule Value", HEATING_SETPOINT_SCHEDULE),
            ("Schedule Value", COOLING_SETPOINT_SCHEDULE),
            ("Schedule Value", OCCUPANCY_SCHEDULE),
        ]
        return specs

    def _resolve_handles(self) -> None:
        """Resolve every handle once. A -1 handle raises immediately, naming the
        offending variable/meter/actuator, per CLAUDE.md's fail-loudly-in-setup rule."""
        h = {}
        for var_name, key in self._variable_specs():
            handle = self.exchange.get_variable_handle(self.state, var_name, key)
            if handle == -1:
                raise RuntimeError(
                    f"Could not resolve variable handle for {var_name!r} @ key {key!r} "
                    "-- is api_data_fully_ready/warmup actually done, or did the IDF change?"
                )
            h[("var", var_name, key)] = handle

        for meter_name in ELECTRICITY_SUBMETERS + [GAS_METER]:
            handle = self.exchange.get_meter_handle(self.state, meter_name)
            if handle == -1:
                raise RuntimeError(f"Could not resolve meter handle for {meter_name!r}")
            h[("meter", meter_name)] = handle

        for sched_name in (HEATING_SETPOINT_SCHEDULE, COOLING_SETPOINT_SCHEDULE):
            handle = self.exchange.get_actuator_handle(
                self.state, "Schedule:Compact", "Schedule Value", sched_name
            )
            if handle == -1:
                raise RuntimeError(f"Could not resolve actuator handle for schedule {sched_name!r}")
            h[("actuator", sched_name)] = handle

        self._handles = h
        self._handles_resolved = True

    def _on_timestep(self, state) -> None:
        exchange = self.exchange
        if not exchange.api_data_fully_ready(state) or exchange.warmup_flag(state):
            return
        if not self._handles_resolved:
            self._resolve_handles()

        # Accumulate this system timestep's energy into the running hour bucket.
        for meter_name in ELECTRICITY_SUBMETERS:
            self._hour_electricity_j += exchange.get_meter_value(state, self._handles[("meter", meter_name)])
        self._hour_gas_j += exchange.get_meter_value(state, self._handles[("meter", GAS_METER)])

        # At this end-of-timestep callback, minutes() reads 15/30/45/60 (never 0) --
        # confirmed against this install; 60 marks the hour boundary, not 0.
        if exchange.minutes(state) % 60 != 0:
            return  # not an hour boundary yet -- only append a row once/hour

        hour_kwh = self._hour_electricity_j / J_PER_KWH
        hour_gas_kwh = self._hour_gas_j / J_PER_KWH
        self._cumulative_electricity_kwh += hour_kwh
        self._cumulative_gas_kwh += hour_gas_kwh

        row = {
            "day_of_year": exchange.day_of_year(state),
            "hour": exchange.hour(state),
            "outdoor_temp_c": exchange.get_variable_value(
                state, self._handles[("var", OUTDOOR_TEMP_VARIABLE, "Environment")]
            ),
            "electricity_kwh_this_hour": hour_kwh,
            "cumulative_electricity_kwh": self._cumulative_electricity_kwh,
            "gas_kwh_this_hour": hour_gas_kwh,
            "cumulative_gas_kwh": self._cumulative_gas_kwh,
            "heating_setpoint_c": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", HEATING_SETPOINT_SCHEDULE)]
            ),
            "cooling_setpoint_c": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", COOLING_SETPOINT_SCHEDULE)]
            ),
            "occupancy_frac": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", OCCUPANCY_SCHEDULE)]
            ),
        }
        for zone in ZONES:
            row[f"{zone}_temp_c"] = exchange.get_variable_value(
                state, self._handles[("var", TEMP_VARIABLE, zone)]
            )
            row[f"{zone}_rh_pct"] = exchange.get_variable_value(
                state, self._handles[("var", HUMIDITY_VARIABLE, zone)]
            )
        for zone, person in zip(ZONES, PEOPLE_OBJECTS):
            row[f"{zone}_pmv"] = exchange.get_variable_value(
                state, self._handles[("var", PMV_VARIABLE, person)]
            )

        self.rows.append(row)
        self._hour_electricity_j = 0.0
        self._hour_gas_j = 0.0

        print(
            f"[day {row['day_of_year']:>3} hour {row['hour']:>2}] "
            f"outdoor={row['outdoor_temp_c']:5.1f}C  "
            f"SPACE1-1={row['SPACE1-1_temp_c']:5.1f}C pmv={row['SPACE1-1_pmv']:+.2f}  "
            f"kWh_this_hr={hour_kwh:6.2f}  cum_kWh={self._cumulative_electricity_kwh:8.2f}"
        )

    def _on_begin_timestep(self, state) -> None:
        """Runs before the predictor, once per system timestep. Fires the
        controller (if any) once per simulated hour and actuates its result.
        Per CLAUDE.md: runtime errors here are caught and answered with the
        last-known-good setpoints -- the simulation must never die because of
        the agent."""
        if self.controller is None:
            return
        exchange = self.exchange
        if not exchange.api_data_fully_ready(state) or exchange.warmup_flag(state):
            return
        if not self._handles_resolved:
            self._resolve_handles()

        hour_key = (exchange.day_of_year(state), exchange.hour(state))
        if hour_key == self._last_control_hour:
            return  # already actuated this simulated hour
        self._last_control_hour = hour_key

        try:
            last_row = self.rows[-1] if self.rows else None
            heating_c, cooling_c = self.controller(last_row)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            self.controller_errors.append((hour_key[0], hour_key[1], str(exc)))
            return  # leave whatever setpoints are already applied in place

        exchange.set_actuator_value(state, self._handles[("actuator", HEATING_SETPOINT_SCHEDULE)], heating_c)
        exchange.set_actuator_value(state, self._handles[("actuator", COOLING_SETPOINT_SCHEDULE)], cooling_c)
        self.injections += 1

    def run(self) -> int:
        self._request_variables()
        self.runtime.callback_end_system_timestep_after_hvac_reporting(self.state, self._on_timestep)
        self.runtime.callback_begin_system_timestep_before_predictor(self.state, self._on_begin_timestep)
        exit_code = self.runtime.run_energyplus(
            self.state,
            ["-w", self.epw_path, "-d", self.output_dir, "-r", self.idf_path],
        )
        self._write_csv()
        return exit_code

    def _write_csv(self, path: Optional[str] = None) -> str:
        path = path or os.path.join(self.output_dir, "state.csv")
        if not self.rows:
            raise RuntimeError("No rows were collected -- did the simulation ever leave warmup?")
        fieldnames = list(self.rows[0].keys())
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.rows)
        print(f"Wrote {len(self.rows)} hourly rows -> {path}")
        return path
