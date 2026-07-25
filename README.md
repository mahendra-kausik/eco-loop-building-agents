# Eco-Loop Building Agents

An autonomous, closed-loop Building Management System: **EnergyPlus** simulates the building,
an **open-weight LLM** reasons over live zone state each simulated hour against energy,
comfort and carbon goals, and **injects new HVAC setpoints back into the running simulation**
— no human in the loop.

Submission for the Honeywell hackathon, Problem 1 (Eco-Loop Building Agents).

## Headline results

7 simulated days, Chicago July, identical weather/building for both runs. All
energy figures from EnergyPlus's own meter output (`eplusmtr.csv`), not a
self-reported total — see `docs/ARCHITECTURE.md`'s metering-correction note.

| Metric | Baseline | LLM + supervisor | Rule-based floor (no LLM) |
|---|---|---|---|
| Total facility electricity (kWh) | 1100.0 | 1032.2 (+6.2%) | **1018.3** (+7.4%) |
| HVAC-only electricity (kWh) | 413.6 | 345.8 (+16.4%) | **331.9** (+19.8%) |
| Occupied hours with PMV in [-0.5, +0.5] | 80.7% | **92.7%** (+12.0 pts) | 92.0% (+11.3 pts) |
| Reheat gas (kWh) | 32.9 | 3.5 | **0.0** |
| CO2 (kg, grid-intensity weighted) | 663.8 | 623.8 | **610.3** |
| Simulated days without a crash | — | 14+ (2x standard horizon) | 14+ |

Both configurations beat baseline on energy **and** comfort at the same time —
the savings aren't taken out of occupants' comfort.

Two caveats we'd rather state than bury:

- **~62% of facility electricity is lighting/plug load** that no setpoint or fan
  decision can touch. That's why HVAC-only % is the honest measure of what
  supervisory control actually moves, and why we report both numbers.
- **The LLM beats its own deterministic fallback on comfort, not on energy.**
  A supervisor-enforced comfort guard caps occupied cooling at 25.5 °C — a
  bound derived from measurement, not guessed: PMV breached +0.5 in 7 of 40
  zone-hours the LLM spent above that setpoint, versus 1 of 205 at or below
  it. Capping it pushes LLM comfort past the floor's own 92.0% for the first
  time (92.7%), at the honest cost of 3.5 points of HVAC savings (18.9% before
  the guard, 16.4% after) — holding a tighter comfort band means real extra
  cooling energy on the hours it binds. Since the supervisor falls back to
  exactly the deterministic floor on any LLM failure, the system's worst case
  never loses more than that trade
  ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)).

Dashboard: `results/dashboard.html` (Phase 5) · Demo video: _link TBD_

## Architecture

```
EnergyPlus (pyenergyplus runtime callback)
        │  zone temps, PMV, facility power, outdoor conditions
        ▼
   Tool layer  ── get_building_state / get_forecast_context /
        │         propose_setpoints / inject_setpoints / get_recent_errors
        │              └── also exposed over MCP (src/mcp_server/)
        ▼
   LLM agent (gpt-oss-120b, Cerebras + Groq round-robin)
        │  strict-JSON setpoint decision, once per simulated hour
        ▼
   Safety supervisor  ── schema validation → clamp to safe ranges →
        │                 watchdog timeout → rule-based fallback on ANY failure
        ▼
EnergyPlus actuators (setpoint schedule values)  ── loop closes
```

The supervisor is the point: the simulation **cannot** be killed by a bad, slow, or absent
LLM response. Full detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quickstart

Requires **Python 3.11+** and **EnergyPlus 26.1.0** installed
([energyplus.net/downloads](https://energyplus.net/downloads)).

```bash
git clone https://github.com/mahendra-kausik/eco-loop-building-agents.git
cd eco-loop-building-agents
```

Create and activate a virtual environment, then install dependencies:

**Windows (PowerShell / cmd)**
```
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Configure secrets and paths:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Then edit `.env` and set at minimum:

- `ENERGYPLUS_DIR` — your EnergyPlus install folder, e.g. `C:\EnergyPlusV26-1-0`
- `GROQ_API_KEY` — free key from [console.groq.com](https://console.groq.com)
- `CEREBRAS_API_KEY` — free key from [cloud.cerebras.ai](https://cloud.cerebras.ai). Preferred
  provider: 1M tokens/day vs Groq's 200K on the same model. (`FALLBACK_API_KEY` is still
  accepted as a legacy alias.)

Either key may be left blank — the loop round-robins whichever providers are configured, and
with both blank it runs entirely on the rule-based controller without erroring.

> `pyenergyplus` is **not** a pip package — it ships inside the EnergyPlus installation and
> is loaded at runtime from `ENERGYPLUS_DIR`. Nothing to install for it.

Verify the setup:

```bash
python scripts/smoke_test.py
```

## Running

```bash
python scripts/run_baseline.py      # fixed-schedule baseline run
python scripts/run_agent.py         # AI closed-loop run
python scripts/make_dashboard.py    # builds results/dashboard.html from both runs
python -m src.mcp_server.server     # MCP server exposing the tool layer
```

Both runs use the same building model, weather file and horizon, so the comparison is
like-for-like.

## Repo layout

| Path | What's in it |
|---|---|
| `src/simulation/` | EnergyPlus runner, runtime callbacks, variable/actuator handles |
| `src/agent/` | LLM client, prompts, safety supervisor, rule-based fallback |
| `src/tools/` | The five tool functions (single source of truth) |
| `src/mcp_server/` | FastMCP server exposing the tool layer |
| `src/analysis/` | Metrics and dashboard generation |
| `models/` | Baseline `.idf` + runtime-modified versions |
| `data/` | Weather file, grid carbon-intensity and tariff profile |
| `results/` | Run outputs, `decision_log.jsonl`, dashboard, figures |
| `docs/` | Problem spec, architecture document |
| `scripts/` | Entry points listed above |

Project context and the phase-by-phase build log live in [`CLAUDE.md`](CLAUDE.md) and
[`PROJECT_PLAN.md`](PROJECT_PLAN.md).
