"""Builds models/baseline.idf from the shipped 5ZoneAirCooled.idf ExampleFile.

Per CLAUDE.md: never author an IDF from scratch, modify a shipped one minimally.
This always rebuilds baseline.idf fresh from the pristine source file, so it's
idempotent by construction -- no "has this already been patched?" bookkeeping.

Four changes, confirmed against the shipped file's IDD (C:\\EnergyPlusV26-1-0\\Energy+.idd)
rather than guessed:
  1. RunPeriod -> Jul 15-21 (the agreed 7-day horizon; Chicago weather pairs with it).
  2. Each of the 5 People objects gets the fields needed to report Fanger-model PMV.
     The field order matters and was pulled from the People object's IDD entry, not
     memory -- notably A7 (MRT calc type) is 'EnclosureAveraged' in this EnergyPlus
     version, not the older 'ZoneAveraged' name used in some docs/tutorials.
  3. Two new Schedule:Compact objects (clothing insulation, air velocity) support the
     Fanger fields; work efficiency reuses the IDF's existing 'Fraction' ScheduleTypeLimits
     and 'Any Number' covers the other two -- no new ScheduleTypeLimits needed.
  4. An hourly Output:Variable / Output:Meter block, so judges can cross-check our
     computed kWh/PMV against EnergyPlus's own eplusout.csv independent of our code.

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
"""

EXTRA_OUTPUTS = """
  Output:Variable,*,Zone Mean Air Temperature,Hourly;
  Output:Variable,*,Site Outdoor Air Drybulb Temperature,Hourly;
  Output:Variable,*,Zone Thermal Comfort Fanger Model PMV,Hourly;
  Output:Variable,*,Zone Air Relative Humidity,Hourly;

  Output:Meter,Electricity:Building,Hourly;
  Output:Meter,Electricity:HVAC,Hourly;
  Output:Meter,Electricity:Plant,Hourly;
  Output:Meter,NaturalGas:Facility,Hourly;
"""


def build_baseline_idf() -> str:
    with open(SOURCE_IDF, "r", encoding="latin-1") as f:
        text = f.read()

    text, n_runperiod = RUN_PERIOD_PATTERN.subn(
        r"\g<1>    7,                       !- Begin Month\n"
        r"    15,                      !- Begin Day of Month\n"
        r"\g<2>    7,                       !- End Month\n"
        r"    21,                      !- End Day of Month\n",
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

    os.makedirs(os.path.dirname(OUTPUT_IDF), exist_ok=True)
    with open(OUTPUT_IDF, "w", encoding="latin-1") as f:
        f.write(text)

    return OUTPUT_IDF


if __name__ == "__main__":
    path = build_baseline_idf()
    print(f"Wrote {path}")
    print("Changes applied: RunPeriod -> Jul 15-21, Fanger comfort on 5 People objects, "
          "3 supporting schedules, hourly Output:Variable/Meter block.")
