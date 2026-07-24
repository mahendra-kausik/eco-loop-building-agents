"""The one place that makes `pyenergyplus` importable.

pyenergyplus ships inside the EnergyPlus install dir, not on PyPI, so it can't live in
requirements.txt / .venv. Every other module reaches it by importing from here --
never repeat the sys.path.append elsewhere.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

_ENERGYPLUS_DIR = os.environ.get("ENERGYPLUS_DIR", "").strip()

if not _ENERGYPLUS_DIR:
    raise RuntimeError(
        "ENERGYPLUS_DIR is not set in .env. It must point at the EnergyPlus install "
        "directory (the one containing the pyenergyplus/ folder), e.g. C:\\EnergyPlusV26-1-0"
    )

if not os.path.isdir(_ENERGYPLUS_DIR):
    raise RuntimeError(f"ENERGYPLUS_DIR does not exist: {_ENERGYPLUS_DIR}")

_pyenergyplus_dir = os.path.join(_ENERGYPLUS_DIR, "pyenergyplus")
if not os.path.isdir(_pyenergyplus_dir):
    raise RuntimeError(
        f"ENERGYPLUS_DIR is set to {_ENERGYPLUS_DIR!r} but it has no pyenergyplus/ "
        "subfolder -- is this really an EnergyPlus install dir?"
    )

if _ENERGYPLUS_DIR not in sys.path:
    sys.path.append(_ENERGYPLUS_DIR)

from pyenergyplus.api import EnergyPlusAPI  # noqa: E402  (must follow sys.path.append)

ENERGYPLUS_DIR = _ENERGYPLUS_DIR

__all__ = ["EnergyPlusAPI", "ENERGYPLUS_DIR"]


if __name__ == "__main__":
    api = EnergyPlusAPI()
    print(f"ENERGYPLUS_DIR = {ENERGYPLUS_DIR}")
    print(f"EnergyPlusAPI import OK: {api}")
