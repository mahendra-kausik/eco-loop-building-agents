"""Phase 5: the quantitative savings dashboard (deliverable 3).

Renders ONE self-contained Plotly HTML file -- plotly.js is inlined, no CDN, no
external assets -- so a judge can open it offline from a fresh clone. That's also
why every number here comes from src/analysis/metrics.py rather than being
recomputed: metrics reads EnergyPlus's own eplusmtr.csv, which is the whole point
of the Phase 4 metering correction (see docs/ARCHITECTURE.md). A dashboard that
recomputed kWh its own way could disagree with the headline numbers in the README,
and then neither would be trustworthy.

Run: python -m src.analysis.dashboard
     python -m src.analysis.dashboard --baseline results/raw/baseline \
         --agent results/raw/agent_llm --out results/dashboard.html
"""
import argparse
import os

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.analysis.metrics import (
    PMV_BAND,
    _read_meter_kwh_by_hour,
    _read_state_csv,
    compare,
    summarize,
    summarize_decision_log,
)
from src.tools.building_tools import ZONES, load_carbon_profile

# Plot palette: baseline is the thing we're beating, agent is the result. Kept
# colourblind-safe (orange/blue rather than red/green).
BASELINE_COLOR = "#E8833A"
AGENT_COLOR = "#2E86C1"
BAND_COLOR = "rgba(46, 134, 193, 0.12)"
OCCUPIED_COLOR = "rgba(120, 120, 120, 0.10)"


def _worst_pmv_series(rows: list[dict]) -> list[float]:
    """Per hour, the zone PMV furthest from neutral -- the same worst-case view
    get_building_state gives the LLM, so the chart shows what the agent saw."""
    return [max((float(r[f"{z}_pmv"]) for z in ZONES), key=abs) for r in rows]


def _occupied_spans(rows: list[dict]) -> list[tuple[int, int]]:
    """Contiguous [start, end) index ranges where the building is occupied, for
    shading. Built from occupancy_frac so it always matches the IDF's OCCUPY-1
    schedule rather than re-deriving calendar logic."""
    spans, start = [], None
    for i, r in enumerate(rows):
        occupied = float(r["occupancy_frac"]) > 0
        if occupied and start is None:
            start = i
        elif not occupied and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(rows)))
    return spans


def _cards_html(result: dict) -> str:
    b, a = result["baseline"], result["agent"]
    cards = [
        ("Total electricity saved", f"{result['total_electricity_pct_saved']:+.1f}%",
         f"{b['total_electricity_kwh']:.0f} &rarr; {a['total_electricity_kwh']:.0f} kWh"),
        ("HVAC electricity saved", f"{result['hvac_pct_saved']:+.1f}%",
         f"{b['hvac_kwh']:.0f} &rarr; {a['hvac_kwh']:.0f} kWh"),
        ("Comfort in band", f"{a['comfort_in_band_pct']:.1f}%",
         f"{result['comfort_delta_pts']:+.1f} pts vs baseline&rsquo;s {b['comfort_in_band_pct']:.1f}%"),
        ("CO&#8322; avoided", f"{result['kg_co2_avoided']:.0f} kg",
         f"{b['kg_co2']:.0f} &rarr; {a['kg_co2']:.0f} kg"),
        ("Cost saved", f"&#8377;{result['cost_saved']:,.2f}",
         f"at &#8377;6/kWh tariff, over {a['hours']} simulated hours"),
        ("Peak demand", f"{a['peak_demand_kw']:.1f} kW",
         f"baseline {b['peak_demand_kw']:.1f} kW"),
    ]
    return "".join(
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="sub">{sub}</div></div>'
        for label, value, sub in cards
    )


def _load_curve_fig(baseline_meters: list[dict], agent_meters: list[dict]) -> go.Figure:
    fig = go.Figure()
    for name, meters, color in (
        ("Baseline", baseline_meters, BASELINE_COLOR),
        ("Agent", agent_meters, AGENT_COLOR),
    ):
        fig.add_trace(go.Scatter(
            y=[m["hvac_kwh"] for m in meters], name=name,
            line=dict(color=color, width=1.5), hovertemplate="hour %{x}: %{y:.2f} kWh<extra></extra>",
        ))
    fig.update_layout(
        title="HVAC electricity, hour by hour (EnergyPlus eplusmtr.csv)",
        xaxis_title="Simulated hour", yaxis_title="kWh",
    )
    return fig


def _comfort_fig(baseline_rows: list[dict], agent_rows: list[dict]) -> go.Figure:
    fig = go.Figure()
    lo, hi = PMV_BAND
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND_COLOR, line_width=0,
                  annotation_text="comfort band", annotation_position="top left")
    for start, end in _occupied_spans(agent_rows):
        fig.add_vrect(x0=start, x1=end, fillcolor=OCCUPIED_COLOR, line_width=0)
    for name, rows, color in (
        ("Baseline", baseline_rows, BASELINE_COLOR),
        ("Agent", agent_rows, AGENT_COLOR),
    ):
        fig.add_trace(go.Scatter(
            y=_worst_pmv_series(rows), name=name,
            line=dict(color=color, width=1.5), hovertemplate="hour %{x}: PMV %{y:+.2f}<extra></extra>",
        ))
    fig.update_layout(
        title="Worst-zone PMV vs the comfort band (shaded columns = occupied hours)",
        xaxis_title="Simulated hour", yaxis_title="PMV",
    )
    return fig


def _carbon_fig(agent_rows: list[dict], agent_meters: list[dict]) -> go.Figure:
    """The carbon-aware differentiator: grid intensity by hour of day against
    when the agent actually spends its HVAC energy."""
    carbon = {row["hour"]: row["carbon_gco2_per_kwh"] for row in load_carbon_profile()}
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    for row, meter in zip(agent_rows, agent_meters):
        by_hour[int(row["hour"]) % 24].append(meter["hvac_kwh"])
    mean_hvac = [sum(v) / len(v) if v else 0.0 for h, v in sorted(by_hour.items())]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        x=list(range(24)), y=[carbon.get(h, 0.0) for h in range(24)],
        name="Grid carbon intensity", marker_color="rgba(150,150,150,0.45)",
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=list(range(24)), y=mean_hvac, name="Agent mean HVAC kWh",
        line=dict(color=AGENT_COLOR, width=2.5),
    ), secondary_y=True)
    fig.update_layout(title="Carbon-aware load shifting: when the agent spends energy",
                      xaxis_title="Hour of day")
    fig.update_yaxes(title_text="gCO&#8322;/kWh", secondary_y=False)
    fig.update_yaxes(title_text="mean HVAC kWh", secondary_y=True)
    return fig


def _decision_fig(log_path: str, days: int) -> go.Figure | None:
    """Reliability panel: which hours used the LLM vs fell back, and latency.
    None when there's no log (a fallback- or mcp-controller run)."""
    import json

    if not os.path.exists(log_path):
        return None
    with open(log_path) as f:
        entries = [json.loads(line) for line in f if line.strip()][-days * 24:]
    if not entries:
        return None

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        y=[1 if e.get("fallback_used") else 0 for e in entries], name="Fallback used",
        marker_color=BASELINE_COLOR,
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        y=[e.get("latency_ms") for e in entries], name="LLM latency",
        line=dict(color=AGENT_COLOR, width=1.2), connectgaps=False,
    ), secondary_y=True)
    fig.update_layout(title="Decision reliability: fallback usage and LLM latency per hour",
                      xaxis_title="Decision (simulated hour)")
    fig.update_yaxes(title_text="fallback", secondary_y=False, range=[0, 1], dtick=1)
    fig.update_yaxes(title_text="ms", secondary_y=True)
    return fig


def _reliability_strip_html(soak_dir: str, soak_log: str) -> str:
    """14-day LLM endurance run has no matching-horizon baseline to compare()
    against (compare() hard-raises on horizon mismatch by design -- see
    metrics.py), so it can't be more comparison cards. It's evidence of a
    different claim (System Integration: survives an extended horizon without
    crashing), rendered as its own strip rather than mixed into the 6 savings
    cards above. Returns "" if the soak run's outputs aren't present, so this
    stays fully optional and demo() doesn't need to fake it."""
    if not (os.path.isdir(soak_dir) and os.path.exists(soak_log)):
        return ""
    try:
        soak = summarize(soak_dir)
    except (FileNotFoundError, ValueError):
        return ""
    days = soak["hours"] // 24
    log = summarize_decision_log(soak_log, days)
    if log is None:
        return ""
    return (
        f'<div class="reliability">'
        f"<strong>{days}-day LLM endurance run</strong> &mdash; "
        f"{log['decisions']}/{log['decisions']} decisions, "
        f"{log['fallback_count']} fallback, "
        f"{soak['comfort_in_band_pct']:.1f}% comfort in-band, "
        f"latency p50 {log['latency_p50_ms']:.0f} ms / p95 {log['latency_p95_ms']:.0f} ms"
        f"</div>"
    )


def build_dashboard(baseline_dir: str, agent_dir: str, out_path: str, decision_log: str,
                     soak_dir: str = "", soak_log: str = "") -> str:
    result = compare(baseline_dir, agent_dir)
    baseline_rows, agent_rows = _read_state_csv(baseline_dir), _read_state_csv(agent_dir)
    baseline_meters, agent_meters = _read_meter_kwh_by_hour(baseline_dir), _read_meter_kwh_by_hour(agent_dir)
    days = max(1, result["agent"]["hours"] // 24)

    figures = [
        _load_curve_fig(baseline_meters, agent_meters),
        _comfort_fig(baseline_rows, agent_rows),
        _carbon_fig(agent_rows, agent_meters),
    ]
    decision_fig = _decision_fig(decision_log, days)
    if decision_fig is not None:
        figures.append(decision_fig)

    for fig in figures:
        fig.update_layout(template="plotly_white", height=380,
                          margin=dict(l=60, r=40, t=60, b=50), hovermode="x unified")

    # plotly.js inlined into the FIRST figure only -- repeating it per figure
    # would multiply a ~3 MB payload by four and threaten the 10 MB cap.
    blocks = [
        fig.to_html(full_html=False, include_plotlyjs="inline" if i == 0 else False)
        for i, fig in enumerate(figures)
    ]

    log_summary = summarize_decision_log(decision_log, days)
    footer = (
        f"{log_summary['decisions']} decisions &middot; "
        f"{log_summary['fallback_count']} fallback &middot; "
        f"{log_summary['retried_count']} retried &middot; "
        f"latency p50 {log_summary['latency_p50_ms']:.0f} ms / p95 {log_summary['latency_p95_ms']:.0f} ms"
        if log_summary and log_summary["latency_p50_ms"] is not None
        else "No LLM decision log for this run (deterministic controller)."
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Eco-Loop Building Agents &mdash; savings dashboard</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
         background: #F4F6F8; color: #1B2631; }}
  header {{ background: #1B2631; color: #fff; padding: 28px 40px; }}
  header h1 {{ margin: 0 0 6px; font-size: 24px; }}
  header p {{ margin: 0; opacity: .75; font-size: 14px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 16px; padding: 24px 40px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 22px; min-width: 190px;
           flex: 1; box-shadow: 0 1px 3px rgba(0,0,0,.10); }}
  .card .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: .5px; opacity: .6; }}
  .card .value {{ font-size: 30px; font-weight: 650; margin: 6px 0 2px; color: {AGENT_COLOR}; }}
  .card .sub {{ font-size: 12px; opacity: .65; }}
  .chart {{ background: #fff; border-radius: 10px; margin: 0 40px 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,.10); overflow: hidden; }}
  .reliability {{ margin: 0 40px 20px; padding: 12px 22px; font-size: 13px;
                   background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.10); }}
  footer {{ padding: 18px 40px 40px; font-size: 12px; opacity: .6; }}
</style></head><body>
<header>
  <h1>Eco-Loop Building Agents &mdash; quantitative savings</h1>
  <p>EnergyPlus closed loop, {result['agent']['hours']} simulated hours &middot;
     baseline <code>{os.path.basename(baseline_dir)}</code> vs agent
     <code>{os.path.basename(agent_dir)}</code> &middot; energy from EnergyPlus
     <code>eplusmtr.csv</code></p>
</header>
<div class="cards">{_cards_html(result)}</div>
{"".join(f'<div class="chart">{b}</div>' for b in blocks)}
{_reliability_strip_html(soak_dir, soak_log)}
<footer>{footer}</footer>
</body></html>"""

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote dashboard -> {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")
    return out_path


def demo() -> None:
    """Self-check on a synthetic 48-hour fixture -- no EnergyPlus run needed,
    same pattern as metrics.demo(). Catches a broken chart/HTML path before a
    real run is spent on it."""
    import csv
    import json
    import shutil
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="dashboard_demo_")
    try:
        def write_run(run_dir: str, hvac: float, pmv: float) -> None:
            os.makedirs(run_dir)
            rows = [
                {
                    "day_of_year": 200 + h // 24, "hour": h % 24,
                    "occupancy_frac": 1.0 if 8 <= h % 24 < 19 else 0.0,
                    **{f"{z}_pmv": pmv for z in ZONES},
                }
                for h in range(48)
            ]
            with open(os.path.join(run_dir, "state.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            with open(os.path.join(run_dir, "eplusmtr.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Date/Time", "Electricity:Building [J](Hourly)",
                            "Electricity:HVAC [J](Hourly)", "Electricity:Plant [J](Hourly)",
                            "NaturalGas:Facility [J](Hourly)"])
                for _ in range(48):
                    w.writerow([" ", 10.0 * 3.6e6, hvac * 3.6e6, 0.0, 0.0])

        baseline_dir = os.path.join(tmp_dir, "baseline")
        agent_dir = os.path.join(tmp_dir, "agent")
        write_run(baseline_dir, hvac=20.0, pmv=0.8)   # baseline: more energy, out of band
        write_run(agent_dir, hvac=15.0, pmv=0.2)      # agent: less energy, in band

        log_path = os.path.join(tmp_dir, "decision_log.jsonl")
        with open(log_path, "w") as f:
            for i in range(48):
                f.write(json.dumps({
                    "fallback_used": i % 10 == 0, "retried": False, "latency_ms": 900.0,
                }) + "\n")

        out_path = os.path.join(tmp_dir, "dashboard.html")
        build_dashboard(baseline_dir, agent_dir, out_path, log_path)

        html = open(out_path, encoding="utf-8").read()
        assert "+25.0%" in html, "expected the 20->15 kWh HVAC saving in the headline cards"
        # plotly.js (~4.5 MB) must be inlined exactly once -- present (self-
        # contained, renders offline) but not once per chart (include_plotlyjs
        # left True on charts 2+ would ~4x the file and risk the 10 MB cap).
        size_mb = os.path.getsize(out_path) / 1e6
        assert 4.0 < size_mb < 8.0, f"expected ~4-8 MB (plotly.js inlined once), got {size_mb:.1f} MB"
        print("dashboard.py: all assertions passed.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    repo = os.path.join(os.path.dirname(__file__), "..", "..")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", default=os.path.join(repo, "results", "raw", "baseline"))
    parser.add_argument("--agent", default=os.path.join(repo, "results", "raw", "agent_llm"))
    parser.add_argument("--out", default=os.path.join(repo, "results", "dashboard.html"))
    parser.add_argument("--decision-log", default=os.path.join(repo, "results", "decision_log.jsonl"))
    parser.add_argument("--soak", default=os.path.join(repo, "results", "raw", "soak_llm"),
                         help="optional 14-day LLM endurance run dir; omitted from the dashboard if absent")
    parser.add_argument("--soak-log", default=os.path.join(repo, "results", "decision_log_soak14.jsonl"))
    parser.add_argument("--demo", action="store_true", help="run the synthetic self-check and exit")
    args = parser.parse_args()

    if args.demo:
        demo()
        return
    build_dashboard(args.baseline, args.agent, args.out, args.decision_log, args.soak, args.soak_log)


if __name__ == "__main__":
    main()
