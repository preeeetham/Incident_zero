# Incident Zero — Requirements

Incident Zero is an AI on-call engineer for the OpenTelemetry Demo ("Astronomy Shop"). It detects a production
incident, investigates it with real telemetry tools, names the root cause with evidence that exists in the collected
data, proposes a fix from an allowed runbook (the Action API), waits for a human to approve it, executes it through
the Action API only, verifies recovery, rolls back and escalates when recovery fails, and writes an incident report.

It is one self-contained package (`incident_zero/`). It began next to an earlier chaos-engineering project ("Break
Point"); the few modules it borrowed from it (LLM client, LangChain model, Docker controller, compose parser) now
live inside the package (BUILD_LOG, "Consolidation").

Source of the requirements: the hackathon brief "Incident Zero: Build an AI On-Call Engineer" (PES University, one
day), and the Control Room decisions agreed with the project owner on 2026-10-23.

---

## 1. Functional requirements (the brief)

| ID | Requirement | Brief step |
|---|---|---|
| F1.1 | Receive an alert: an error-rate or latency threshold on the demo's metrics (also: container down, traffic spike, telemetry gone silent) | 1 Detect |
| F1.2 | Start an investigation automatically, with no human trigger | 1 Detect |
| F1.3 | Alerts that start close together or share a dependency path are correlated into **one** incident | 1 Detect |
| F2.1 | Query traces (Jaeger), metrics (Prometheus), logs (OpenSearch, falling back to container logs) and container health (Docker) | 2 Investigate |
| F2.2 | Choose the next tool from the findings so far (an agent tool loop), never dump all data into one prompt | 2 Investigate |
| F2.3 | Every tool result is stored as evidence with an ID (`E1`, `E2`, ...) before a model sees it | 2 Investigate |
| F3.1 | Name the failing component(s) and the root cause(s); a combination incident may have several | 3 Diagnose |
| F3.2 | Give a confidence (0–1) and the exact traces, log lines or metric values that support the diagnosis | 3 Diagnose |
| F3.3 | Every cited evidence ID must exist and every quoted value must appear verbatim in that evidence, checked by code; failures go back to investigation | 3 Diagnose |
| F3.4 | The diagnosis must explain every alerting symptom; unexplained symptoms send the case back to look for another cause | 3 Diagnose |
| F4.1 | Choose only from the actions the Action API exposes; parameters are validated by code | 4 Propose |
| F4.2 | Give a short reason for the proposed action | 4 Propose |
| F4.3 | Dry-run mode (on by default) and a kill switch | 4 Propose |
| F5.1 | Show the diagnosis, evidence and proposed plan to a human | 5 Approve |
| F5.2 | No change without explicit approval; the approval is bound to a hash of the exact plan shown | 5 Approve |
| F5.3 | The human can approve, reject, or send back to investigation with a note | 5 Approve |
| F5.4 | No response within the timeout → the incident is escalated; nothing runs | 5 Approve |
| F6.1 | Execute the approved action through the Action API only (the agent process has no Docker write access path) | 6 Act |
| F6.2 | Check health signals (the ones that triggered the alert) to confirm recovery | 6 Verify |
| F6.3 | If recovery fails: roll back through the Action API and escalate | 6 Verify |
| F7.1 | Concise incident report: timeline, root cause, evidence, action taken, verification, prevention, limitations | 7 Report |
| F7.2 | The report validates against the jury's JSON schema (`approval.required` and `execution.action_api_only` are always `true`, no extra properties) | 7 Report |

## 2. Integrity rules (the jury's "reason from telemetry" rule)

| ID | Rule |
|---|---|
| R1 | The agent never reads feature-flag settings: no access to flagd, its config file or flagd-ui |
| R2 | Every `feature_flag.*` attribute, span event and log field is removed from telemetry before it becomes evidence (the demo's OpenFeature hooks write flag values into traces) |
| R3 | The agent never reads practice ground truth |
| R4 | R1–R3 are enforced by tests, not by prompts |
| R5 | Invented evidence is impossible to submit: the Evidence Auditor rejects it (F3.3) |

## 3. Safety requirements

| ID | Requirement |
|---|---|
| S1 | Dry run is on at start; the mode is always visible; turning it off is a deliberate action |
| S2 | "Stop all" (kill switch) freezes every agent, blocks every Action API call, rolls back actions executed in the last N minutes, and needs an explicit resume |
| S3 | The kill switch is checked before every Action API call |
| S4 | One action runs at a time per service |
| S5 | Model output is never executed directly: actions, parameters and evidence citations are validated by code |

## 4. Control Room requirements (agreed 2026-10-23)

| ID | Decision |
|---|---|
| C1 | Its own Control Room: a React single-page app with routes |
| C2 | `/` is the live war room: the active incident, agent progress, current hypothesis, an approval card when a decision is due |
| C3 | Routes: `/incidents`, `/incidents/:id` (+ `/timeline`, `/evidence`, `/diagnosis`, `/plan`, `/report`), `/approvals`, `/agents`, `/services`, `/practice` |
| C4 | Approval actions: approve / reject / send back with a note |
| C5 | Timeline: one readable line per agent step, expandable to the exact query, raw result and evidence ID |
| C6 | Visual style: dark ops console, **minimal**: no neon, no clutter, no nested scroll regions (one page scroll), muted colour with status colour only where it carries meaning, one primary action per screen |
| C7 | Slim top bar: name, routes, approvals badge, dry-run/live mode, "Stop all" |
| C8 | Approver enters a name once per session; it is stamped on every decision with a timestamp |
| C9 | Approval waiting is signalled by the top-bar badge, a browser notification, and email (when SMTP is configured) |
| C10 | `/practice` exists in the same app but is switched off entirely in eval mode (`--eval`): no route, no API, no Simulator |
| C11 | Live updates without refreshing (server-sent events) |

## 5. Practice requirements

| ID | Requirement |
|---|---|
| P1 | A Simulator injects practice incidents: the demo's application faults (feature flags), infrastructure faults (stop, crash loop, CPU/memory starvation, network delay/loss, datastore down) and combinations |
| P2 | Ground truth is stored where the agents cannot read it (R3) |
| P3 | A Judge scores each run: component, root cause category, evidence validity, action allowed and effective, time to diagnosis |
| P4 | A hold-out set: scenarios used for tuning are separate from scenarios used for scoring |

## 6. Non-functional requirements

| ID | Requirement |
|---|---|
| N1 | Runs on its own: `incident_zero up` starts the Sentinel, agents, server and Control Room; nothing else is needed besides the demo and the Action API |
| N2 | Durable: incident state is written to disk after every phase; a restart resumes open incidents (including pending approvals) |
| N3 | Works without an LLM (deterministic investigation playbook and rule-based diagnosis), so the pipeline never stalls on a rate limit; the LLM path is preferred when available |
| N4 | Extensible: a new telemetry backend, tool, action backend or agent is one adapter or one module, without changing the others |
| N5 | Configurable endpoints, discovered from the running Compose project when not set |
| N6 | Tested: unit tests for every agent and tool with fakes; the integrity rules R1–R4 as tests |

## 7. What the full brief says (docs/hackathon_brief.json)

| Topic | Brief | How Incident Zero handles it |
|---|---|---|
| Action API | Only way to change the system: restart a service, toggle a feature flag, throttle the load generator, scale a service (Kubernetes only); every call logged | The mock serves exactly these four (`restart_service`, `toggle_feature_flag`, `throttle_load_generator`, `scale_service`); scale is refused on Compose. Parameter names are our guess until the team's API is seen; the client is one adapter |
| Report format | "required output format (JSON schema)" in the template repository; the brief extraction's notes say the official schema was not legible | **Ours, defined from what the agent produces**: `schema/incident_report.schema.json` (status, summary, timeline, diagnosis with confidence and alternatives, evidence with source/observation/reference/supports, proposed action, approval, execution, verification, prevention, limitations). Every report is validated against it; `report_schema_file` swaps in another schema without code changes |
| faultctl | The organisers' tool injects practice incidents and resets | Our Simulator does the same for local practice; on the day faultctl replaces it. `incident_id` can carry faultctl's id (`external_id`) |
| Faults with no allowed fix | CPU/memory starvation by limit, a detached network: the four actions cannot change limits or networks | The Planner proposes nothing and the incident escalates with the reason; the Judge scores escalation as correct for these |
| Flags | Reading flag settings to diagnose = disqualification | Diagnosis never sees flag data (R1, R2). The Planner knows which flags guard each service's code from the demo source (`runbook.yaml`) and may propose toggling them off; to confirm with the organisers |
| Deliverables | Source + README (setup, run, approval gate, kill switch, limitations), one-page architecture, incident reports (schema + readable), evaluation note, team form | README and BUILD_LOG cover the first two; reports come out as JSON + Markdown; the practice Judge produces the evaluation note's data |
| **Rule: "All code is written on the day, from 09:15; commits before then are not allowed."** | | **This package cannot be submitted as written before the event.** It is the rehearsal and blueprint: architecture, prompts, safety design, practice harness and lessons, to be rebuilt on the day (AI coding assistants are allowed if declared) |

## 8. Assumptions and open items

| Item | Assumption until resolved |
|---|---|
| Action API wire format and parameter names | Mock: `GET /actions`, `POST /actions/execute` (with `dry_run`), `POST /actions/rollback`; parameters `service`, `flag`+`enabled`, `users`, `service`+`replicas` |
| Whether toggling a flag the agent did not read is acceptable | Assumed yes (it is a write through the Action API); delete `service_flags` in runbook.yaml if not |
| Team size (5 vs "all three members must commit") and code freeze (16:30) | To confirm with the organisers |
| Kafka group (`compose.full.yaml`) | Optional: the default local stack is `compose.yaml` + `compose.observability.yaml` to fit 8 GB of Docker memory |
