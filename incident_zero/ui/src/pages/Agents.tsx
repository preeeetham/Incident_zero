import { Link } from "react-router";
import { EventRow } from "../components";
import { useLive, useLiveData } from "../live";
import type { AgentRow, BrainStats } from "../types";
import { ago } from "../util";

const ROLE: Record<string, [string, string]> = {
  sentinel: ["Detects", "Alert rules on error rate, latency, traffic and container health; opens incidents"],
  investigator: ["Investigates", "Chooses the next telemetry tool from what it has found; records evidence"],
  diagnostician: ["Diagnoses", "Names the root cause with a confidence and quoted evidence"],
  auditor: ["Audits", "Rejects citations that are not in the evidence; checks every alert is explained"],
  planner: ["Plans", "Chooses allowed Action API actions; previews them as a dry run"],
  approval: ["Asks", "Holds the plan for a human; escalates on timeout"],
  executor: ["Acts", "Executes the approved plan through the Action API only"],
  verifier: ["Verifies", "Re-checks the triggering signals; rolls back and escalates on failure"],
  reporter: ["Reports", "Writes the incident report in the jury's schema"],
};

const LAYERS: Record<AgentRow["layer"], [string, string]> = {
  eyes: ["Eyes", "Perceive and diagnose. They watch, collect evidence and name the root cause; they never change anything."],
  hands: ["Hands", "Act, only through guarded steps: a validated plan, a hash-bound human approval, the Action API."],
};

function AgentList({ rows, offset }: { rows: AgentRow[]; offset: number }) {
  return (
    <div className="list">
      {rows.map((a, i) => (
        <details key={a.agent} className="item">
          <summary>
            <div className="item-row">
              <span className="faint mono" style={{ width: 18 }}>{offset + i + 1}</span>
              <span className={`dot ${a.state === "working" ? "info pulse" : ""}`} />
              <span style={{ width: 120, fontWeight: 500, textTransform: "capitalize" }}>{a.agent}</span>
              <span className="grow muted">
                {a.state === "working"
                  ? <>{a.doing}{a.incident_id && <> · <Link className="t-info" to={`/incidents/${a.incident_id}`}>{a.incident_id}</Link></>}</>
                  : ROLE[a.agent]?.[1]}
              </span>
              <span className="small faint hide-sm">{a.runs} runs · {ago(a.since)}</span>
              <span className="chev">›</span>
            </div>
          </summary>
          <div className="expand">
            {a.recent.length ? <div className="list">{[...a.recent].reverse().map((e) => <EventRow key={e.seq} ev={e} showIncident />)}</div>
              : <p className="muted small">No recent activity.</p>}
          </div>
        </details>
      ))}
    </div>
  );
}

function Layer({ layer, rows, offset }: { layer: AgentRow["layer"]; rows: AgentRow[]; offset: number }) {
  return (
    <section className="stack-s">
      <h3>{LAYERS[layer][0]}</h3>
      <p className="small muted">{LAYERS[layer][1]}</p>
      <AgentList rows={rows} offset={offset} />
    </section>
  );
}

function Brain({ b }: { b: BrainStats | null | undefined }) {
  const num = (n: number | undefined) => (n ?? 0).toLocaleString();
  return (
    <section className="card stack-s">
      <div className="spread">
        <h3>Brain</h3>
        <span className="small faint">{b?.model ?? "-"}</span>
      </div>
      <p className="small muted">The model, the knowledge the Planner cites, and the casebook of every closed incident. A repeat of a solved incident is answered from the casebook with no model calls{b && !b.recall_enabled ? " (recall is switched off)" : ""}.</p>
      <div className="item-row small">
        <span><b>{num(b?.incidents)}</b> <span className="muted">incidents remembered</span></span>
        <span><b>{num(b?.known_fixes)}</b> <span className="muted">known fixes</span></span>
        <span><b>{num(b?.recalled)}</b> <span className="muted">answered from memory</span></span>
        <span><b>{num(b?.tokens_used)}</b> <span className="muted">tokens used ({num(b?.model_calls)} calls)</span></span>
        <span className="t-info"><b>{num(b?.tokens_saved)}</b> tokens saved</span>
      </div>
      {!!b?.cases.length && (
        <div className="list">
          {b.cases.slice(0, 6).map((c) => (
            <div key={c.incident} className="item item-row small">
              <Link className="mono t-info" to={`/incidents/${c.incident}`} style={{ width: 80 }}>{c.incident}</Link>
              <span style={{ width: 120 }}>{c.outcome.replace(/_/g, " ")}</span>
              <span className="grow muted">{c.components.join(", ") || "-"}{c.categories.length ? ` (${c.categories.join(", ")})` : ""}</span>
              <span className="faint">{c.mode === "recalled" ? `recalled from ${c.recall_of} · ${num(c.tokens_saved)} saved` : `${c.tokens.calls} calls · ${num(c.tokens.total)} tokens`}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Agents() {
  const { data } = useLiveData<AgentRow[]>("/api/agents");
  const { data: brain } = useLiveData<BrainStats>("/api/brain");
  const { state } = useLive();
  const eyes = (data ?? []).filter((r) => r.layer === "eyes");
  const hands = (data ?? []).filter((r) => r.layer === "hands");
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Agents</h1>
          <p className="sub">{state?.llm ? `Model ${state.llm}` : "No model: deterministic playbooks"} · three layers: Eyes, Brain, Hands · four agents use the model, five are plain code</p>
        </div>
      </div>
      <Layer layer="eyes" rows={eyes} offset={0} />
      <Brain b={brain} />
      <Layer layer="hands" rows={hands} offset={eyes.length} />
    </div>
  );
}
