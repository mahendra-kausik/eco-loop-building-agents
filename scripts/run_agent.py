"""Agent run ("Run B"): same horizon and IDF as run_baseline.py, with a
selectable controller:

  --controller fallback   Phase 2's deterministic rule (src/agent/fallback.py)
  --controller llm        Phase 3's LLM + safety supervisor (src/agent/safety.py),
                           the default -- logs every decision to
                           results/decision_log.jsonl
  --controller mcp        polls results/pending_setpoints.json (written by an
                           external MCP client via src/mcp_server/server.py's
                           inject_setpoints tool) once per sim hour; falls back
                           to the rule-based controller if no file is present yet

Prints the headline comparison against results/raw/baseline/state.csv: total
kWh, % saved, occupied-hours PMV-in-band %, injection count, controller errors,
and (llm mode) a decision-log summary.

Run: python scripts/run_baseline.py   (first, to produce the comparison point)
     python scripts/run_agent.py [--controller {fallback,llm,mcp}] [--days N]
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent.fallback import fallback_controller  # noqa: E402
from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner, ZONES  # noqa: E402

BASELINE_CSV = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "baseline", "state.csv")
PENDING_SETPOINTS_PATH = os.path.join(os.path.dirname(__file__), "..", "results", "pending_setpoints.json")
DECISION_LOG = os.path.join(os.path.dirname(__file__), "..", "results", "decision_log.jsonl")

PMV_BAND = (-0.5, 0.5)


def comfort_in_band_pct(rows: list[dict]) -> float:
    occupied = [r for r in rows if float(r["occupancy_frac"]) > 0]
    if not occupied:
        return 0.0
    total = len(occupied) * len(ZONES)
    in_band = sum(
        1
        for r in occupied
        for z in ZONES
        if PMV_BAND[0] <= float(r[f"{z}_pmv"]) <= PMV_BAND[1]
    )
    return 100.0 * in_band / total


def _make_mcp_controller():
    """Polls pending_setpoints.json once per sim hour; falls back to the
    deterministic controller if the file is missing or stale (no fresh write
    since the last poll) -- an external MCP client not calling inject_setpoints
    that hour must never stall the simulation."""
    last_mtime = None

    def controller(row, day_of_year, hour, day_of_week):
        nonlocal last_mtime
        if os.path.exists(PENDING_SETPOINTS_PATH):
            mtime = os.path.getmtime(PENDING_SETPOINTS_PATH)
            if mtime != last_mtime:
                last_mtime = mtime
                with open(PENDING_SETPOINTS_PATH) as f:
                    pending = json.load(f)
                return pending["heating_c"], pending["cooling_c"]
        return fallback_controller(row, day_of_year, hour, day_of_week)

    return controller


def _summarize_decision_log(path: str, days: int) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    entries = entries[-days * 24:]  # only this run's tail, if the log predates it
    if not entries:
        return
    fallback_count = sum(1 for e in entries if e.get("fallback_used"))
    retried_count = sum(1 for e in entries if e.get("retried"))
    latencies = sorted(e["latency_ms"] for e in entries if e.get("latency_ms") is not None)
    p50 = latencies[len(latencies) // 2] if latencies else None
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else None
    print(
        f"Decision log: {len(entries)} decisions, {fallback_count} fallback-used, "
        f"{retried_count} retried"
        + (f", latency p50={p50:.0f}ms p95={p95:.0f}ms" if latencies else ", no LLM latency recorded")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--controller", choices=["fallback", "llm", "mcp"], default="llm")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join(
        os.path.dirname(__file__), "..", "results", "raw", f"agent_{args.controller}"
    )

    if args.controller == "fallback":
        controller = fallback_controller
    elif args.controller == "llm":
        from src.agent.safety import make_llm_controller

        controller = make_llm_controller(log_path=DECISION_LOG, run_dir=output_dir)
    else:
        controller = _make_mcp_controller()

    # build_baseline_idf(days=7, out_path=None) overwrites models/baseline.idf --
    # that's the deliverable, so only the canonical 7-day case is allowed to
    # touch it. Any other --days writes to a scratch copy (smoke_test.py's
    # pattern) instead.
    if args.days == 7:
        idf_path = build_baseline_idf(days=7)
    else:
        scratch_idf = os.path.join(output_dir, "run.idf")
        idf_path = build_baseline_idf(days=args.days, out_path=scratch_idf)
    epw_path = os.path.join(
        ENERGYPLUS_DIR, "WeatherData", "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    )
    runner = EnergyPlusRunner(idf_path, epw_path, output_dir, controller=controller)
    exit_code = runner.run()

    expected_rows = args.days * 24
    print(f"\nEnergyPlus exit code: {exit_code}")
    print(f"Rows collected: {len(runner.rows)}")

    assert len(runner.rows) == expected_rows, f"expected {expected_rows} hourly rows, got {len(runner.rows)}"
    assert runner.controller_errors == [], f"controller raised errors: {runner.controller_errors}"
    assert runner.injections == expected_rows, f"expected {expected_rows} injections, got {runner.injections}"

    agent_kwh = runner.rows[-1]["cumulative_electricity_kwh"]
    agent_comfort = comfort_in_band_pct(runner.rows)
    print(f"\nAgent run ({args.controller}): {agent_kwh:.1f} kWh electricity, "
          f"{agent_comfort:.1f}% occupied PMV in-band, "
          f"{runner.injections} injections, {len(runner.controller_errors)} controller errors.")

    if args.controller == "llm":
        _summarize_decision_log(DECISION_LOG, args.days)

    if not os.path.exists(BASELINE_CSV):
        print(f"No baseline found at {BASELINE_CSV} -- run scripts/run_baseline.py first for a comparison.")
    else:
        with open(BASELINE_CSV) as f:
            baseline_rows = list(csv.DictReader(f))
        if len(baseline_rows) != expected_rows:
            print(f"Baseline has {len(baseline_rows)} rows but this run has {expected_rows} -- "
                  f"different horizons, skipping the savings comparison (re-run with matching --days).")
        else:
            baseline_kwh = float(baseline_rows[-1]["cumulative_electricity_kwh"])
            baseline_comfort = comfort_in_band_pct(baseline_rows)
            pct_saved = 100.0 * (baseline_kwh - agent_kwh) / baseline_kwh
            print(f"Baseline run: {baseline_kwh:.1f} kWh electricity, {baseline_comfort:.1f}% occupied PMV in-band.")
            print(f"Savings: {pct_saved:+.1f}% kWh vs baseline; comfort delta: {agent_comfort - baseline_comfort:+.1f} pts.")


if __name__ == "__main__":
    main()
