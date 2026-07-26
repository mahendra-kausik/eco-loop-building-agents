"""Deterministic rule-based controller: Phase 2's closed-loop proof, and the
safety supervisor's fallback in Phase 3 when the LLM times out, errors, or
returns invalid JSON (the loop must survive with the LLM totally unreachable).

Pure functions, no pyenergyplus import -- runnable and testable standalone.
"""
from src.tools.building_tools import ZONES, is_occupied_hour

# Occupancy-aware clamp (user decision, 2026-07-25): the baseline schedule lets
# cooling float to 29.4C when unoccupied. A flat [24,28] clamp would force the
# agent to cool an empty building harder than the baseline does, losing energy
# on nights/weekends for no comfort benefit (nobody's there to feel it).
OCCUPIED_HEATING_RANGE = (18.0, 23.0)
OCCUPIED_COOLING_RANGE = (24.0, 28.0)
UNOCCUPIED_HEATING_RANGE = (15.0, 23.0)
UNOCCUPIED_COOLING_RANGE = (24.0, 30.0)
MIN_DEADBAND_C = 1.0

# Phase 5 investigated, NOT shipped: optimal-start pre-heat (raising heating
# during the hours right before occupancy) to close the hour-8 cold-side PMV
# violation shared identically by baseline/floor/LLM (12/275 zone-hours, see
# docs/ARCHITECTURE.md). Measured, not assumed: tried 1h lead @ 21C (violation
# count UNCHANGED at 12), 2h lead @ 21C (still unchanged at 12), and 2h lead @
# 23C -- UNOCCUPIED_HEATING_RANGE's own ceiling, as aggressive as the locked
# clamp range allows (11/275, a 1-zone-hour dent) while reheat gas spiked from
# 0.0 to 51.2 kWh and HVAC savings fell 19.8%->10.5%. Independently confirmed
# structural rather than a scheduling gap: the baseline schedule ALREADY runs
# its AHU fan 06:00-20:00, a 2-hour lead over the 08:00-19:00 occupancy window,
# and has the identical violation anyway -- this building's zone thermal
# recovery is capacity-limited, not lead-time-limited, so no setpoint-only lever
# fixes it without a cost that outweighs the benefit. Reported here rather than
# silently dropped, per this project's practice of shipping measured results,
# not hoped ones.

# Phase 4: optimal stop for the AHU fan. FanAvailSched runs 06:00-20:00 in the
# baseline regardless of the 08:00-19:00 occupancy schedule -- 5%+ of total
# facility electricity spent conditioning an empty building. This margin is the
# hard guard's non-negotiable part: fans may only be forced off once zone temps
# are already comfortably below the cooling setpoint, never on request alone.
FAN_OFF_TEMP_MARGIN_C = 1.0


def clamp_fan_available(
    requested_off: bool,
    hour: int,
    day_of_week: int,
    max_zone_temp_c: float | None,
    cooling_setpoint_c: float,
) -> float:
    """The single source of truth for the fan safety guard (same role as
    clamp_setpoints, for the third actuator). Fans may only be forced OFF (0.0)
    when BOTH the hour being decided for AND the next hour are unoccupied, AND
    there's no latent cooling demand (max_zone_temp_c comfortably below the
    cooling setpoint -- None, e.g. no prior row yet, means "unknown" and is
    treated as demand present). Otherwise fans stay ON (1.0) no matter what was
    requested -- ventilation for occupants is never negotiable.

    Investigated, NOT gated on CO2: this building's DesignSpecification:OutdoorAir
    is per-person, so outdoor-air intake -- and CO2 removal -- drops to near zero
    once occupancy hits 0, regardless of fan state. Measured: max zone CO2 sits
    flat around 1060ppm for 6+ straight unoccupied hours with the fan ON the
    whole time, never dropping below a naive 1000ppm comfort threshold. Gating
    fan-off on "CO2 already below threshold" is therefore an unreachable
    condition for most of the night in this model -- it silently disabled every
    fan-off decision (0/113 unoccupied hours, was routinely >0 before), costing
    ~35 kWh HVAC for zero actual safety benefit, since this guard's own
    both-hours-unoccupied check already forces the fan back ON at least one hour
    before occupants arrive regardless of CO2 -- nobody is ever present under a
    fan-off decision. CO2 stays a first-class SENSED value (get_building_state,
    the LLM prompt) -- IAQ awareness without deadlocking a proven working
    optimal-stop feature on it."""
    next_hour = (hour + 1) % 24
    next_day_of_week = day_of_week if hour + 1 < 24 else (day_of_week % 7) + 1
    both_unoccupied = not is_occupied_hour(hour, day_of_week) and not is_occupied_hour(
        next_hour, next_day_of_week
    )
    no_cooling_demand = (
        max_zone_temp_c is not None and max_zone_temp_c < cooling_setpoint_c - FAN_OFF_TEMP_MARGIN_C
    )
    if requested_off and both_unoccupied and no_cooling_demand:
        return 0.0
    return 1.0


def clamp_setpoints(heating: float, cooling: float, occupied: bool) -> tuple[float, float]:
    """The single source of truth for the hard safety range. Always returns a
    legal (heating, cooling) pair: each within its occupancy-aware range, and
    heating <= cooling - MIN_DEADBAND_C (enforced by pulling heating down --
    cooling is the one occupants notice most in a hot climate)."""
    h_lo, h_hi = OCCUPIED_HEATING_RANGE if occupied else UNOCCUPIED_HEATING_RANGE
    c_lo, c_hi = OCCUPIED_COOLING_RANGE if occupied else UNOCCUPIED_COOLING_RANGE

    heating = min(max(heating, h_lo), h_hi)
    cooling = min(max(cooling, c_lo), c_hi)

    if heating > cooling - MIN_DEADBAND_C:
        heating = cooling - MIN_DEADBAND_C

    return heating, cooling


def fallback_controller(
    row: dict | None, day_of_year: int = 0, hour: int = 0, day_of_week: int = 0
) -> tuple[float, float, float]:
    """row is the most recently completed hourly reading (EnergyPlusRunner.rows[-1]),
    or None on the very first control hour before any row exists -- used only for
    its zone temps (the fan guard's cooling-demand check), everything else is
    predicted for the hour being decided FOR via is_occupied_hour(hour,
    day_of_week), not read reactively off the last completed row.

    Phase 4 correction: the original design used row["occupancy_frac"] (the LAST
    completed hour's occupancy) as a proxy for the hour about to start. At the
    exact hour occupancy begins, that last-completed row is still the unoccupied
    hour before, so the controller kept applying the wider unoccupied cooling
    ceiling (30C) for the first occupied hour -- caught by smoke_test.py's clamp
    check (cooling=29.0 logged against an occupied hour). day_of_year is still
    unused (occupancy has no seasonal dependence in this IDF).

    Returns (heating_c, cooling_c, fan_available).

    Phase 4 tuning: occupied cooling target raised 24.5 -> 25.0, unoccupied
    29.0 -> 29.5. Both stay well inside the existing hard range (occupied
    cooling ceiling is 28C, unoccupied 30C -- that range is a locked
    architecture decision, not reopened here); the baseline schedule itself
    over-cools during occupancy (23.9C, measured occupied PMV as low as -1.17
    overnight and -0.30 mid-day), so there was headroom on the warm side that
    is simultaneously an energy saving and, since PMV was already well inside
    [-0.5, 0.5], not a comfort cost."""
    occupied = is_occupied_hour(hour, day_of_week)

    if occupied:
        heating, cooling = 21.0, 25.0
    else:
        heating, cooling = 18.0, 29.5
    heating, cooling = clamp_setpoints(heating, cooling, occupied)

    max_zone_temp_c = max((row[f"{z}_temp_c"] for z in ZONES), default=None) if row is not None else None
    fan_available = clamp_fan_available(not occupied, hour, day_of_week, max_zone_temp_c, cooling)

    return heating, cooling, fan_available


def demo() -> None:
    """Runnable self-check -- no simulation needed to catch a broken clamp."""
    # In-range pair passes through unchanged.
    assert clamp_setpoints(21.0, 24.5, occupied=True) == (21.0, 24.5)

    # Out-of-range clamps to the occupancy-appropriate edges.
    assert clamp_setpoints(10.0, 40.0, occupied=True) == (18.0, 28.0)
    assert clamp_setpoints(10.0, 40.0, occupied=False) == (15.0, 30.0)

    # Unoccupied cooling ceiling (30) sits above the occupied one (28).
    assert clamp_setpoints(18.0, 29.0, occupied=False) == (18.0, 29.0)

    # Inverted pair gets corrected via the heating side, never violates deadband.
    h, c = clamp_setpoints(23.0, 24.0, occupied=True)
    assert h <= c - MIN_DEADBAND_C, (h, c)

    # fallback_controller predicts occupancy from (hour, day_of_week), not row --
    # day_of_week=3 is a weekday (WEEKEND_DAYS={1,7}), hour=10 is occupied (8-19),
    # hour=3 and weekend hour=10 are not. With no row (no zone temps to check),
    # the fan guard's cooling-demand check can never pass -> fan always stays on.
    assert fallback_controller(None, hour=3, day_of_week=3) == (18.0, 29.5, 1.0)
    assert fallback_controller(None, hour=10, day_of_week=3) == (21.0, 25.0, 1.0)
    assert fallback_controller(None, hour=10, day_of_week=1) == (18.0, 29.5, 1.0)  # Sunday

    # Fan guard: hour=2 and hour=3 (its "next hour") are both unoccupied weekday
    # hours. With cool zone temps (20C, well under cooling(29.5)-1.0=28.5), the
    # fan is allowed off.
    cool_row = {f"{z}_temp_c": 20.0 for z in ZONES}
    assert fallback_controller(cool_row, hour=2, day_of_week=3) == (18.0, 29.5, 0.0)

    # Same cool zone temps, but hour=7's next hour (8) is occupied -> guard keeps
    # the fan on despite the request, so occupants have ventilation when they arrive.
    assert fallback_controller(cool_row, hour=7, day_of_week=3) == (18.0, 29.5, 1.0)

    # Both hours unoccupied, but zone temps are still hot (30C > 28.5 threshold) --
    # latent cooling demand keeps the fan on too.
    hot_row = {f"{z}_temp_c": 30.0 for z in ZONES}
    assert fallback_controller(hot_row, hour=2, day_of_week=3) == (18.0, 29.5, 1.0)

    print("fallback.py: all assertions passed.")


if __name__ == "__main__":
    demo()
