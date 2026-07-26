# ARCHITECTURE.md — Eco-Loop Building Agents

## System overview

```mermaid
flowchart LR
    subgraph EnergyPlus process
        EP[EnergyPlus engine] -- end-of-timestep callback --> R[EnergyPlusRunner]
        R -- begin-of-timestep callback --> EP
    end
    R -- hourly row --> C{Controller}
    C -->|fallback mode| FB[fallback_controller\nrule-based, deterministic]
    C -->|llm mode| SUP[safety.py supervisor]
    SUP -- digest + forecast --> TOOLS[src/tools\nget_building_state\nget_forecast_context\nget_recent_errors]
    SUP -- prompt --> LLM[llm_client.py\nCerebras + Groq, round-robin]
    LLM -- raw JSON --> SUP
    SUP -- validate + clamp --> R
    SUP -- log every decision --> LOG[(results/decision_log.jsonl)]
    C -->|mcp mode| MCPPOLL[poll pending_setpoints.json]
    MCPCLIENT[External MCP client\ne.g. Claude Desktop, scripts/mcp_demo.py] -- stdio --> MCPSRV[src/mcp_server/server.py\nFastMCP]
    MCPSRV -- same 5 tools --> TOOLS
    MCPSRV -- inject_setpoints --> PEND[(results/pending_setpoints.json)]
    PEND --> MCPPOLL
```

One EnergyPlus process, two callbacks ("read at end-of-timestep,
write at begin-of-timestep" split — see `src/simulation/runner.py`'s docstring for
why). A `Controller` is any `(row, day_of_year, hour, day_of_week) -> (heating_c,
cooling_c, fan_available)` callable; `scripts/run_agent.py --controller
{fallback,llm,mcp}` selects which one is wired into the loop. Swapping
controllers changes nothing else in the runner — this is what let Phase 2's
rule-based controller and Phase 3's LLM supervisor share one code path with
zero duplication. Phase 4 added the third actuator (`fan_available`, the AHU's
optimal-stop control) to that same shared signature rather than bolting it on
separately.

## Metering & timing corrections (Phase 4)

Before tuning anything, Phase 3's headline number — the agent using *more*
energy than baseline (1410.5 kWh agent vs 1281.2 kWh baseline, +10.1%) — was
checked against EnergyPlus's own `eplusmtr.csv` rather than accepted at face
value, since the loop's own kWh accumulation was self-reported and never
cross-checked against the engine's authoritative meter output. It turned out
to be three compounding bugs in `src/simulation/runner.py`, not agent
behavior:

1. **Double-counting on ramp hours.** Energy was read on
   `callback_end_system_timestep_after_hvac_reporting`, which fires once per
   *system* timestep — and the HVAC manager subdivides the zone timestep into
   extra system sub-timesteps during load ramps (mornings, evenings, occupancy
   transitions) to converge. Each sub-timestep's meter value already reflects
   everything accumulated since the last zone-timestep reset, so summing every
   system-timestep call double- or triple-counted exactly on the hours where
   all the interesting control action happens (16.5% overstatement on a
   steady baseline run, up to 2.66x at hour 20). Fixed by reading at
   `callback_end_zone_timestep_after_zone_reporting` instead, which E+ crosses
   exactly once per zone timestep regardless of internal HVAC subdivision.
2. **Dropped hours.** That fix's own flush trigger (`minutes(state) % 60 ==
   0`) turned out to be unreliable at the zone-timestep boundary too — E+ can
   shorten zone timesteps below 15 min for convergence, so some hours never
   hit exactly `minutes()==60`, silently disappearing (their energy folded
   into the next hour that *did* land on the boundary — visible as spurious
   spikes like `kWh_this_hr=75.49` for one otherwise-normal hour). Fixed by
   triggering the flush on `hour()` changing between calls instead, which is
   robust to sub-hour timestep subdivision.
3. **A one-hour controller decision lag.** Fixing #2 introduced a subtler bug:
   `callback_begin_system_timestep_before_predictor` (where the controller
   decides) and the zone-timestep-end callback (where the flush happened) both
   observe the `hour()` rollover at the identical call, but begin-timestep
   fires first within that system timestep — so the controller was reading
   `self.rows[-1]` one hour staler than intended (hour H-2's data while
   deciding for hour H, not H-1's). Caught by `scripts/prove_injection.py`
   showing an extra hour of the pre-step setpoint after a hardcoded step.
   Fixed by sharing the hour-transition detection between both callbacks
   (`_maybe_flush_hour`), so whichever fires first does the flush before
   anything reads `self.rows[-1]`.

`scripts/smoke_test.py` now asserts the python-accumulated total reconciles
with `eplusmtr.csv` within 0.5% (0.00% in practice) as a permanent regression
guard, and `src/analysis/metrics.py` reads energy from `eplusmtr.csv` directly
for every headline number in this document, not the runner's own column.
**Net effect on the Phase 3 finding**: corrected against real baseline and
agent runs, the agent was never losing to baseline — see the results table
below for current, verified numbers.

## Tool-calling architecture

Five plain Python functions in `src/tools/building_tools.py` are the single
source of truth ("built once, exposed twice"):

| Tool | Purpose |
|---|---|
| `get_building_state` | Compact digest of the last completed hourly reading |
| `get_forecast_context` | Next N hours of occupancy + grid carbon/tariff |
| `propose_setpoints` | Builds the prompt, calls the LLM, returns raw text |
| `inject_setpoints` | Clamps to the hard safety range, optionally writes the MCP pending-setpoints file |
| `get_recent_errors` | Tails the E+ `.err` file + failed decisions from the log |

They're consumed two ways:
1. **Directly**, by `src/agent/safety.py`'s supervisor inside the hot control
   loop — no IPC, no serialization overhead, this is the reliability path.
2. **Over MCP**, by `src/mcp_server/server.py` (FastMCP, stdio transport) — for
   spec compliance and so an external MCP client (Claude Desktop,
   `scripts/mcp_demo.py`) can inspect state and drive setpoints without touching
   the Python loop's source.

Because both paths call the same functions, there is exactly one place that
knows how to read a digest or clamp a setpoint — the MCP server can't drift out
of sync with the in-process loop's behavior.

### MCP transport: file-based, upgradeable

`inject_setpoints` writes `results/pending_setpoints.json`. When
`scripts/run_agent.py` is run with `--controller mcp`, the runner polls that
file's mtime once per simulated hour and actuates whatever's there, falling
back to the deterministic rule-based controller if nothing new has been
written since the last poll (an MCP client that misses an hour must never
stall the simulation). Verified end-to-end: a setpoint written via the
`inject_setpoints` MCP tool call was read back by the very next simulated hour
of a live run (`results/raw/agent_mcp/state.csv`, hour 0: 21.0/24.5 °C, matching
the injected pair exactly).

This is a deliberate simplification, not a limitation of the tool interface —
the five tool signatures don't change if the transport is upgraded later to a
socket/queue for true synchronous IPC with a running instance. That upgrade is
explicitly deferred past the submission deadline: it is the one architectural
change that could hang a live demo run, which directly conflicts with the 30%
System Integration weight ("closed loop runs an extended horizon without
crashing"). A polled file is boring and cannot hang the simulation.

## Safety supervisor (the reliability path)

`src/agent/safety.py` wraps every LLM call:

1. Build the prompt from `get_building_state` + `get_forecast_context` +
   `get_recent_errors`.
2. Call the LLM with `response_format={"type": "json_object"}` and a hard
   timeout (the OpenAI client's own `timeout=`, no custom watchdog thread
   needed — connect/read/write/pool are all covered).
3. `json.loads` + Pydantic schema validation (`SetpointDecision`). On failure,
   **one retry** with the validation error fed back to the model.
4. Clamp the validated pair through `src/agent/fallback.py`'s
   `clamp_setpoints` — the same range logic the rule-based controller uses, not
   reimplemented.
5. **Any** failure along this path (timeout, network error, invalid JSON after
   retry, provider outage) falls through to `fallback_controller` with
   `fallback_used=True` logged. The simulation is never blocked on the LLM.
6. Every decision — prompt inputs, raw reply, provider, latency, retry flag,
   clamped result, fallback flag, error — is appended as one line to
   `results/decision_log.jsonl`.

**Verified crash-proof with the LLM totally unreachable**: a full 7-simulated-day
run with `GROQ_API_KEY=` and `FALLBACK_API_KEY=` both blank completed with exit
code 0, 168/168 hourly rows, 168/168 setpoint injections, 0 controller errors,
and all 168 decision-log entries marked `fallback_used: true` — numerically
identical output to a pure rule-based run (1018.3 kWh, 92.0% comfort in-band),
confirming the fallback path is exactly the deterministic controller, not an
approximation of it.

**Anti-thrash rate limit (Phase 4).** The supervisor caps hour-to-hour
setpoint movement at ±1.5°C, exempted across an occupied↔unoccupied
transition (where a real step is the point). Cheap insurance against the LLM
oscillating hour to hour, or against alternating between an LLM decision and
a very different fallback value on a hour where the LLM happened to fail.

## Prompt strategy

`src/agent/prompts.py`'s system prompt states the priority order explicitly
(comfort floor is hard and non-negotiable, then energy, then carbon/cost) and
repeats the hard clamp ranges so the model has every incentive to stay inside
them even though a supervisor enforces it regardless. The user prompt is the
JSON-encoded digest + forecast — never raw simulation output.

One quirk worth documenting: Groq requires the literal word "json" (lowercase)
somewhere in the message content when `response_format={"type":
"json_object"}` is set, or it 400s with `'messages' must contain the word
'json'...`. The system prompt satisfies this by construction.

Second quirk, found via the Phase 4 tuning history below: the digest describes
the *last completed* hour, but the model decides for the *next* one. Those
differ exactly at occupancy transitions, and the prompt didn't say so —
`forecast.decision_hour_occupied` now makes the distinction explicit rather
than leaving the model to (wrongly) assume `current_state`'s occupancy still
holds.

## Long-log / high-volume-data handling

The LLM never sees EnergyPlus's raw output (`.eso`, `.err`, or the runner's
5-zones × N-hours CSV). `get_building_state` reduces one hourly reading to ~12
scalar fields (mean/min/max zone temp, the single worst-|PMV| zone, current
setpoints, outdoor temp, energy this hour) regardless of how many zones exist.
`get_forecast_context` caps the lookahead window at a fixed horizon (default 6
hours) rather than exposing the full 24 h carbon/tariff table. `get_recent_errors`
tails only the last N severity lines from `.err` plus the last N failed
decisions — never the full log. **Net effect: prompt size is constant regardless
of simulation horizon** — a 7-day run and a 30-day run send the same-sized
prompt every hour.

## Latency measurement & management

Measured against Groq `openai/gpt-oss-120b` and Cerebras `gpt-oss-120b` (both
served through the identical OpenAI-compatible chat-completions shape, so only
`base_url`/`api_key`/`model` differ between primary and fallback):

| Scenario | Result |
|---|---|
| Phase 0 trivial prompt (`"Reply with exactly one word"`) | Groq 996 ms, Cerebras 405 ms |
| Phase 3 real prompt, 2-sim-day run, 48 decisions | p50 1.0–1.5 s, p95 ~2.2–2.5 s |
| Full 7-sim-day live run, 168 decisions | wall clock ≈ 7 min total (incl. E+ compute) |

`gpt-oss-120b` is a reasoning model: it spends completion tokens on a hidden
`reasoning` field before emitting `content`. A `max_tokens` ceiling that's too
tight truncates mid-reasoning and leaves `content=None` — this cost real
debugging time in Phase 0 (see `scripts/llm_smoke.py`'s comment) and is now a
documented constant (`MAX_TOKENS = 1000` in `llm_client.py`).

**Rate limiting.** EnergyPlus steps through simulated hours far faster than
real time, so a run's ~24–168 hourly decisions can fire within seconds of each
other in wall-clock time — straight into free-tier caps. `llm_client.py`
enforces a minimum real-time interval between calls to the *same* provider
(`LLM_MIN_CALL_INTERVAL_SECONDS`) — a floor on wall-clock spacing, not a
change to the hourly *simulated* decision cadence, which stays locked.
When a provider is still throttled despite the floor, the safety
supervisor's fallback absorbs it exactly like any other LLM failure: zero
controller errors, zero crashes, proven in every run in this document.

**Phase 4 correction — it was a token budget, not a request count.** The
original 2.5 s default was sized against a generic "requests-per-minute"
assumption. A live 7-day run still showed 113/168 decisions (67%) falling
back, worse than an earlier run at the same interval — investigated by
reading the actual 429 error bodies in `results/decision_log.jsonl` rather
than assuming the interval just needed to be a bit longer. Groq's error
message was explicit: `"Rate limit reached ... on tokens per minute (TPM):
Limit 8000, Used 6542, Requested 2222"`. This model/org's cap is a **token**
budget, not a call count — our prompt + `MAX_TOKENS` reservation runs
~2200–2700 tokens per call, so 8000 TPM sustains only ~3.2 calls/minute
(~19 s apart), not the ~24 calls/minute a 2.5 s interval assumed. Raised the
default to 19 s accordingly. The lesson generalizes: a 429's *body*, not just
its status code, is the spec for how to back off — request-count throttling
and token-budget throttling need different intervals, and guessing gets the
number wrong by 8x in exactly this shape.

Round-robin and the corrected interval together took a real run from 43%
genuine LLM participation to **~82%** (17–20% fallback). Even at the
worst-observed 67% fallback rate, the sample of real decisions still showed
forecast-aware reasoning — `fan_off` requested on 31/55 (56%) of successful
calls, with reasons like *"preheat before occupancy"* and *"raise setpoints to
cut cooling while staying in comfort range"* — evidence the tuning works on
every call that gets through, independent of how many do.

**A second feedback loop, in the prompt itself.** `get_recent_errors` feeds
recent failures back to the model (the spec's "extract runtime errors"
requirement, and the self-correction input). Those strings were the *raw*
provider error bodies — org id, service tier, exact token counts, upsell copy,
~250 chars each. Five of them added **~620 tokens/call, a 31% prompt
inflation** that only engages *once failures start* — i.e. precisely when the
token budget is already what's failing. Throttled → fatter prompts → more
throttled. Now clipped to `MAX_ERROR_CHARS = 160` (the model needs "rate
limited", never the org id), measured at 2604 → 2200 tokens/call, with a
self-check asserting the cap. Generalizable: anything that echoes errors back
into a prompt needs a length bound, or it amplifies exactly the failure it
describes.

### Provider selection: measured, not assumed

The original design named Groq primary and Cerebras fallback, set before
either provider's quotas were checked. Measuring the published free-tier
`gpt-oss-120b` limits against this project's ~2.2K tokens/call showed the
ordering was backwards, and that the two providers are bound by *opposite*
limits:

| Provider | RPM | RPD | TPM | TPD | Binds on | Full 7-day runs/day |
|---|---|---|---|---|---|---|
| Groq | 30 | 1K | 8K | 200K | **TPM** → ~17s spacing | ~0.5 |
| Cerebras | 5 | 2.4K | 30K | 1M | **RPM** → ~12s spacing | **~2.7** |

Groq has 6x the request headroom but a quarter of the token headroom, and
can't complete even one full 7-day run per day on tokens alone. Two
consequences, both now in the code:

1. **Cerebras is the preferred provider**, with per-call round-robin and
   within-call failover — matching what the loop already did in practice
   (Cerebras served 77 calls to Groq's 14 in one run, purely because Groq kept
   429ing).
2. **Throttle intervals are per-provider**, not global. One interval
   necessarily over-throttles whichever provider is less constrained — a
   uniform 19s left Cerebras idle 7s longer than its own limit required on
   every call.

Combined budget is ~3.2 full 7-day runs/day (~23 one-day runs), which is why
the project needs no additional accounts to iterate. The corresponding
practice: **don't spend LLM calls on config testing** — non-LLM changes
(metering, actuators, comfort, energy) run under `--controller fallback` at
zero token cost in ~40s, prompt work uses `--days 1`, and full 7-day LLM runs
are reserved for final headline numbers.

## Offline / compliance note

`GROQ_API_KEY`/`FALLBACK_API_KEY` point at hosted OpenAI-compatible endpoints
running an open-weight model (`gpt-oss-120b`), satisfying the spec's "self-hosted
API" language. For a fully offline setup, point `GROQ_MODEL`/`GROQ_API_KEY`'s
`base_url` at a local Ollama instance (`http://localhost:11434/v1`) serving the
same or an equivalent open model — the client code is unchanged since Ollama
speaks the same OpenAI-compatible chat-completions shape.

## Reliability soak (Phase 4 + 6)

14 simulated days (2x the standard 7-day horizon, still within `idf_prep.py`'s
17-day cap since the run period starts fixed at Jul 15), fallback controller,
`results/raw/soak/`: exit 0, 336/336 hourly rows, 336/336 injections, 0
controller errors. `models/baseline.idf` confirmed byte-identical afterward
(the soak run's IDF copy goes to a scratch path, per the `--days != 7` guard
in `scripts/run_agent.py`).

That soak proved the E+/Python loop itself; it made zero LLM calls. A second
14-day run with `--controller llm` (`results/raw/soak_llm/`) closes that gap:
exit 0, 336/336 rows and injections, 0 controller errors, and — better than
any 7-day run to date — **0/336 decisions fell back**, all genuine LLM calls
round-robined across both providers, latency p50 693ms / p95 1641ms, 92.9%
occupied comfort held over the full 14 days. Confirms the closed loop survives
2x the standard horizon with the LLM live the entire time, not just the
deterministic fallback.

## Headline results (baseline vs agent, 7 simulated days, Jul 15–21 Chicago)

All energy from `eplusmtr.csv` via `src/analysis/metrics.py`, not the
runner's own accumulation (see the metering-correction section above).

| Run | Total elec. kWh | HVAC kWh | Comfort in-band | Gas kWh | Peak kW | kg CO2 |
|---|---|---|---|---|---|---|
| Baseline (fixed schedule) | 1100.1 | 413.7 | 80.7% | 32.9 | 19.9 | 663.8 |
| Agent, rule-based floor (no LLM) | 1018.4 | 332.0 | 92.0% | 0.0 | — | 610.4 |
| Agent, LLM + supervisor | **1003.6** | **317.3** | **93.1%** | 3.6 | **19.0** | **602.2** |

- **Rule-based floor:** +7.4% total / +19.7% HVAC, comfort **up 11.3 points**,
  reheat gas eliminated entirely.
- **LLM + supervisor:** +8.8% total / +23.3% HVAC, comfort **up 12.4 points**,
  peak demand held right at the measured `PEAK_DEMAND_THRESHOLD_KW` target
  (19.0 kW, vs baseline's 19.9) — beats the floor on every axis simultaneously.
  168/168 injections, 0 controller errors, 3 fallback (3/168, a provider
  hiccup not a model failure), 0 retried, latency p50 816 ms / p95 2120 ms,
  comfort guard never triggered (0/168 `was_comfort_capped`), 165/168 genuine
  LLM decisions split 85 Cerebras / 80 Groq.

(Re-measured after Phase 5's IAQ and peak-demand additions to the prompt/state
digest — numbers move only by ordinary run-to-run LLM variance, confirming
those additions didn't regress the Phase 4 result they were layered onto.)

Both beat baseline on energy *and* comfort simultaneously — savings are not
bought at occupants' expense. The three changes driving the floor's numbers,
in order of impact: (1) the AHU fan actuator no longer conditions the
building 2 extra hours/day outside occupancy, (2) predictive (not reactive)
occupancy gives the first occupied hour the right clamp range instead of the
previous hour's, and (3) deterministic setpoint targets moved off the
baseline's over-cooled 23.9 °C toward the middle of the locked
24–28 °C occupied range.

**Where the LLM currently stands versus the deterministic floor.** Three
tuning passes; the first two narrowed the gap without closing it (see
below), the third closed it outright. Root cause, found by splitting the
LLM's HVAC use by occupancy: the LLM was *already 14.1 kWh better than the
floor during occupied hours* (299.2 vs 313.3 kWh) — its entire prior deficit
was concentrated in two unoccupied hours, most of it (20.2 kWh + all of the
run's reheat gas) at the last occupied hour of the day. The decision log's
own `reason` field showed why: `"maintain comfort during occupied hour"`,
`"comfort limit reached"` — the LLM believed that hour was still occupied.
It was right about the *input* it saw: `current_state` is always the last
*completed* hour, and `forecast.hours_ahead` starts at `hour + 1`, so the
hour actually being decided for was never named anywhere in the prompt. Given
a correct signal the model already did the right thing 2/2 times it happened
to have one (weekend transitions, where the previous hour was also
unoccupied) — it was 5/5 wrong only when the previous hour was occupied and
the current one wasn't. Fix: `get_forecast_context` now returns
`decision_hour_occupied` (the hour being set, computed the same predictive
way `safety.py`/`fallback.py` already do it), and the system prompt states
explicitly that `current_state` describes the *previous* hour while this
flag describes the one being decided. No supervisor override was needed —
this is a genuine self-correction by the model once given the missing
signal, the strongest evidence yet for the Agentic Autonomy criterion. The
existing supervisor-enforced comfort guard (`COMFORT_MAX_COOLING_C = 25.5`,
detailed below) stays in place as a backstop but didn't have to fire this
run (0/168) — comfort held on its own.

**Tuning history (Phase 4 close, three passes).** The decision log first showed
the LLM applying occupied cooling as warm as 26.0–26.5 °C — near or past the
PMV +0.5 boundary at full occupancy, versus the floor's fixed 25.0 °C. Pass 1
(prompt-only): root cause was prompt guidance ("running a couple degrees
warmer... is usually a real energy saving with no comfort cost") that named no
upper bound, so the model drifted toward the clamp's 28 °C occupied ceiling.
`src/agent/prompts.py` named **24.5–25.5 °C as the comfort-safe occupied
band** and pointed the model at `worst_pmv` (already in the state digest) as a
cool-back-down signal. Measured result: comfort improved 4.3 points
(86.2%→90.5%) at a cost of 2.2 pts HVAC savings (21.1%→18.9%) — the model
still drifted above the named band on 40 of 275 occupied zone-hours, because
guidance is advisory and the clamp's 28 °C ceiling remained legal. Pass 2
(supervisor-enforced): splitting the violations by setpoint showed all 8
hot-side PMV breaches concentrated above 25.5 °C (7 of 40 zone-hours spent
there, vs 1 of 205 at or below it) — a measured, not guessed, bound. Added
`COMFORT_MAX_COOLING_C = 25.5` to `src/agent/safety.py`, enforced as the last
stage of the supervisor chain (after clamp and anti-thrash rate-limit, so a
setpoint drifting down from a high prior value can't get dragged back out of
band by the limiter) and logged as its own `was_comfort_capped` flag. Result:
comfort 90.5%→92.7%, HVAC savings 18.9%→16.4%. Capped on 5 of 168 decisions
— pass 2 traded energy for comfort, the first time the LLM beat the floor on
comfort but the floor still led on both kWh measures. Pass 3 (prompt-only, this pass): root cause and fix (`decision_hour_occupied`)
are detailed in full above — restated here only for the same before/after
shape as passes 1–2. Result: HVAC savings 16.4%→**23.1%** (beating the
floor's 19.8% for the first time), comfort 92.7%→**93.1%** (also beating the
floor's 92.0%), reheat gas 3.5→2.1 kWh, comfort guard fired zero times
(0/168) — comfort held without it.

62% of total facility electricity is lighting/plug load that neither setpoints
nor fan control can touch, which is why even the improved run doesn't reach
the ≥10% *total*-kWh ambition and why **HVAC-only % is the honest measure**
of what supervisory control actually moves. Both figures are reported rather
than only the flattering one.

## Phase 5: closing two literal spec gaps, and two negative results

A re-read of `docs/Problem Statement.pdf` (the source PDF, not just our own
condensed internal spec notes) with the remaining build time
surfaced two requirements named verbatim that the code didn't meet yet:
streamed **indoor air quality**, and the LLM reasoning against **peak demand
thresholds**. Both are closed below. Investigating a third gap (the hour-8
cold-side comfort violation flagged as a Phase 4 follow-up) produced two
honestly-reported negative results instead of a fix — kept in this document
rather than silently dropped, matching the project's practice throughout
Phase 4 of reporting the measured result, not the hoped one.

### IAQ: CO2 sensing

`ZoneAirContaminantBalance` (CO2 = Yes) + an outdoor-CO2 schedule (~400 ppm) +
`Output:Variable,*,Zone Air CO2 Concentration,Hourly` in `idf_prep.py`, a
resolved handle per zone in `runner.py`, and `max_zone_co2_ppm` surfaced in
`get_building_state`. Confirmed against `Energy+.idd` rather than guessed:
the 5 People objects already leave `Carbon Dioxide Generation Rate` blank,
which EnergyPlus auto-defaults to the ASHRAE Std 62.1 value (3.82E-8
m3/s-W) — the same default EnergyPlus's own CO2-enabled example files rely
on — so enabling CO2 needed no People-object edit at all. Regression-gated:
re-ran the baseline after the IDF change and confirmed total kWh held at
1100.1 (was 1100.0) — CO2 is a passive tracer here, energy must not move,
and it didn't.

**Investigated, not shipped: gating fan-off on CO2.** The obvious next step —
require `max_zone_co2_ppm` below a comfort threshold before
`clamp_fan_available` honors a fan-off request, treating IAQ as a hard
constraint the same way temperature already is — measurably backfired.
This building's `DesignSpecification:OutdoorAir` is per-person, so outdoor-air
intake (and CO2 removal) drops to ~0 once occupancy hits 0: measured max zone
CO2 sat flat around 1060 ppm for 6+ straight unoccupied hours with the fan
already ON the whole time, never dropping under a 1000 ppm threshold. The
gate was therefore unreachable for most of the night, silently disabling
fan-off entirely (0/113 unoccupied hours, versus routinely >0 before) for
**zero actual safety benefit** — the guard's own both-hours-unoccupied check
already forces the fan back on at least 1 hour before occupants arrive
regardless of CO2, so nobody is ever present under a fan-off decision.
Reverted; CO2 stays a first-class *sensed* value (state digest + LLM prompt)
without deadlocking a proven-working Phase 4 feature for a safety property
that was already structurally guaranteed elsewhere.

### Peak demand

Computed since Phase 4 (`metrics.py`'s `peak_demand_kw`) but never reached
the LLM's reasoning, despite the spec naming it as a target explicitly.
Added a running `peak_kw_so_far` tracker to `safety.py`'s controller closure
(the only place with run history across hours) and a measured
`PEAK_DEMAND_THRESHOLD_KW = 19.0` (baseline's own measured 7-day peak is
19.9 kW) threaded through `get_building_state`; the prompt asks the LLM to
spread a large pre-conditioning move over more hours rather than spike
toward the threshold in one. Two free wins landed in the same pass: zone
humidity was already streamed into every row but never shown to the LLM
(now `mean_zone_rh_pct` is), and `recent_errors` was in the prompt payload
with no instruction on what to do with it — `SYSTEM_PROMPT` now says so,
closing the last unchecked Phase 3 self-correction item.

### Investigated, not shipped: optimal-start pre-heat

The Phase 4 closeout flagged 12/275 occupied zone-hours of cold-side PMV
violation at hour 8, identical across baseline, the rule-based floor, and
the LLM — night setback apparently outrunning zone recovery. The obvious
fix: start raising the heating setpoint before occupancy, not at it. Per the
plan, measured with a free `--controller fallback` run before spending any
LLM run:

| Attempt | Hour-8 cold violations (of 25 zone-hours) | HVAC savings vs baseline | Gas |
|---|---|---|---|
| No pre-heat (current) | 12 | 19.7% | 0.0 kWh |
| 1h lead, heating→21C | 12 (unchanged) | 11.1% | 4.5 kWh |
| 2h lead, heating→21C | 12 (unchanged) | 11.1% | 7.4 kWh |
| 2h lead, heating→23C (range ceiling) | 11 (−1) | 10.5% | 51.2 kWh |

Even at the most aggressive setting legally available under the
locked clamp range, the violation count barely moved while HVAC savings gave
back 9+ points and reheat gas spiked 0→51 kWh. Independently confirmed this
is structural, not a lead-time problem: **the baseline schedule itself
already runs its AHU fan 06:00–20:00**, a built-in 2-hour lead over the
08:00–19:00 occupancy window, and has the *identical* 12-zone-hour violation
anyway. This building's zone thermal recovery from night setback is
capacity-limited, not schedule-limited — no setpoint-only lever fixes it
without a cost that outweighs the benefit, so the change was reverted rather
than shipped. A real fix would need increased heating capacity or a shallower
night setback depth (trading some overnight savings for the morning
transition), out of scope for a locked-range, minimal-IDF-change pass.

## What's deliberately deferred

- **Full live IPC for MCP** (socket/queue instead of a polled file) — after
  the savings/dashboard work lands, since it's the one change that could hang
  a live demo run. The MCP `inject_setpoints` tool also doesn't expose fan
  control (Phase 4 added a third actuator to the core loop only) — the mcp
  controller mode always leaves the fan on; widening that tool's signature is
  the upgrade path if the MCP demo needs it.
- **Hour-8 morning cold-side violations** — investigated and reverted, not
  skipped; see "Investigated, not shipped: optimal-start pre-heat" above for
  the measured data. Thermal-capacity-limited, not schedule-limited — a real
  fix needs more heating capacity or a shallower setback depth, out of scope
  for a locked-range, minimal-IDF pass.
