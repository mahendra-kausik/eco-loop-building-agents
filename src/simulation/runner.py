"""Phase 1+2: E+ <-> Python closed loop.

EnergyPlusRunner owns one simulation: it resolves variable/meter/actuator handles
once (after warmup, guarding -1 per CLAUDE.md), streams one row per simulated hour
into an in-memory buffer + CSV, and -- if given a `controller` -- writes new
setpoints back in once per simulated hour.

Two callbacks, deliberately different: actuation happens at
`callback_begin_system_timestep_before_predictor` (setpoints must be pushed
before the predictor runs, so they affect that timestep's HVAC calc). Meter
*reads* happen at `callback_end_zone_timestep_after_zone_reporting`, not any
system-timestep callback -- see the correction note below.

Meter values are also not cumulative: `get_meter_value` documents itself as
"the instantaneous value ... not the cumulative value" -- i.e. the energy (J)
added since the meter's last reset. With Timestep=4 (15-min), a *zone*
timestep boundary is crossed exactly 4 times per simulated hour, so this
runner accumulates the zone-timestep values in Python and appends a row once
per hour, converting J -> kWh with /3.6e6.

CORRECTION (Phase 4): this originally read meters on
`callback_end_system_timestep_after_hvac_reporting`. That callback fires once
per *system* timestep, and the HVAC manager subdivides the zone timestep into
extra system sub-timesteps during load ramps (mornings, evenings, occupancy
transitions) to converge -- each sub-timestep's meter value already reflects
everything accumulated since the last zone-timestep reset, so summing every
system-timestep call double-(or triple-)counted exactly on the hours where
all the interesting control action happens. Verified against EnergyPlus's own
`eplusmtr.csv`: the old code overstated total facility electricity by 16.5%
on a steady baseline run, concentrated at ramp hours (2.66x at hour 20) while
matching almost exactly during flat occupied hours (10-17). Reading at the
zone-timestep boundary instead -- which E+ crosses exactly once per zone
timestep regardless of how many system sub-timesteps it took -- fixes this.
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
FIXED_LOAD_METER = "Electricity:Building"  # lighting + plug load -- not setpoint-controllable
HVAC_SUBMETERS = ["Electricity:HVAC", "Electricity:Plant"]
ELECTRICITY_SUBMETERS = [FIXED_LOAD_METER] + HVAC_SUBMETERS
GAS_METER = "NaturalGas:Facility"

# Setpoint schedules -- read-only in Phase 1, actuated in Phase 2.
HEATING_SETPOINT_SCHEDULE = "Htg-SetP-Sch"
COOLING_SETPOINT_SCHEDULE = "Clg-SetP-Sch"

# Phase 4: third actuator, optimal start/stop for the AHU. Baseline IDF runs this
# schedule 06:00-20:00 regardless of the 08:00-19:00 occupancy schedule --
# conditioning an empty building for 2 hours daily. Actuated the same way as the
# setpoint schedules (Schedule:Compact "Schedule Value").
FAN_AVAIL_SCHEDULE = "FanAvailSched"

# Shared by all 5 zones (see People/OCCUPY-1 objects in baseline.idf) -- reading
# it directly means the controller/Phase 4 comfort metric never has to reimplement
# weekday/weekend/holiday calendar logic that already lives in the schedule.
OCCUPANCY_SCHEDULE = "OCCUPY-1"

J_PER_KWH = 3.6e6

# Controller signature: takes the last completed hourly row (or None before the
# first hour exists), plus the day/hour/day-of-week being decided FOR (rows[-1] is
# the hour before -- passing the target hour explicitly is what lets a controller
# see occupancy/forecast context ahead of the row that would otherwise reveal it,
# e.g. pre-cooling before occupancy starts), and returns
# (heating_setpoint_c, cooling_setpoint_c, fan_available).
Controller = Callable[[Optional[dict], int, int, int], tuple[float, float, float]]


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
        self._hour_hvac_j = 0.0
        self._hour_gas_j = 0.0
        self._cumulative_electricity_kwh = 0.0
        self._cumulative_gas_kwh = 0.0
        self._current_hour_key: Optional[tuple[int, int]] = None
        self._last_snapshot: Optional[dict] = None
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
            ("Schedule Value", FAN_AVAIL_SCHEDULE),
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

        for sched_name in (HEATING_SETPOINT_SCHEDULE, COOLING_SETPOINT_SCHEDULE, FAN_AVAIL_SCHEDULE):
            handle = self.exchange.get_actuator_handle(
                self.state, "Schedule:Compact", "Schedule Value", sched_name
            )
            if handle == -1:
                raise RuntimeError(f"Could not resolve actuator handle for schedule {sched_name!r}")
            h[("actuator", sched_name)] = handle

        self._handles = h
        self._handles_resolved = True

    def _maybe_flush_hour(self, day_of_year: int, hour: int) -> None:
        """Detects an hour transition and flushes the just-completed hour's row.
        Called from BOTH callbacks -- not just _on_timestep -- because of a
        callback-ordering subtlety: within one system timestep,
        callback_begin_system_timestep_before_predictor fires BEFORE
        callback_end_zone_timestep_after_zone_reporting, and BOTH observe the
        hour() rollover at the exact same call (confirmed empirically: hour()
        and minutes() read identically in both callbacks throughout a run).
        If only _on_timestep called this, the controller (fired from
        _on_begin_timestep, which runs first) would read self.rows[-1] one hour
        stale -- it'd see hour H-2's row while deciding for hour H, since hour
        H-1's row wouldn't be flushed until _on_timestep's turn, later in that
        same system timestep. Calling it from _on_begin_timestep too means
        whichever callback observes the transition first (always begin-timestep,
        per the above) does the flush before anything reads self.rows[-1].
        Idempotent: a no-op once _current_hour_key already matches."""
        new_key = (day_of_year, hour)
        if self._current_hour_key is None:
            self._current_hour_key = new_key
        elif new_key != self._current_hour_key:
            self._flush_row()
            self._current_hour_key = new_key

    def _on_timestep(self, state) -> None:
        exchange = self.exchange
        if not exchange.api_data_fully_ready(state) or exchange.warmup_flag(state):
            return
        if not self._handles_resolved:
            self._resolve_handles()

        # Flush trigger: the simulated *hour* has changed since the last call, not
        # `minutes(state) % 60 == 0`. E+ can shorten zone timesteps below 15 min for
        # convergence, so a fixed-hour build can skip minutes==60 entirely on some
        # hours -- that silently dropped whole hours and folded their energy into
        # the next hour that *did* land on the boundary (see module docstring).
        # hour() itself only advances on real clock-hour boundaries regardless of
        # how the zone timestep was subdivided, so comparing it call-to-call is the
        # robust trigger. (See _maybe_flush_hour's docstring for why this same
        # check also has to run from _on_begin_timestep.)
        self._maybe_flush_hour(exchange.day_of_year(state), exchange.hour(state))

        # Accumulate this zone timestep's energy into the (possibly just-reset)
        # bucket. (Zone-timestep boundary, not system-timestep -- see module
        # docstring: this avoids the HVAC-iteration double-counting bug.)
        for meter_name in ELECTRICITY_SUBMETERS:
            self._hour_electricity_j += exchange.get_meter_value(state, self._handles[("meter", meter_name)])
        for meter_name in HVAC_SUBMETERS:
            self._hour_hvac_j += exchange.get_meter_value(state, self._handles[("meter", meter_name)])
        self._hour_gas_j += exchange.get_meter_value(state, self._handles[("meter", GAS_METER)])

        # Snapshot state readings on every call, overwriting the previous one --
        # when this hour eventually flushes, the snapshot in effect is from the
        # last zone timestep actually inside that hour (i.e. its end-of-hour state).
        snapshot = {
            "outdoor_temp_c": exchange.get_variable_value(
                state, self._handles[("var", OUTDOOR_TEMP_VARIABLE, "Environment")]
            ),
            "heating_setpoint_c": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", HEATING_SETPOINT_SCHEDULE)]
            ),
            "cooling_setpoint_c": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", COOLING_SETPOINT_SCHEDULE)]
            ),
            "occupancy_frac": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", OCCUPANCY_SCHEDULE)]
            ),
            "fan_available": exchange.get_variable_value(
                state, self._handles[("var", "Schedule Value", FAN_AVAIL_SCHEDULE)]
            ),
        }
        for zone in ZONES:
            snapshot[f"{zone}_temp_c"] = exchange.get_variable_value(
                state, self._handles[("var", TEMP_VARIABLE, zone)]
            )
            snapshot[f"{zone}_rh_pct"] = exchange.get_variable_value(
                state, self._handles[("var", HUMIDITY_VARIABLE, zone)]
            )
        for zone, person in zip(ZONES, PEOPLE_OBJECTS):
            snapshot[f"{zone}_pmv"] = exchange.get_variable_value(
                state, self._handles[("var", PMV_VARIABLE, person)]
            )
        self._last_snapshot = snapshot

    def _flush_row(self) -> None:
        """Emit the accumulated bucket for self._current_hour_key as one row, then
        reset the accumulators. Called on every hour transition, plus once more
        after the sim ends (run() calls it directly) to emit the final hour, which
        has no following transition to trigger it."""
        if self._current_hour_key is None or self._last_snapshot is None:
            return  # nothing accumulated yet (called before the first real hour)

        day_of_year, hour = self._current_hour_key
        snap = self._last_snapshot
        hour_kwh = self._hour_electricity_j / J_PER_KWH
        hour_hvac_kwh = self._hour_hvac_j / J_PER_KWH
        hour_gas_kwh = self._hour_gas_j / J_PER_KWH
        self._cumulative_electricity_kwh += hour_kwh
        self._cumulative_gas_kwh += hour_gas_kwh

        row = {
            "day_of_year": day_of_year,
            "hour": hour,
            "outdoor_temp_c": snap["outdoor_temp_c"],
            "electricity_kwh_this_hour": hour_kwh,
            "cumulative_electricity_kwh": self._cumulative_electricity_kwh,
            "hvac_kwh_this_hour": hour_hvac_kwh,
            "fixed_kwh_this_hour": hour_kwh - hour_hvac_kwh,
            "gas_kwh_this_hour": hour_gas_kwh,
            "cumulative_gas_kwh": self._cumulative_gas_kwh,
            "heating_setpoint_c": snap["heating_setpoint_c"],
            "cooling_setpoint_c": snap["cooling_setpoint_c"],
            "occupancy_frac": snap["occupancy_frac"],
            "fan_available": snap["fan_available"],
        }
        for zone in ZONES:
            row[f"{zone}_temp_c"] = snap[f"{zone}_temp_c"]
            row[f"{zone}_rh_pct"] = snap[f"{zone}_rh_pct"]
        for zone in ZONES:
            row[f"{zone}_pmv"] = snap[f"{zone}_pmv"]

        self.rows.append(row)
        self._hour_electricity_j = 0.0
        self._hour_hvac_j = 0.0
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
        # Flush the previous hour's row (if this is the first call of a new hour)
        # BEFORE the dedup check below -- see _maybe_flush_hour's docstring: this
        # callback always observes the hour rollover before _on_timestep does, so
        # without this call here, self.rows[-1] just below would be one hour stale.
        self._maybe_flush_hour(*hour_key)
        if hour_key == self._last_control_hour:
            return  # already actuated this simulated hour
        self._last_control_hour = hour_key

        try:
            last_row = self.rows[-1] if self.rows else None
            day_of_year, hour = hour_key
            day_of_week = exchange.day_of_week(state)
            heating_c, cooling_c, fan_available = self.controller(last_row, day_of_year, hour, day_of_week)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            self.controller_errors.append((hour_key[0], hour_key[1], str(exc)))
            return  # leave whatever setpoints are already applied in place

        exchange.set_actuator_value(state, self._handles[("actuator", HEATING_SETPOINT_SCHEDULE)], heating_c)
        exchange.set_actuator_value(state, self._handles[("actuator", COOLING_SETPOINT_SCHEDULE)], cooling_c)
        exchange.set_actuator_value(state, self._handles[("actuator", FAN_AVAIL_SCHEDULE)], fan_available)
        self.injections += 1

    def run(self) -> int:
        self._request_variables()
        self.runtime.callback_end_zone_timestep_after_zone_reporting(self.state, self._on_timestep)
        self.runtime.callback_begin_system_timestep_before_predictor(self.state, self._on_begin_timestep)
        exit_code = self.runtime.run_energyplus(
            self.state,
            ["-w", self.epw_path, "-d", self.output_dir, "-r", self.idf_path],
        )
        self._flush_row()  # emit the final hour -- no further transition will trigger it
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
