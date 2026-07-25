"""Post-run metrics for one EnergyPlusRunner output directory (results/raw/*/):
total/fixed/HVAC kWh, gas, comfort-in-band %, kg CO2, cost, peak demand -- and
compare() for baseline-vs-agent % savings.

Energy comes from EnergyPlus's own eplusmtr.csv, not state.csv's python-
accumulated columns -- eliminates any doubt about the runner's own
accumulation (see runner.py's Phase 4 "CORRECTION" docstring note for the bug
that made this the safer source of truth).

comfort_in_band_pct is the single source of truth for that metric -- it used
to be duplicated inline in scripts/run_agent.py; that script now imports it
from here instead.
"""
import csv
import json
import os

from src.tools.building_tools import ZONES, load_carbon_profile

PMV_BAND = (-0.5, 0.5)


def _read_state_csv(run_dir: str) -> list[dict]:
    with open(os.path.join(run_dir, "state.csv")) as f:
        return list(csv.DictReader(f))


def _read_meter_kwh_by_hour(run_dir: str) -> list[dict]:
    """One dict per hourly row of eplusmtr.csv: {building_kwh, hvac_kwh, gas_kwh}.
    hvac_kwh is HVAC + Plant, matching runner.py's HVAC_SUBMETERS split."""
    path = os.path.join(run_dir, "eplusmtr.csv")
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        i_building = header.index("Electricity:Building [J](Hourly)")
        i_hvac = header.index("Electricity:HVAC [J](Hourly)")
        i_plant = header.index("Electricity:Plant [J](Hourly)")
        i_gas = header.index("NaturalGas:Facility [J](Hourly)")
        return [
            {
                "building_kwh": float(r[i_building]) / 3.6e6,
                "hvac_kwh": (float(r[i_hvac]) + float(r[i_plant])) / 3.6e6,
                "gas_kwh": float(r[i_gas]) / 3.6e6,
            }
            for r in reader
        ]


def comfort_in_band_pct(rows: list[dict]) -> float:
    """% of (occupied hour, zone) pairs with PMV in [-0.5, 0.5]. 0.0 if no
    occupied hours (e.g. a short scratch run that never reaches occupancy)."""
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


def summarize(run_dir: str) -> dict:
    """Headline numbers for one run directory (baseline or agent)."""
    state_rows = _read_state_csv(run_dir)
    meter_rows = _read_meter_kwh_by_hour(run_dir)
    if len(state_rows) != len(meter_rows):
        raise ValueError(
            f"{run_dir}: state.csv has {len(state_rows)} rows but eplusmtr.csv has "
            f"{len(meter_rows)} -- mismatched run outputs, can't align hour-by-hour"
        )

    carbon_profile = {row["hour"]: row for row in load_carbon_profile()}

    fixed_kwh = sum(r["building_kwh"] for r in meter_rows)
    hvac_kwh = sum(r["hvac_kwh"] for r in meter_rows)
    gas_kwh = sum(r["gas_kwh"] for r in meter_rows)
    total_kwh = fixed_kwh + hvac_kwh
    peak_kw = max((r["building_kwh"] + r["hvac_kwh"] for r in meter_rows), default=0.0)

    kg_co2 = 0.0
    cost = 0.0
    for state_row, meter_row in zip(state_rows, meter_rows):
        carbon_row = carbon_profile.get(int(state_row["hour"]))
        if carbon_row is None:
            continue  # carbon_intensity.csv is malformed/incomplete -- skip, don't crash
        hourly_kwh = meter_row["building_kwh"] + meter_row["hvac_kwh"]
        kg_co2 += hourly_kwh * carbon_row["carbon_gco2_per_kwh"] / 1000.0
        cost += hourly_kwh * carbon_row["tariff_per_kwh"]

    return {
        "run_dir": run_dir,
        "hours": len(state_rows),
        "total_electricity_kwh": round(total_kwh, 1),
        "fixed_load_kwh": round(fixed_kwh, 1),
        "hvac_kwh": round(hvac_kwh, 1),
        "gas_kwh": round(gas_kwh, 1),
        "comfort_in_band_pct": round(comfort_in_band_pct(state_rows), 1),
        "kg_co2": round(kg_co2, 1),
        "cost": round(cost, 2),
        "peak_demand_kw": round(peak_kw, 1),
    }


def compare(baseline_dir: str, agent_dir: str) -> dict:
    """% savings of agent vs baseline: total-facility (the spec's literal ask)
    and HVAC-only (the honest measure of what setpoints/fan control can
    actually move -- ~62% of total facility electricity is lighting/plug load,
    see docs/ARCHITECTURE.md)."""
    baseline = summarize(baseline_dir)
    agent = summarize(agent_dir)
    if baseline["hours"] != agent["hours"]:
        raise ValueError(
            f"horizon mismatch: baseline has {baseline['hours']} hours, agent run has "
            f"{agent['hours']} -- re-run with matching --days before comparing"
        )

    def pct_saved(key: str) -> float:
        b, a = baseline[key], agent[key]
        return round(100.0 * (b - a) / b, 1) if b else 0.0

    return {
        "baseline": baseline,
        "agent": agent,
        "total_electricity_pct_saved": pct_saved("total_electricity_kwh"),
        "hvac_pct_saved": pct_saved("hvac_kwh"),
        "comfort_delta_pts": round(agent["comfort_in_band_pct"] - baseline["comfort_in_band_pct"], 1),
        "kg_co2_avoided": round(baseline["kg_co2"] - agent["kg_co2"], 1),
        "cost_saved": round(baseline["cost"] - agent["cost"], 2),
    }


def summarize_decision_log(path: str, days: int) -> dict | None:
    """Tail of the LLM decision log matching this run's horizon: fallback rate,
    retry rate, latency percentiles. None if the log doesn't exist or has no
    entries for this horizon (e.g. a fallback/mcp-controller run that never
    wrote to it, or a fresh log). Moved here from scripts/run_agent.py so it
    isn't duplicated -- that script now just prints this dict."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    entries = entries[-days * 24:]  # only this run's tail, if the log predates it
    if not entries:
        return None

    latencies = sorted(e["latency_ms"] for e in entries if e.get("latency_ms") is not None)
    return {
        "decisions": len(entries),
        "fallback_count": sum(1 for e in entries if e.get("fallback_used")),
        "retried_count": sum(1 for e in entries if e.get("retried")),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": latencies[int(len(latencies) * 0.95)] if latencies else None,
    }


def demo() -> None:
    """Runnable self-check with a small synthetic 2-hour fixture -- no E+ run
    needed. Writes throwaway state.csv/eplusmtr.csv into a temp dir, matching
    the real column layout, and checks summarize()/compare() against
    hand-computed expected values."""
    import shutil
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="metrics_demo_")
    try:
        base_dir = os.path.join(tmp_dir, "baseline")
        agent_dir = os.path.join(tmp_dir, "agent")
        os.makedirs(base_dir)
        os.makedirs(agent_dir)

        def write_run(run_dir: str, hvac_kwh_per_hour: list[float], pmvs: list[float]) -> None:
            # 2 hours, hour 8 (occupied) and hour 20 (unoccupied) -- keeps the
            # carbon-profile join and the occupied-only comfort filter both
            # exercised in one tiny fixture.
            hours = [8, 20]
            occ = [1.0, 0.0]
            state_rows = [
                {
                    "day_of_year": 200, "hour": h, "occupancy_frac": o,
                    **{f"{z}_pmv": pmv for z in ZONES},
                }
                for h, o, pmv in zip(hours, occ, pmvs)
            ]
            with open(os.path.join(run_dir, "state.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(state_rows[0].keys()))
                w.writeheader()
                w.writerows(state_rows)

            building_kwh = [10.0, 2.0]  # fixed load, identical baseline vs agent
            with open(os.path.join(run_dir, "eplusmtr.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "Date/Time",
                    "Electricity:Building [J](Hourly)",
                    "Electricity:HVAC [J](Hourly)",
                    "Electricity:Plant [J](Hourly)",
                    "NaturalGas:Facility [J](Hourly)",
                ])
                for bkwh, hkwh in zip(building_kwh, hvac_kwh_per_hour):
                    w.writerow([" ", bkwh * 3.6e6, hkwh * 3.6e6, 0.0, 0.0])

        write_run(base_dir, hvac_kwh_per_hour=[20.0, 5.0], pmvs=[-0.3, -1.0])
        write_run(agent_dir, hvac_kwh_per_hour=[15.0, 2.0], pmvs=[0.1, -0.2])

        base_summary = summarize(base_dir)
        assert base_summary["hours"] == 2
        assert base_summary["fixed_load_kwh"] == 12.0  # 10 + 2
        assert base_summary["hvac_kwh"] == 25.0  # 20 + 5
        assert base_summary["total_electricity_kwh"] == 37.0
        # Only hour 8 is occupied -> comfort measured over that hour's 5 zones only.
        # PMV -0.3 is in [-0.5, 0.5] -> 100% in-band.
        assert base_summary["comfort_in_band_pct"] == 100.0

        agent_summary = summarize(agent_dir)
        assert agent_summary["hvac_kwh"] == 17.0  # 15 + 2
        # PMV 0.1 at hour 8 -> still in-band.
        assert agent_summary["comfort_in_band_pct"] == 100.0

        result = compare(base_dir, agent_dir)
        # HVAC: 25.0 -> 17.0 is a 32.0% cut; total: 37.0 -> 29.0 is a 21.6% cut.
        assert result["hvac_pct_saved"] == 32.0, result
        assert result["total_electricity_pct_saved"] == 21.6, result
        assert result["comfort_delta_pts"] == 0.0  # both hit 100% in this fixture
        assert result["kg_co2_avoided"] > 0  # less kWh at a nonzero carbon intensity -> avoided > 0

        log_path = os.path.join(tmp_dir, "decision_log.jsonl")
        with open(log_path, "w") as f:
            f.write(json.dumps({"fallback_used": False, "retried": False, "latency_ms": 100.0}) + "\n")
            f.write(json.dumps({"fallback_used": True, "retried": True, "latency_ms": None}) + "\n")
        log_summary = summarize_decision_log(log_path, days=1)
        assert log_summary == {
            "decisions": 2, "fallback_count": 1, "retried_count": 1,
            "latency_p50_ms": 100.0, "latency_p95_ms": 100.0,
        }, log_summary
        assert summarize_decision_log(os.path.join(tmp_dir, "missing.jsonl"), days=1) is None

        print("metrics.py: all assertions passed.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    demo()
