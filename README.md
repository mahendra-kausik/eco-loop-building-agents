# Eco-Loop Building Agents

An autonomous, closed-loop Building Management System: **EnergyPlus** simulates the building,
an **open-weight LLM** reasons over live zone state each simulated hour against energy,
comfort and carbon goals, and **injects new HVAC setpoints back into the running simulation**
— no human in the loop.

Submission for the Honeywell hackathon, Problem 1 (Eco-Loop Building Agents).

## Headline results

7 simulated days, Chicago July, identical weather/building for both runs. All energy
figures are read from EnergyPlus's own meter output (`eplusmtr.csv`).

| Metric | Baseline | LLM + supervisor | Rule-based floor (no LLM) |
|---|---|---|---|
| Total facility electricity (kWh) | 1100.1 | **1003.6** (+8.8%) | 1018.4 (+7.4%) |
| HVAC-only electricity (kWh) | 413.7 | **317.3** (+23.3%) | 332.0 (+19.7%) |
| Occupied hours with PMV in [-0.5, +0.5] | 80.7% | **93.1%** (+12.4 pts) | 92.0% (+11.3 pts) |
| Reheat gas (kWh) | 32.9 | **3.6** | 0.0 |
| Peak demand (kW) | 19.9 | **19.0** | — |
| CO2 (kg, grid-intensity weighted) | 663.8 | **602.2** | 610.4 |
| Simulated days without a crash | — | 14 (336/336 decisions, 0 fallback) | 14 (fallback-only) |

The LLM controller beats both the fixed baseline and the deterministic rule-based
controller on energy and comfort simultaneously — the savings aren't taken out of
occupants' comfort.

~62% of facility electricity is lighting and plug load, outside supervisory HVAC
control. HVAC-only figures isolate what the agent can actually influence, which is
why both total and HVAC-only numbers are reported.

Dashboard: [`results/dashboard.html`](results/dashboard.html) · Demo video: _link TBD_

## How it works

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

The supervisor is the point: the simulation **cannot** be killed by a bad, slow, or
absent LLM response.

## Tool-calling architecture

Five plain Python functions in `src/tools/building_tools.py` are the single source
of truth for every interaction with the building:

| Tool | Purpose |
|---|---|
| `get_building_state` | Compact digest of the last completed hourly reading |
| `get_forecast_context` | Next N hours of occupancy + grid carbon/tariff |
| `propose_setpoints` | Builds the prompt, calls the LLM, returns raw text |
| `inject_setpoints` | Clamps to the hard safety range, optionally writes the MCP pending-setpoints file |
| `get_recent_errors` | Tails the E+ `.err` file + failed decisions from the log |

They're consumed two ways: **directly**, by `src/agent/safety.py`'s supervisor
inside the hot control loop (no IPC, no serialization overhead — the reliability
path), and **over MCP**, by `src/mcp_server/server.py` (FastMCP, stdio transport),
so an external MCP client (Claude Desktop, `scripts/mcp_demo.py`) can inspect state
and drive setpoints without touching the loop's source. Because both paths call the
same functions, there is exactly one place that knows how to read a digest or clamp
a setpoint — the MCP server can't drift out of sync with the in-process loop.

The MCP transport is a polled file (`inject_setpoints` writes
`results/pending_setpoints.json`; `--controller mcp` reads it once per simulated
hour, falling back to the rule-based controller if nothing new has arrived). Verified
end-to-end: a setpoint written via the MCP `inject_setpoints` call was read back and
actuated on the very next simulated hour. A file is boring and cannot hang a live
run — deliberate, given a hang directly conflicts with the 30%-weighted requirement
that the closed loop survive an extended horizon without crashing.

Full detail: [`docs/ARCHITECTURE.md` § Tool-calling architecture](docs/ARCHITECTURE.md#tool-calling-architecture).

## Safety supervisor

`src/agent/safety.py` wraps every LLM call in a fixed six-step chain:

1. Build the prompt from `get_building_state` + `get_forecast_context` + `get_recent_errors`.
2. Call the LLM with `response_format={"type": "json_object"}` and a hard timeout.
3. Validate against a Pydantic schema (`SetpointDecision`); one retry on failure,
   with the validation error fed back to the model.
4. Clamp the validated pair through the same `clamp_setpoints` logic the rule-based
   controller uses — not a reimplementation.
5. **Any** failure anywhere in this chain — timeout, network error, invalid JSON
   after retry, provider outage — falls through to the deterministic rule-based
   controller. The simulation is never blocked on the LLM.
6. Every decision (prompt inputs, raw reply, provider, latency, retry flag, clamped
   result, fallback flag, error) is appended as one line to `results/decision_log.jsonl`.

Verified crash-proof with the LLM totally unreachable: a full 7-simulated-day run
with both API keys blank completed with exit code 0, 168/168 setpoint injections, 0
controller errors, and output numerically identical to a pure rule-based run —
confirming the fallback path *is* the deterministic controller, not an approximation
of it. A separate 14-day run with the LLM live the entire time (336/336 decisions,
0 fallback) is the endurance counterpart.

An anti-thrash rate limit caps hour-to-hour setpoint movement at ±1.5°C (exempted
across occupied↔unoccupied transitions, where a real step is the point) — insurance
against oscillation between LLM decisions or between an LLM decision and a very
different fallback value.

Full detail: [`docs/ARCHITECTURE.md` § Safety supervisor](docs/ARCHITECTURE.md#safety-supervisor-the-reliability-path).

## Prompt engineering

The system prompt states the decision priority explicitly — comfort is a hard,
non-negotiable floor, then energy, then carbon/cost — and restates the hard clamp
ranges even though the supervisor enforces them regardless, so the model has every
incentive to stay inside them on its own. The user prompt is always the JSON-encoded
state digest and forecast, never raw simulation output.

The forecast context distinguishes the hour being decided *for* from the hour
described in the state digest (`forecast.decision_hour_occupied`) — the digest
reflects the last completed hour, but the model is setting the next one, and those
differ exactly at occupancy transitions. Making the distinction explicit in the
prompt, rather than leaving the model to infer it, is what lets the agent set full
setback the moment occupancy ends instead of coasting on near-occupied setpoints
into an empty building.

Full detail: [`docs/ARCHITECTURE.md` § Prompt strategy](docs/ARCHITECTURE.md#prompt-strategy).

## Handling lengthy simulation logs

The LLM never sees EnergyPlus's raw output — not the `.eso` file, not `.err`, not
the runner's 5-zones × N-hours CSV. `get_building_state` reduces one hourly reading
to ~12 scalar fields (mean/min/max zone temp, the single worst-|PMV| zone, current
setpoints, outdoor temp, energy this hour) regardless of how many zones the building
has. `get_forecast_context` caps the lookahead window at a fixed horizon rather than
exposing the full 24-hour carbon/tariff table. `get_recent_errors` tails only the
last N severity lines plus the last N failed decisions, clipped to a fixed character
budget — never the full log.

Net effect: **prompt size is constant regardless of simulation horizon.** A 7-day
run and a 30-day run send the same-sized prompt every hour.

Full detail: [`docs/ARCHITECTURE.md` § Long-log / high-volume-data handling](docs/ARCHITECTURE.md#long-log--high-volume-data-handling).

## Prompt latency management

Measured against Groq and Cerebras (`gpt-oss-120b` on both, identical
OpenAI-compatible API — only `base_url`/`api_key` differ):

| Scenario | Latency |
|---|---|
| Trivial one-word prompt | Groq 996 ms, Cerebras 405 ms |
| Real prompt, 48-decision run | p50 1.0–1.5 s, p95 ~2.2–2.5 s |
| Full 7-day run, 168 decisions | wall clock ≈ 7 min total (incl. E+ compute) |

The LLM decides once per simulated hour — decoupled from EnergyPlus's own timestep
rate, which advances far faster than real time. Left unmanaged, a run's ~24–168
hourly decisions would fire within seconds of each other in wall-clock time,
straight into free-tier rate limits. `llm_client.py` enforces a minimum real-time
interval between calls to the same provider, sized per-provider against each one's
measured limit rather than a single guessed number: Groq and Cerebras bind on
different constraints (Groq's cap is a *token* budget, not a request count) and
inverting that assumption cost 8x in the interval before it was corrected by reading
the actual 429 response bodies instead of assuming. Cerebras and Groq are
round-robined per call with within-call failover to the other, which took genuine
LLM participation in a live run from 43% to over 80%.

Error strings fed back to the model via `get_recent_errors` are clipped to a fixed
length — the raw provider error bodies otherwise add ~30% to prompt size the moment
failures start, which is exactly when the token budget is already the problem.

Full detail: [`docs/ARCHITECTURE.md` § Latency measurement & management](docs/ARCHITECTURE.md#latency-measurement--management).

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
| `data/` | Grid carbon-intensity and tariff profile (weather loads from the EnergyPlus install) |
| `results/` | Run outputs, `decision_log.jsonl`, dashboard, figures |
| `docs/` | Problem spec, architecture document |
| `scripts/` | Entry points listed above |

Full technical detail — prompt strategy, latency measurement, metering corrections, and
the Phase 5 spec-gap closures — lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
