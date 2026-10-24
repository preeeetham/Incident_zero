import { Link } from "react-router";
import { EventRow } from "../components";
import { useLive, useLiveData } from "../live";
import type { AgentRow } from "../types";
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

export default function Agents() {
  const { data } = useLiveData<AgentRow[]>("/api/agents");
  const { state } = useLive();
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Agents</h1>
          <p className="sub">{state?.llm ? `Model ${state.llm}` : "No model: deterministic playbooks"} · four agents use the model, five are plain code</p>
        </div>
      </div>
      <div className="list">
        {(data ?? []).map((a, i) => (
          <details key={a.agent} className="item">
            <summary>
              <div className="item-row">
                <span className="faint mono" style={{ width: 18 }}>{i + 1}</span>
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
    </div>
  );
}
