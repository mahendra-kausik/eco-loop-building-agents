"""Builds models/baseline.idf from the shipped 5ZoneAirCooled.idf ExampleFile.

Per CLAUDE.md: never author an IDF from scratch, modify a shipped one minimally.
This always rebuilds baseline.idf fresh from the pristine source file, so it's
idempotent by construction -- no "has this already been patched?" bookkeeping.

Five changes, confirmed against the shipped file's IDD (C:\\EnergyPlusV26-1-0\\Energy+.idd)
rather than guessed:
  1. RunPeriod -> Jul 15-21 (the agreed 7-day horizon; Chicago weather pairs with it).
  2. Each of the 5 People objects gets the fields needed to report Fanger-model PMV.
     The field order matters and was pulled from the People object's IDD entry, not
     memory -- notably A7 (MRT calc type) is 'EnclosureAveraged' in this EnergyPlus
     version, not the older 'ZoneAveraged' name used in some docs/tutorials.
  3. Three new Schedule:Compact objects (clothing insulation, air velocity, outdoor
     CO2) support Fanger comfort + IAQ; work efficiency reuses the IDF's existing
     'Fraction' ScheduleTypeLimits and 'Any Number' covers the rest -- no new
     ScheduleTypeLimits needed.
  4. ZoneAirContaminantBalance (CO2 = Yes) turns on IAQ simulation. The 5 People
     objects' CO2 generation rate is left blank rather than filled in -- Energy+.idd
     confirms EnergyPlus auto-defaults a blank rate to the ASHRAE Std 62.1 value
     (3.82E-8 m3/s-W), so there's nothing to add there.
  5. An hourly Output:Variable / Output:Meter block, so judges can cross-check our
     computed kWh/PMV/CO2 against EnergyPlus's own eplusout.csv independent of our code.

Htg-SetP-Sch and Clg-SetP-Sch (the setpoint schedules) are deliberately left untouched --
they're both the baseline schedule and the Phase 2 actuation target.
"""
import os
import re

from src.simulation.eplus_path import ENERGYPLUS_DIR

SOURCE_IDF = os.path.join(ENERGYPLUS_DIR, "ExampleFiles", "5ZoneAirCooled.idf")
OUTPUT_IDF = os.path.join(
    os.path.dirname(__file__), "..", "..", "models", "baseline.idf"
)

RUN_PERIOD_PATTERN = re.compile(
    r"(RunPeriod,\s*\n"
    r"\s*Run Period 1,\s*!- Name\s*\n)"
    r"\s*\d+,\s*!- Begin Month\s*\n"
    r"\s*\d+,\s*!- Begin Day of Month\s*\n"
    r"(\s*,\s*!- Begin Year\s*\n)"
    r"\s*\d+,\s*!- End Month\s*\n"
    r"\s*\d+,\s*!- End Day of Month\s*\n"
)

# The exact trailer shared by all 5 People objects in the shipped file (confirmed:
# occurs exactly 5 times). Replacing it appends the Fanger-comfort fields (A6-A14
# per the People object's IDD entry) to every zone in one shot.
PEOPLE_TRAILER_OLD = "    ActSchd;                 !- Activity Level Schedule Name\n"
PEOPLE_TRAILER_NEW = (
    "    ActSchd,                 !- Activity Level Schedule Name\n"
    "    ,                        !- Carbon Dioxide Generation Rate\n"
    "    No,                      !- Enable ASHRAE 55 Comfort Warnings\n"
    "    EnclosureAveraged,       !- Mean Radiant Temperature Calculation Type\n"
    "    ,                        !- Surface Name/Angle Factor List Name\n"
    "    WorkEffSched,            !- Work Efficiency Schedule Name\n"
    "    ClothingInsulationSchedule,  !- Clothing Insulation Calculation Method\n"
    "    ,                        !- Clothing Insulation Calculation Method Schedule Name\n"
    "    ClothingInsulationSched, !- Clothing Insulation Schedule Name\n"
    "    AirVelocitySched,        !- Air Velocity Schedule Name\n"
    "    Fanger;                  !- Thermal Comfort Model 1 Type\n"
)

EXTRA_SCHEDULES = """
  Schedule:Compact,
    WorkEffSched,            !- Name
    Fraction,                !- Schedule Type Limits Name
    Through: 12/31,          !- Field 1
    For: AllDays,            !- Field 2
    Until: 24:00,0.0;        !- Field 3

  Schedule:Compact,
    ClothingInsulationSched, !- Name
    Any Number,              !- Schedule Type Limits Name
    Through: 12/31,          !- Field 1
    For: AllDays,            !- Field 2
    Until: 24:00,0.5;        !- Field 3

  Schedule:Compact,
    AirVelocitySched,        !- Name
    Any Number,              !- Schedule Type Limits Name
    Through: 12/31,          !- Field 1
    For: AllDays,            !- Field 2
    Until: 24:00,0.137;      !- Field 3

  Schedule:Compact,
    Outdoor CO2 Schedule,    !- Name
    Any Number,              !- Schedule Type Limits Name
    Through: 12/31,          !- Field 1
    For: AllDays,            !- Field 2
    Until: 24:00,400.0;      !- Field 3

  ZoneAirContaminantBalance,
    Yes,                     !- Carbon Dioxide Concentration
    Outdoor CO2 Schedule;    !- Outdoor Carbon Dioxide Schedule Name
"""

# IAQ (spec: "stream continuous performance metrics ... indoor air quality"). All
# 5 People objects already leave "Carbon Dioxide Generation Rate" blank, which
# EnergyPlus auto-fills with the ASHRAE Std 62.1 default (3.82E-8 m3/s-W, per
# Energy+.idd) -- confirmed against the shipped file, not guessed, and matches
# how EnergyPlus's own CO2-enabled example files (e.g.
# 1ZoneUncontrolled_Win_ResilienceReports.idf) leave it too. So enabling CO2
# above needs no People-object edit, just the contaminant balance + its outdoor
# schedule + this output variable.
EXTRA_OUTPUTS = """
  Output:Variable,*,Zone Mean Air Temperature,Hourly;
  Output:Variable,*,Site Outdoor Air Drybulb Temperature,Hourly;
  Output:Variable,*,Zone Thermal Comfort Fanger Model PMV,Hourly;
  Output:Variable,*,Zone Air Relative Humidity,Hourly;
  Output:Variable,*,Zone Air CO2 Concentration,Hourly;

  Output:Meter,Electricity:Building,Hourly;
  Output:Meter,Electricity:HVAC,Hourly;
  Output:Meter,Electricity:Plant,Hourly;
  Output:Meter,NaturalGas:Facility,Hourly;
"""


def build_baseline_idf(days: int = 7, out_path: str | None = None) -> str:
    """days=7 / out_path=None reproduces models/baseline.idf exactly (the
    deliverable). scripts/smoke_test.py passes a short `days` + a scratch
    `out_path` so its fast pre-commit run never touches the deliverable file."""
    with open(SOURCE_IDF, "r", encoding="latin-1") as f:
        text = f.read()

    end_day = 15 + days - 1
    if not 15 <= end_day <= 31:
        raise ValueError(f"days={days} pushes the run period past July (end day {end_day})")

    text, n_runperiod = RUN_PERIOD_PATTERN.subn(
        r"\g<1>    7,                       !- Begin Month\n"
        r"    15,                      !- Begin Day of Month\n"
        rf"\g<2>    7,                       !- End Month\n"
        rf"    {end_day},                      !- End Day of Month\n",
        text,
    )
    if n_runperiod != 1:
        raise RuntimeError(
            f"Expected exactly 1 RunPeriod match to patch, found {n_runperiod}. "
            "The shipped IDF's RunPeriod block may have changed format -- check "
            "RUN_PERIOD_PATTERN against the current source."
        )

    n_people = text.count(PEOPLE_TRAILER_OLD)
    if n_people != 5:
        raise RuntimeError(
            f"Expected exactly 5 People objects ending in the ActSchd trailer, "
            f"found {n_people}. The shipped IDF may have changed -- check "
            "PEOPLE_TRAILER_OLD against the current source."
        )
    text = text.replace(PEOPLE_TRAILER_OLD, PEOPLE_TRAILER_NEW)

    text = text.rstrip() + "\n" + EXTRA_SCHEDULES + EXTRA_OUTPUTS

    dest = out_path or OUTPUT_IDF
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="latin-1") as f:
        f.write(text)

    return dest


# Schedule:Compact objects this function rewrites, and their IDD Schedule Type
# Limits Name (confirmed against the shipped file, not guessed -- see
# scripts/probe_handles.py-style verification elsewhere in this project).
_RUNTIME_SCHEDULES = [
    ("Htg-SetP-Sch", "Temperature", "heating_setpoint_c"),
    ("Clg-SetP-Sch", "Temperature", "cooling_setpoint_c"),
    ("FanAvailSched", "Fraction", "fan_available"),
]


def export_runtime_idf(rows: list[dict], base_idf_path: str, out_path: str) -> str:
    """Bakes one run's ACTUAL applied setpoint/fan values into a standalone IDF --
    deliverable 2's "modified version generated during runtime evaluation",
    literally.

    Per CLAUDE.md's locked architecture, control happens via live
    set_actuator_value() calls during the run, never by rewriting IDF text --
    this function doesn't feed back into the simulation and nothing re-reads it.
    It's a post-hoc, human-readable record of what the controller actually did,
    hour by hour, replacing Htg-SetP-Sch/Clg-SetP-Sch/FanAvailSched's
    Schedule:Compact objects with the real applied values so a diff against
    models/baseline.idf makes the agent's behaviour visible without reading code.

    rows: EnergyPlusRunner.rows (or state.csv re-read into dicts) from a
    completed run -- called once, after the run, never before."""
    with open(base_idf_path, "r", encoding="latin-1") as f:
        text = f.read()

    # RunPeriod is always Jul 15 + offset (see build_baseline_idf's end_day calc)
    # -- rows are already in run order, so sorted day_of_year values map onto
    # July 15, 16, 17... positionally, no calendar/leap-year math needed.
    unique_days = sorted({int(r["day_of_year"]) for r in rows})
    calendar_day = {doy: 15 + i for i, doy in enumerate(unique_days)}

    def schedule_block(name: str, type_limits: str, value_key: str) -> str:
        lines = [f"  Schedule:Compact,\n    {name},\n    {type_limits},\n"]
        for doy in unique_days:
            day_rows = sorted(
                (r for r in rows if int(r["day_of_year"]) == doy), key=lambda r: int(r["hour"])
            )
            lines.append(f"    Through: 7/{calendar_day[doy]},\n    For: AllDays,\n")
            for r in day_rows:
                until = f"{int(r['hour']) + 1:02d}:00"
                lines.append(f"    Until: {until},{float(r[value_key]):.1f},\n")
        block = "".join(lines).rstrip()
        return block[:-1] + ";\n"  # trailing comma -> semicolon: IDD's object terminator

    for name, type_limits, value_key in _RUNTIME_SCHEDULES:
        # ";[^\n]*\n", not ";\n" -- a field's terminating semicolon is often
        # followed by a same-line "!- Field N" comment before the real newline
        # (confirmed in the shipped IDF), and a bare ";\n" would jump straight
        # past that into the NEXT object's terminator instead of this one's.
        pattern = re.compile(
            rf"[ \t]*Schedule:Compact,\s*\n\s*{re.escape(name)},.*?;[^\n]*\n", re.DOTALL
        )
        text, n = pattern.subn(schedule_block(name, type_limits, value_key), text, count=1)
        if n != 1:
            raise RuntimeError(
                f"Expected exactly 1 Schedule:Compact block for {name!r} to replace, found {n}"
            )

    header = (
        "! Runtime-modified IDF: the ACTUAL heating/cooling/fan schedule values\n"
        "! this agent run applied, hour by hour -- baked from results/raw's\n"
        "! state.csv, not hand-edited. Not simulation input for the live loop\n"
        "! (setpoints are injected via the pyenergyplus API at runtime, never by\n"
        "! rewriting IDF text -- see CLAUDE.md's locked architecture decision);\n"
        "! this file is a readable record for diffing against models/baseline.idf.\n"
    )
    text = header + text

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="latin-1") as f:
        f.write(text)
    return out_path


def demo() -> None:
    """Self-check on a tiny fabricated IDF fragment -- doesn't need the real
    E+ install or ENERGYPLUS_DIR, matching the no-E+-needed self-check pattern
    the rest of the codebase uses (fallback.py, metrics.py, dashboard.py)."""
    import shutil
    import tempfile

    fake_idf = (
        "  Schedule:Compact,\n    Htg-SetP-Sch,            !- Name\n"
        "    Temperature,             !- Schedule Type Limits Name\n"
        "    Through: 12/31,          !- Field 1\n    For: AllDays,\n"
        "    Until: 24:00,21.0;       !- Field 3\n\n"
        "  Schedule:Compact,\n    Clg-SetP-Sch,            !- Name\n"
        "    Temperature,             !- Schedule Type Limits Name\n"
        "    Until: 24:00,24.0;\n\n"
        "  Schedule:Compact,\n    FanAvailSched,           !- Name\n"
        "    Fraction,                !- Schedule Type Limits Name\n"
        "    Until: 24:00,1.0;\n\n"
        "  Schedule:Compact,\n    PlenumHtg-SetP-Sch,      !- Name\n"
        "    Temperature,             !- Schedule Type Limits Name\n"
        "    Until: 24:00,12.8;       !- untouched: not one of the 3 runtime schedules\n"
    )
    tmp_dir = tempfile.mkdtemp(prefix="idf_prep_demo_")
    try:
        base_path = os.path.join(tmp_dir, "fake_base.idf")
        with open(base_path, "w", encoding="latin-1") as f:
            f.write(fake_idf)

        rows = [
            {"day_of_year": 196, "hour": 0, "heating_setpoint_c": 15.0, "cooling_setpoint_c": 29.5, "fan_available": 0.0},
            {"day_of_year": 196, "hour": 8, "heating_setpoint_c": 21.0, "cooling_setpoint_c": 25.0, "fan_available": 1.0},
            {"day_of_year": 197, "hour": 0, "heating_setpoint_c": 15.0, "cooling_setpoint_c": 29.5, "fan_available": 0.0},
        ]
        out_path = os.path.join(tmp_dir, "fake_runtime.idf")
        export_runtime_idf(rows, base_path, out_path)

        with open(out_path, encoding="latin-1") as f:
            out_text = f.read()

        assert "Until: 01:00,15.0," in out_text  # hour 0 -> ends at 01:00
        assert "Until: 09:00,21.0," in out_text  # hour 8 -> ends at 09:00
        assert "Through: 7/15," in out_text and "Through: 7/16," in out_text
        assert "PlenumHtg-SetP-Sch" in out_text  # untouched schedule survives
        assert "12.8" in out_text  # ... with its own value unchanged
        # Old block fully replaced, not duplicated -- "Htg-SetP-Sch," also occurs
        # as a substring of "PlenumHtg-SetP-Sch," so anchor on the 4-space-indented
        # standalone field to count only the schedule we actually rewrote.
        assert out_text.count("\n    Htg-SetP-Sch,\n") == 1
        assert "Runtime-modified IDF" in out_text  # header present

        print("idf_prep.py: all assertions passed.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    path = build_baseline_idf()
    print(f"Wrote {path}")
    print("Changes applied: RunPeriod -> Jul 15-21, Fanger comfort on 5 People objects, "
          "CO2/IAQ simulation enabled, 4 supporting schedules, hourly Output:Variable/Meter block.")
