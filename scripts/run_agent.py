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

Prints the headline comparison against results/raw/baseline/ (via
src/analysis/metrics.py): total + HVAC-only kWh, % saved, occupied-hours
PMV-in-band %, kg CO2, cost, injection count, controller errors, and (llm
mode) a decision-log summary.

Run: python scripts/run_baseline.py   (first, to produce the comparison point)
     python scripts/run_agent.py [--controller {fallback,llm,mcp}] [--days N]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent.fallback import fallback_controller  # noqa: E402
from src.analysis.metrics import compare, summarize, summarize_decision_log  # noqa: E402
from src.simulation.eplus_path import ENERGYPLUS_DIR  # noqa: E402
from src.simulation.idf_prep import build_baseline_idf  # noqa: E402
from src.simulation.runner import EnergyPlusRunner  # noqa: E402

BASELINE_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "raw", "baseline")
PENDING_SETPOINTS_PATH = os.path.join(os.path.dirname(__file__), "..", "results", "pending_setpoints.json")
DECISION_LOG = os.path.join(os.path.dirname(__file__), "..", "results", "decision_log.jsonl")


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
                # Fan control isn't exposed via the MCP inject_setpoints tool
                # (Phase 4 scope: fan actuation is core-loop-only) -- always on.
                return pending["heating_c"], pending["cooling_c"], 1.0
        return fallback_controller(row, day_of_year, hour, day_of_week)

    return controller


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--controller", choices=["fallback", "llm", "mcp"], default="llm")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--decision-log", default=DECISION_LOG)
    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join(
        os.path.dirname(__file__), "..", "results", "raw", f"agent_{args.controller}"
    )

    if args.controller == "fallback":
        controller = fallback_controller
    elif args.controller == "llm":
        from src.agent.safety import make_llm_controller

        controller = make_llm_controller(log_path=args.decision_log, run_dir=output_dir)
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

    # Deliverable 2 also asks for "the modified versions generated during
    # runtime evaluation" alongside the baseline. Only the canonical 7-day case
    # writes into models/ (not gitignored, unlike results/raw/) -- see
    # src/simulation/idf_prep.py:export_runtime_idf for why this bakes the
    # ACTUAL applied setpoint/fan values rather than being a duplicate of
    # baseline.idf.
    if args.days == 7:
        from src.simulation.idf_prep import export_runtime_idf

        runtime_idf_path = os.path.join(
            os.path.dirname(__file__), "..", "models", f"agent_{args.controller}_runtime.idf"
        )
        export_runtime_idf(runner.rows, idf_path, runtime_idf_path)
        print(f"Wrote runtime-modified IDF -> {runtime_idf_path}")

    # summarize() reads eplusmtr.csv (EnergyPlus's own meter output) + state.csv,
    # both already written into output_dir by runner.run() -- see
    # src/analysis/metrics.py's module docstring for why the meter file, not our
    # own cumulative_electricity_kwh column, is the source of truth for kWh.
    agent_summary = summarize(output_dir)
    print(
        f"\nAgent run ({args.controller}): {agent_summary['total_electricity_kwh']:.1f} kWh total "
        f"({agent_summary['hvac_kwh']:.1f} kWh HVAC, {agent_summary['fixed_load_kwh']:.1f} kWh fixed load), "
        f"{agent_summary['gas_kwh']:.1f} kWh gas, {agent_summary['comfort_in_band_pct']:.1f}% occupied PMV in-band, "
        f"{agent_summary['kg_co2']:.1f} kg CO2, cost {agent_summary['cost']:.2f}, "
        f"{runner.injections} injections, {len(runner.controller_errors)} controller errors."
    )

    if args.controller == "llm":
        log_summary = summarize_decision_log(args.decision_log, args.days)
        if log_summary:
            latency = (
                f", latency p50={log_summary['latency_p50_ms']:.0f}ms p95={log_summary['latency_p95_ms']:.0f}ms"
                if log_summary["latency_p50_ms"] is not None
                else ", no LLM latency recorded"
            )
            print(
                f"Decision log: {log_summary['decisions']} decisions, "
                f"{log_summary['fallback_count']} fallback-used, {log_summary['retried_count']} retried{latency}"
            )

    if not os.path.exists(os.path.join(BASELINE_DIR, "state.csv")):
        print(f"No baseline found at {BASELINE_DIR} -- run scripts/run_baseline.py first for a comparison.")
    else:
        try:
            result = compare(BASELINE_DIR, output_dir)
        except ValueError as exc:
            print(f"Skipping the savings comparison: {exc}")
        else:
            b = result["baseline"]
            print(
                f"Baseline run: {b['total_electricity_kwh']:.1f} kWh total ({b['hvac_kwh']:.1f} kWh HVAC), "
                f"{b['comfort_in_band_pct']:.1f}% occupied PMV in-band, {b['kg_co2']:.1f} kg CO2."
            )
            print(
                f"Savings: {result['total_electricity_pct_saved']:+.1f}% total kWh "
                f"({result['hvac_pct_saved']:+.1f}% HVAC kWh) vs baseline; "
                f"comfort delta: {result['comfort_delta_pts']:+.1f} pts; "
                f"CO2 avoided: {result['kg_co2_avoided']:+.1f} kg; cost saved: {result['cost_saved']:+.2f}."
            )


if __name__ == "__main__":
    main()
