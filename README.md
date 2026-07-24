# Eco-Loop Building Agents

An autonomous, closed-loop Building Management System: **EnergyPlus** simulates the building,
an **open-weight LLM** reasons over live zone state each simulated hour against energy,
comfort and carbon goals, and **injects new HVAC setpoints back into the running simulation**
— no human in the loop.

Submission for the Honeywell hackathon, Problem 1 (Eco-Loop Building Agents).

## Headline results

| Metric | Baseline (fixed schedule) | AI closed loop | Delta |
|---|---|---|---|
| Total facility electricity (kWh) | _TBD_ | _TBD_ | **_TBD_ %** |
| Occupied hours with PMV in [-0.5, +0.5] | _TBD_ | _TBD_ | _TBD_ |
| CO2 (kg, grid-intensity weighted) | _TBD_ | _TBD_ | _TBD_ |
| Simulated days completed without a crash | — | _TBD_ | — |

Dashboard: `results/dashboard.html` · Demo video: _link TBD_

## Architecture

```
EnergyPlus (pyenergyplus runtime callback)
        │  zone temps, PMV, facility power, outdoor conditions
        ▼
   Tool layer  ── get_building_state / get_forecast_context /
        │         propose_setpoints / inject_setpoints / get_recent_errors
        │              └── also exposed over MCP (src/mcp_server/)
        ▼
   LLM agent (gpt-oss-120b, Groq primary / Cerebras fallback)
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
- `FALLBACK_API_KEY` — optional; free key from [cloud.cerebras.ai](https://cloud.cerebras.ai).
  Leave blank to run on Groq alone (the rule-based fallback still protects the loop).

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
