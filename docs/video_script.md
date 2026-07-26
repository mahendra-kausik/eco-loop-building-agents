# Demo video script (≤ 3 min)

Host externally (unlisted YouTube/Drive), link in README and the deck — never bundle the
file itself in the submission zip.

Record against a **1-simulated-day LLM run** (`--days 1` = 24 decisions), not the full
7-day headline run: same code path, same live tool calls, ~1-2 minutes of real wall-clock
time instead of several, and burns a small fraction of the daily LLM token budget. Run it
once before recording so the terminal output is warm/predictable, then record the second
pass.

```
python scripts/run_agent.py --controller llm --days 1
```

Keep `results/decision_log.jsonl` from your last full 7-day run untouched for the
dashboard beat (step 4) — don't overwrite it with the 1-day recording run; redirect
output or copy it back afterward if needed.

## Shot list

**0:00–0:25 — Cold open: what this is**
Show the README headline table or the architecture diagram on screen. One sentence,
spoken: "EnergyPlus simulates a real building; an open-weight LLM reads its live state
every simulated hour and injects new HVAC setpoints back in — closed loop, no human."

**0:25–1:05 — E+ running + live state stream (System Integration criterion)**
Terminal, `python scripts/run_agent.py --controller llm --days 1` running. Let the
EnergyPlus startup banner scroll briefly, then cut to the per-hour printed rows (outdoor
temp, zone temp, PMV, kWh). Point at one row and read it aloud: "this is a live read
from the running simulation, once per simulated hour."

**1:05–1:50 — LLM reasoning + tool calls on screen (Agentic Autonomy criterion)**
Either: (a) tail `results/decision_log.jsonl` in a second terminal
(`python -c "import json;[print(json.loads(l)['reason']) for l in open('results/decision_log.jsonl')]"`
or just `tail -f` on Unix) so the model's stated `reason` field scrolls per decision, or
(b) run `python scripts/mcp_demo.py` briefly to show the same 5 tools
(`get_building_state`, `get_forecast_context`, `propose_setpoints`, `inject_setpoints`,
`get_recent_errors`) being called over MCP. Narrate: "the model sees forecast context —
weather, grid carbon intensity, occupancy ahead — and reasons about pre-cooling into
cheap, clean hours."

**1:50–2:25 — Setpoints injecting + zone temps responding (the closed loop itself)**
Show `scripts/prove_injection.py`'s output or a zoomed decision-log excerpt where a
setpoint change is followed by the next hour's zone temp moving toward it. Narrate: "the
safety supervisor validates and clamps every decision before it's ever written back —
temperature ranges are hard-limited, and if the LLM times out or errors, a deterministic
rule-based controller takes over automatically. The simulation cannot be killed by a bad
LLM response."

**2:25–3:00 — Dashboard + headline savings**
Open `results/dashboard.html` in a browser (this is the real 7-day run's dashboard, not
the 1-day recording run). Scroll past the headline cards (total kWh saved, HVAC-only kWh
saved, comfort-in-band %, peak demand) into the load-curve and comfort-band charts. Close
on the one-line summary: "beats its own deterministic fallback on energy, HVAC-only
energy, and comfort simultaneously — not a trade-off."

## Recording notes
- Increase terminal font size before recording; nobody can read 10pt in a demo video.
- If audio narration isn't available, on-screen text callouts covering the same four
  beats are an acceptable substitute — the rubric asks for the four moments to be shown,
  not necessarily narrated.
- Trim dead air around the EnergyPlus warmup banner (several seconds of boilerplate before
  the first hourly row prints) — cut or speed up in editing.
