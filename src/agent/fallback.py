"""Deterministic rule-based controller: Phase 2's closed-loop proof, and the
safety supervisor's fallback in Phase 3 when the LLM times out, errors, or
returns invalid JSON (per CLAUDE.md, the loop must survive with the LLM
totally unreachable).

Pure functions, no pyenergyplus import -- runnable and testable standalone.
"""

# Occupancy-aware clamp (user decision, 2026-07-25): the baseline schedule lets
# cooling float to 29.4C when unoccupied. A flat [24,28] clamp would force the
# agent to cool an empty building harder than the baseline does, losing energy
# on nights/weekends for no comfort benefit (nobody's there to feel it).
OCCUPIED_HEATING_RANGE = (18.0, 23.0)
OCCUPIED_COOLING_RANGE = (24.0, 28.0)
UNOCCUPIED_HEATING_RANGE = (15.0, 23.0)
UNOCCUPIED_COOLING_RANGE = (24.0, 30.0)
MIN_DEADBAND_C = 1.0


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


def fallback_controller(row: dict | None) -> tuple[float, float]:
    """row is the most recently completed hourly reading (EnergyPlusRunner.rows[-1]),
    or None on the very first control hour before any row exists. Occupancy comes
    from the OCCUPY-1 schedule value already in the row, not calendar math."""
    occupied = row is not None and row.get("occupancy_frac", 0.0) > 0.0

    if occupied:
        heating, cooling = 21.0, 24.5
    else:
        heating, cooling = 18.0, 29.0

    return clamp_setpoints(heating, cooling, occupied)


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

    # fallback_controller returns a legal pair either side of occupancy.
    assert fallback_controller(None) == (18.0, 29.0)
    assert fallback_controller({"occupancy_frac": 1.0}) == (21.0, 24.5)
    assert fallback_controller({"occupancy_frac": 0.0}) == (18.0, 29.0)

    print("fallback.py: all assertions passed.")


if __name__ == "__main__":
    demo()
