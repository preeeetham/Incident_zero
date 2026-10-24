# Handoff: where we stopped (2026-10-24, ~03:05 UTC / 08:35 IST)

Read this first. Session 2 (morning of 2026-10-24) is at the top; session 1's notes follow unchanged below.

## Status at 05:00 UTC: goal met (10 different faults at 10/10, see LIVE_RESULTS.md)

Still running: demo, Action API and server. Next: re-run homepage-flood (40 users), payment-paused,
currency-partitioned, combo-valkey-and-payment, then the blind set.

## Resume (session 3)

Everything is **stopped** cleanly: demo containers stopped (not removed), agents, Action API and campaign stopped,
no fault injected, every flag off, no open incident, server mode LIVE.

```bash
cd ~/Developer/bgsw/incident_zero
.venv/bin/incident_zero demo up                       # ~2 min; product-catalog may restart once (DB race): harmless
nohup .venv/bin/incident_zero action-api > /tmp/iz-api.log 2>&1 &
nohup .venv/bin/incident_zero up > /tmp/iz-server.log 2>&1 &    # War Room: http://127.0.0.1:8700
curl -s -X POST http://127.0.0.1:8080/loadgen/swarm -d 'user_count=5&spawn_rate=5'   # make sure load is normal
# then the remaining 18 scenarios in one campaign (~2.5 h), kept awake:
nohup .venv/bin/incident_zero practice campaign catalog-db-down,quote-packet-loss,valkey-down,checkout-cpu-starved,currency-memory-starved,shipping-crash-loop,payment-charge-fails,cart-stopped,homepage-flood,recommendation-cache-leak,catalog-latency,ad-high-cpu,payment-paused,currency-partitioned,catalog-one-product,combo-valkey-and-payment,combo-latency-and-cpu,payment-unreachable --as preetham --live --gap 150 > /tmp/iz-campaign.log 2>&1 &
caffeinate -dimsu -w $(pgrep -f 'practice campaign') &   # lid must stay open (or AC + external display)
# afterwards, the overfitting check:  practice campaign blind --as preetham --live --gap 150
```

Let the demo run ~10 min before the campaign so the Sentinel learns baselines (the campaign's first gap helps).
The server can now be restarted mid-campaign: the campaign retries its API calls for 2 min (restart it only between
scenarios anyway, so a scenario is not scored across a restart).

## Session 2 results (live mode, campaign auto-approving as test operator)

| Scenario | Score | What happened |
|---|---|---|
| homepage-flood | 3/10 | 80 users saturated the 8 GB host: entry rps FELL (13.6 -> 7.4), so traffic_spike never fired; latency everywhere -> product-catalog restarted. **Fixed after** (rule below); re-run pending |
| ad-gc-pressure | **10/10** | `adManualGc` off, recovery verified |
| cart-intermittent | 9/10 | `cartFailure` off (the restart problem is fixed), verified with the new op check; category said dependency_failure (logs say "can't connect to redis"). Prompt rule added after; re-run pending |
| shipping-crash-loop | 3/10 | killed every 20 s and started again by the simulator; model saw "uptime_s=3" and said false alarm; later kills suppressed. **Fixed after**; re-run pending |

Not part of a test: INC-0016 — `productCatalogFailure` was really on (Action API: "turned productCatalogFailure
off") right after the demo started; the agents fixed it. Probably left in the flag file from session 1.

10/10 so far over both sessions: payment-charge-fails, catalog-one-product, recommendation-cache-leak, ad-high-cpu
(session 1), ad-gc-pressure (session 2). Target: >= 19 faults tested, >= 10 at 10/10.

## Changes in session 2 (all tests pass: 92)

- Verifier: operation_errors incidents are checked over the time since the fix and need `op_verify_min_calls` (6)
  calls of the failing operation before "clear"; `op_verify_extra_s` (240) more verify time. (open issue 2)
- Planner prompt: a flag read in the failing code outweighs a past restart. Removed false memory INC-0003/0006/0011
  (backup was in the session scratchpad).
- Memory: `container_resources` adds `memory_baseline_percent` (30 min avg of `container_memory_percent_ratio`,
  offset baseline_s); "near limit" counts only >= baseline + 8 (or >= 97 without baseline). (open issue 3)
- Sentinel: `traffic_spike` on `load-generator` when Locust's `user_count` > traffic_factor x `normal_load_users`;
  diagnostician weights it 7 and the prompt names load-generator as the root cause.
- Simulator: a `load` fault always reverts to `normal_load_users` (it restored a leftover 31 users once).
- Restart loops: container fact parses `uptime_s`; a container_down/restarts alert + uptime < 120 s = container_down
  finding; a model "false alarm" is overridden by the rules when Docker shows a container fault
  (`_hard_container_fault`); a false alarm no longer suppresses container_down/restarts re-alerts.
- Diagnostician prompt: dependency_failure only when the dependency itself looks unhealthy.
- Campaign: API calls retry 2 min (server restart), settle timeout 420 -> 180 s.
- Tests: conftest points `loadgen_url` at an unreachable port (tests were reading the live Locust).

Open: the campaign's quiet check once waited the full settle timeout with nothing visibly failing (cause not found;
watch for "system was not quiet" in the campaign JSON notes). War Room tiles show ↻ forever after a Docker
auto-restart/OOM (offered: show it only while uptime < 5 min). opensearch restarted and load-generator was
OOM-flagged during the flood (host memory).

---

# Session 1 handoff (2026-10-24, ~02:00)

Read this first tomorrow. It records what was built, what the live tests showed, what is still open, and how to
resume. Results here come from the running demo; nothing in it is simulated.

## How to start again

Everything is **stopped**: all demo containers (`docker compose stop`, not removed), the agents and the Action API.

```bash
cd ~/Developer/bgsw/incident_zero
.venv/bin/incident_zero demo up       # starts the demo again (containers were stopped, not deleted)
.venv/bin/incident_zero action-api &  # the Action API (port 8701)
.venv/bin/incident_zero up &          # agents + Control Room on http://127.0.0.1:8700
.venv/bin/python -m pytest -q         # 91 tests, no Docker needed
```

Left clean: every flag off, no open incident, no active practice run. The server's mode was **live** (persisted in
`.incident_zero/state.json`); switch to dry run in the top bar if approvals should not execute. The load generator
was ramping down from the flood test when it was stopped; it starts at its normal 5 users.

## What was built in this session

| Area | What | Where |
|---|---|---|
| Knowledge for the Planner (RAG) | Code search over the demo's source (the line that raises the error, and the flag that guards it, found from the code), the handler of a failing operation, runbooks by symptom, incident memory of verified outcomes. Every action must cite knowledge verbatim. The hand-written flag lists were removed. | `knowledge/`, `agents/planner.py`, `runbook.yaml` |
| CPU detection | New Sentinel rule `resource_saturation`: a container ≥ 80 % of a core and ≥ 3x its baseline (rate of `container_cpu_usage_nanoseconds_total`). Replayed on 2 h of real data, it fired only during the ad high-CPU fault. A memory variant was rejected: the Go services swing between 55 and 98 % of their limits on their own. | `agents/sentinel.py`, `telemetry/queries.py` |
| Practice | Blind set (9 hidden scenarios, `practice run blind`), `requests` (real user traffic to a page), `load` (real traffic flood through Locust), campaign timing fixes | `practice/` |
| Control Room | Live War Room: stability %, container tiles, traffic and error charts (10/15/30 min, smooth one-per-minute curves, alert ▼ / fix ◆ / resolved ● markers, band = alert to verified recovery), live activity log with status dots, sparklines on Services; Approvals page folded into the War Room; Menu button removed | `ui/src/viz.tsx`, `server/overview.py` |
| Docs | ARCHITECTURE (one page), TEAM_FORM (to fill in), README, CHECKPOINTS, LIVE_RESULTS | `docs/` |

## Live results of this session (brief faults, live mode, test operator approving)

| # | Scenario | Score | What happened |
|---|---|---|---|
| 1 | payment-charge-fails | 10/10 (4 runs) | model diagnosis, `paymentFailure` off, cited from `src/payment/charge.js`, recovery verified |
| 2 | cart-intermittent | 6/10 | detected (EmptyCart 5 of 15 failed), right component, but a restart ran instead of `cartFailure` off — see open issue 2 |
| 3 | catalog-one-product | 10/10 | real visits to the product made the fault visible; `productCatalogFailure` off |
| 4 | recommendation-cache-leak | 10/10 | `recommendationCacheFailure` off + restart |
| 5 | ad-high-cpu | 0/10 → **10/10 after the fixes** | first run: no CPU rule existed (and flags had stopped reaching flagd); re-run INC-0010: new CPU rule fired (ad 396 % CPU), `adHighCpu` off chosen from ad's code, recovery verified |
| 6 | ad-gc-pressure | 0/10, **re-run not done** | first run: the flag never reached ad (flagd bug below), so the earlier "no measurable effect" conclusion is withdrawn. Scenario now also adds real ad traffic |
| 7 | homepage-flood | 0/10, **re-run stopped mid-way** | first run: the flood flag never reached the load generator (same flagd bug). Scenario now raises Locust to 80 users (expected fix: throttle back to 5). Stopped on request while it was running |

### Two bugs found tonight in the test harness (both fixed)

- **Flags stopped reaching flagd.** `actions/flags.py` wrote the flag file as temp-file + rename; through the Docker
  Desktop bind mount flagd lost its watch after a rename, so the file said `on` while flagd served `off`. Now written
  in place; verified live: flagd sees each change within 1 s, repeatedly. flagd was restarted once. Consequence: any
  flag fault or flag fix after roughly 00:50 did not take effect (ad-gc-pressure, homepage-flood first runs; the
  agents' own flag fixes in that window). Results before that (payment, catalog, recommendation) were real.
- **Campaign timing**: the detection window started before the settle wait (fixed), and the first scenario of a new
  campaign now also waits the gap. `--gap` is configurable: keep 320 s when consecutive scenarios hit the same
  service (the Sentinel holds same-service symptoms for 300 s after a resolution); 50–220 s is fine otherwise.

CP status: CP1 and CP3 met, CP2 met (payment, catalog, recommendation fully correct), CP0 needs the mentor, CP4 is
the organisers' run.

## Open issues, in priority order

0. **Finish tonight's re-run:** `incident_zero practice campaign homepage-flood,ad-gc-pressure --as <name> --live --gap 50`
   (about 15 min). Confirm the flood is detected (traffic_spike) and throttled back to 5, and whether heavy GC under
   real ad traffic trips the CPU rule (if it does, note that the extra traffic is part of the CPU rise).
1. **Finish the brief campaign** from scenario 8 (stopped there on request):
   `incident_zero practice campaign cart-stopped,shipping-crash-loop,valkey-down,catalog-db-down,catalog-latency,quote-packet-loss,checkout-cpu-starved,currency-memory-starved,combo-valkey-and-payment --as <name> --live`
   (about 1.5 h). Then the blind set: `practice campaign blind --as <name> --live` (about 1.5 h).
2. **Cart fix chose a restart.** The handler knowledge (K3: `EmptyCart` reads `cartFailure`) was retrieved, but the
   model followed incident memory instead: INC-0003 and INC-0006 were recorded as "restart recovered cart", both
   false (INC-0003: the fault was already reverted; INC-0006: the Verifier's 2-minute window saw no failures of a
   rare operation by chance). To do:
   - Verifier: for `operation_errors` alerts, check over `op_window` (5 m) and require enough calls before
     "recovered" (`agents/verifier.py`, `agents/sentinel.py` `evaluate(verify=True)`).
   - Planner prompt: a flag read in the failing code outweighs a past restart (`agents/planner.py` SYSTEM).
   - Remove the false entries: `.incident_zero/memory.jsonl` lines for INC-0003 and INC-0006.
   - Then re-run `cart-intermittent`.
3. **Natural checkout latency spikes are misdiagnosed as memory.** INC-0011 (checkout p95 1.34 s, no fault
   injected) was diagnosed "checkout memory at 87 % of its limit" and checkout was restarted (auto-approved by the
   campaign's test operator). 87 % is normal for checkout (it swings 55–98 %). Make "memory near the limit" evidence
   only when it is above the container's own baseline (diagnostician prompt and rules, `agents/diagnostician.py`),
   like the CPU rule does. Also: the campaign approves every incident that opens during a run, including natural ones.
4. **Fill in the deliverables:** `docs/TEAM_FORM.md`, first git commits by every member (the repo is initialised,
   nothing committed), `docs/LIVE_RESULTS.md` and the README status with the final numbers.
5. **Model:** `gemini-3.5-flash-lite` in `.env` is the smallest tier; consider a stronger one.
6. **Ask the organisers:** is reading the demo's service code to plan a fix allowed; the real Action API and the
   official report schema.
7. Files moved out of the project on day 1 (old state, caches, duplicate brief, old Approvals page) are in
   `/private/tmp/claude-501/-Users-preet-Developer-bgsw/579f62a5-674d-499a-8f34-0b5f79a9a2ad/scratchpad/removed/`
   (a temporary folder: it may be gone after a reboot; nothing in it is needed).

## Things learned that are easy to forget

- Span metrics arrive about once a minute: use `rate(...[2m])` (the Sentinel's window); `[1m]` is empty.
- `container_cpu_utilization_ratio` is a slow running average; use the rate of `container_cpu_usage_nanoseconds_total`.
- Natural checkout latency spikes (~1.3 s) happen with no fault; they open real incidents (e.g. INC-0009).
- The campaign must not have the server restarted while it polls; restart only during its "waiting Ns" sleep.
- To check that a flag really reached the services: `curl -s -X POST http://127.0.0.1:8080/flagservice/flagd.evaluation.v1.Service/ResolveBoolean -H 'Content-Type: application/json' -d '{"flagKey":"adHighCpu","context":{}}'` (operator check only; the agents never read flags).
- Headless Chrome `--timeout` freezes network requests; for screenshots use the CDP script (`shot.mjs` in the
  scratchpad) or a real browser.
