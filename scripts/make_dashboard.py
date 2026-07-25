"""Thin CLI wrapper so `python scripts/make_dashboard.py` (the command README.md
documents) works -- the actual implementation is src/analysis/dashboard.py;
see that module for the chart/HTML logic and its own --demo self-check."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.analysis.dashboard import main  # noqa: E402

if __name__ == "__main__":
    main()
