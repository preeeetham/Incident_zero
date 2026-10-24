# Incident Zero — Architecture (one page)

![Incident Zero architecture](architecture.svg)

Full diagram: [`architecture.svg`](architecture.svg) (agents, telemetry, knowledge, Action API, safety controls, thresholds).

```
 OpenTelemetry Demo (28 containers, Docker Compose)
   │ traces ─► Jaeger      metrics ─► Prometheus      logs ─► OpenSearch      state ─► Docker (read only)
   ▼
 ┌──────────── agent process (incident_zero up) ─────────────────────────────────────────────────────┐
 │ 1 Sentinel ─ alert rules on span metrics + container state ─► opens / correlates an Incident        │
 │ 2 Investigator ─ model picks the next telemetry tool (LangGraph loop); every result = evidence E<n> │
 │ 3 Diagnostician ─ component, cause, confidence; claims quote evidence verbatim                      │
 │ 4 Evidence Auditor (code) ─ ids exist, quotes verbatim, every alert explained ─ else back to 2      │
 │ 5 Remediation Planner ─ retrieves knowledge K<n>, chooses allowed actions, cites K<n> verbatim      │
 │      ├ code search over the demo's source: error line → the flag that guards it                     │
 │      ├ runbooks by symptom (knowledge/runbooks/*.md)                                                │
 │      └ incident memory: past fixes the Verifier judged (data_dir/memory.jsonl)                      │
 │ 6 Approval Gate ─ human approves the hashed plan (Control Room / CLI / email); timeout escalates     │
 │ 7 Executor ─ HTTP calls to the Action API only; kill switch checked before every call               │
 │ 8 Verifier ─ re-checks the triggering signals; not recovered → rollback + escalate                  │
 │ 9 Reporter ─ JSON (schema-validated) + Markdown report; outcome written to incident memory          │
 └───────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                             │ HTTP (the agent process holds no Docker write client)
                                  Action API (separate process) ─► restart / toggle flag / throttle load
```

## Components and tools

| Layer | What | Where |
|---|---|---|
| Telemetry tools (Investigator) | `overview`, `service_metrics`, `find_traces`, `trace_detail`, `search_logs`, `container_status`, `container_resources`, `dependencies`, `datastore_metrics`, `promql` | `telemetry/tools.py` |
| Knowledge (Planner) | code search (`knowledge/code_index.py`), runbooks (`knowledge/runbooks.py`), memory (`knowledge/memory.py`), BM25 ranking (`knowledge/search.py`) | `knowledge/` |
| Allowed actions | `restart_service`, `toggle_feature_flag`, `throttle_load_generator`, `scale_service` (Kubernetes only) | Action API, `actions/` |
| Model | any OpenAI-compatible endpoint, Anthropic or Ollama (`.env`); without one every agent has a deterministic path | `llm*.py` |
| Control Room | React UI: war room, incidents, evidence, knowledge, plan + approval, report, practice | `ui/`, `server/http.py` |
| Practice | fault injector (flags, stop, pause, crash loop, CPU/memory limits, latency/loss, disconnect, user traffic), hidden truth, Judge, blind set | `practice/` |

## Decision process

1. **Detect**: a signal must hold for 2 ticks (15 s each) against a baseline learned from Prometheus.
2. **Investigate → diagnose → audit**, up to 3 rounds. The audit is code. If the model's diagnosis fails it twice,
   rules over the same evidence take over. The UI marks this "backup rules".
3. **Plan.** Telemetry says *what* is broken; knowledge says *how to fix it*. The error text from the audited evidence
   is searched in the demo's source. The flag condition that guards the raising line is a fix candidate, and the
   runbooks for the symptom say what else applies (or that nothing allowed does: escalate). Past verified outcomes
   give a track record. Every action must cite a `K` item verbatim, or code drops it.
4. **Approve, act, verify.** Correctness is proven only after the fact. The Verifier requires the triggering signals to
   be clear for 2 consecutive checks; otherwise it rolls back and escalates. The outcome goes to memory.

## Safety controls

- **Dry run** at start; going live is a deliberate switch. **Kill switch** (UI, API, CLI or STOP file): the Action
  API refuses everything, recent reversible actions roll back, and agents freeze.
- **Approval** is bound to a SHA-256 hash of the exact plan shown (actions, parameters, reasons, citations).
- **Action API only**: the agent process has an HTTP client, not a Docker write client (enforced by an import test).
- **No invented evidence or reasons**: quotes are checked verbatim for diagnosis claims (`E` ids) and plan citations
  (`K` ids).
- **No flag settings**: flag data is redacted from all telemetry. The flagd file, the flagd logs and `src/flagd*`
  are never read. Knowledge about flags comes from service code and is used only to plan, never to diagnose.
