import { useEffect } from "react";
import { NavLink, Outlet, useLocation, useOutletContext, useParams } from "react-router";
import { Decision, EventRow, Markdown, PhaseLabel, Pipeline, PlanActions } from "../components";
import { useLiveData } from "../live";
import type { Event, Evidence, Incident } from "../types";
import { clock, duration } from "../util";
import { Causes } from "./WarRoom";

type Ctx = { inc: Incident; reload: () => void };
const useInc = () => useOutletContext<Ctx>();

export default function IncidentPage() {
  const { id } = useParams();
  const { data: inc, error, reload } = useLiveData<Incident>(`/api/incidents/${id}`);
  if (error) return <div className="card empty"><h2>{id}</h2><p>{error}</p></div>;
  if (!inc) return null;
  const tabs: [string, string][] = [
    ["timeline", "Timeline"], ["evidence", "Evidence"], ["diagnosis", "Diagnosis"],
    ["plan", inc.phase === "awaiting_approval" ? "Plan · decide" : "Plan"], ["report", "Report"],
  ];
  return (
    <div>
      <div className="page-head">
        <div>
          <div className="row">
            <span className="mono faint">{inc.id}</span>
            <PhaseLabel phase={inc.phase} />
          </div>
          <h1 style={{ marginTop: 6 }}>{inc.title}</h1>
          <p className="sub">
            Opened {clock(inc.created_at)} · {inc.resolved_at ? `resolved in ${duration(inc.created_at, inc.resolved_at)}` : `${duration(inc.created_at, inc.phase === "awaiting_approval" ? null : inc.updated_at)}`}
            {" · "}{inc.alerts.length} alert(s) · model {inc.llm || "-"}
            {" · "}{inc.tokens.calls} model call(s), {inc.tokens.prompt_tokens + inc.tokens.completion_tokens} tokens{inc.tokens.estimated ? " (estimated)" : ""}
          </p>
          {inc.recall && (
            <p className={`small ${inc.recall.status === "abandoned" ? "t-warn" : "t-info"}`} style={{ marginTop: 4 }}>
              {inc.recall.status === "abandoned"
                ? <>Known-incident recall of {inc.recall.case} abandoned: {inc.recall.reason}. Reasoned with the model.</>
                : <>Known incident: a repeat of {inc.recall.case} ({inc.recall.component}, {inc.recall.category}) · {inc.recall.track_record} · answered from the casebook with no model calls{inc.recall.tokens_saved ? ` · about ${inc.recall.tokens_saved.toLocaleString()} tokens saved` : ""}{inc.recall.status === "matched" ? " (confirming)" : ""}</>}
            </p>
          )}
        </div>
      </div>
      <div style={{ marginBottom: 24 }}><Pipeline phase={inc.phase} /></div>
      <nav className="tabs">
        {tabs.map(([to, label]) => (
          <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>{label}</NavLink>
        ))}
      </nav>
      <Outlet context={{ inc, reload } satisfies Ctx} />
    </div>
  );
}

export function TimelineTab() {
  const { inc } = useInc();
  const { data } = useLiveData<Event[]>(`/api/incidents/${inc.id}/events`);
  return (
    <div className="list">
      {(data ?? []).map((e) => <EventRow key={e.seq} ev={e} />)}
      {data && !data.length && <div className="item muted">No events yet.</div>}
    </div>
  );
}

export function EvidenceTab() {
  const { inc } = useInc();
  const { data } = useLiveData<Evidence[]>(`/api/incidents/${inc.id}/evidence`);
  const { hash } = useLocation();
  const cited = new Set((inc.diagnosis?.root_causes ?? []).flatMap((r) => r.claims.map((c) => c.evidence_id)));
  useEffect(() => {
    if (hash && data) document.getElementById(hash.slice(1))?.scrollIntoView({ block: "center" });
  }, [hash, data]);
  return (
    <div className="stack-s">
      <p className="small muted">Every tool result, redacted, in the order it was collected. Claims in the diagnosis must quote these verbatim.</p>
      <div className="list">
        {(data ?? []).map((e) => (
          <details key={e.id} id={e.id} className="item" open={hash === `#${e.id}`}>
            <summary>
              <div className="item-row">
                <span className="eid" style={{ width: 36 }}>{e.id}</span>
                <span className="tag">{e.source}</span>
                <span className="grow">{e.summary}</span>
                {cited.has(e.id) && <span className="small t-info">cited</span>}
                <span className="chev">›</span>
              </div>
            </summary>
            <div className="expand stack-s">
              <div className="small muted mono">{e.tool}({Object.entries(e.args).filter(([, v]) => v !== null).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ")}) · {clock(e.created_at)}</div>
              <pre>{e.content}</pre>
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}

export function DiagnosisTab() {
  const { inc } = useInc();
  const d = inc.diagnosis;
  return (
    <div className="grid-2">
      <section className="card stack">
        {d ? (
          <>
            <div>
              <h3>Diagnosis</h3>
              <p style={{ marginTop: 8 }}>{d.summary}</p>
              <p className="small faint" style={{ marginTop: 6 }}>produced by {d.produced_by === "llm" ? "the model" : "rules over the evidence"} · explains {d.explained_alerts.join(", ") || "-"}</p>
              {d.produced_by === "rules" && (
                <p className="small t-warn" style={{ marginTop: 6 }}>
                  Backup path: this diagnosis comes from fixed rules, not the model (the model was unavailable or its
                  diagnosis failed the evidence audit). Rules only know the failure kinds they were written for.
                </p>
              )}
            </div>
            <Causes inc={inc} />
          </>
        ) : <p className="muted">No diagnosis yet.</p>}
        {inc.limitations.length > 0 && (
          <div className="section">
            <h3>Limitations</h3>
            <ul style={{ margin: 0, paddingLeft: 18 }}>{inc.limitations.map((l, i) => <li key={i} className="muted">{l}</li>)}</ul>
          </div>
        )}
      </section>
      <section className="section">
        <h3>Evidence audit</h3>
        <div className="list">
          {inc.audits.map((a, i) => (
            <div key={i} className="item">
              <div className="item-row">
                <span className={`dot ${a.passed ? "ok" : "crit"}`} />
                <span className="grow">{a.passed ? "Passed" : "Rejected"} · {a.checked_claims} claims checked</span>
                <span className="ts">{clock(a.created_at)}</span>
              </div>
              {a.problems.map((p, j) => <p key={j} className="small muted" style={{ marginTop: 4, paddingLeft: 19 }}>{p}</p>)}
            </div>
          ))}
          {!inc.audits.length && <div className="item muted">Not audited yet.</div>}
        </div>
        {inc.human_notes.length > 0 && (
          <>
            <h3 style={{ marginTop: 8 }}>Sent back by a human</h3>
            <div className="list">{inc.human_notes.map((n, i) => <div key={i} className="item">{n}</div>)}</div>
          </>
        )}
      </section>
    </div>
  );
}

export function PlanTab() {
  const { inc, reload } = useInc();
  const ex = inc.execution;
  const v = inc.verification;
  return (
    <div className="grid-2">
      <section className="card stack">
        <div className="spread">
          <h3>Proposed plan</h3>
          {inc.plan && <span className="mono faint small" title="plan hash">#{inc.plan.change_hash.slice(0, 10)}</span>}
        </div>
        <PlanActions inc={inc} />
        {inc.plan && <p className="small faint">chosen by {inc.plan.produced_by === "llm" ? "the model" : inc.plan.produced_by === "memory" ? `the casebook (the verified fix of ${inc.recall?.case ?? "a known incident"})` : "rules"} from the retrieved knowledge; every citation is checked verbatim by code</p>}
        <Decision inc={inc} onDone={reload} />
        {inc.approval.decision !== "pending" && inc.approval.decision !== "not_requested" && (
          <p className="small muted">
            {inc.approval.decision.replace("_", " ")} by {inc.approval.approver ?? "-"} at {inc.approval.timestamp ? clock(inc.approval.timestamp) : "-"}
            {inc.approval.note && ` · “${inc.approval.note}”`}
          </p>
        )}
      </section>
      <section className="stack">
        <div className="section">
          <h3>Knowledge retrieved</h3>
          <p className="small muted">What the Planner looked up before choosing: the code raising the error and the flags guarding it, runbooks for the symptom, and past incidents' verified outcomes.</p>
          <div className="list">
            {(inc.knowledge ?? []).map((k) => (
              <details key={k.id} className="item">
                <summary>
                  <div className="item-row">
                    <span className="eid" style={{ width: 36 }}>{k.id}</span>
                    <span className="tag">{k.source}</span>
                    <span className="grow">{k.summary}</span>
                    <span className="chev">›</span>
                  </div>
                </summary>
                <div className="expand stack-s">
                  <div className="small muted mono">{k.ref}</div>
                  <pre>{k.content}</pre>
                </div>
              </details>
            ))}
            {!(inc.knowledge ?? []).length && <div className="item muted small">Nothing retrieved yet.</div>}
          </div>
        </div>
        <div className="section">
          <h3>Execution</h3>
          <div className="list">
            {ex.results.map((r, i) => (
              <div key={i} className="item small">
                <div className="item-row">
                  <span className={`dot ${r.ok ? "ok" : "crit"}`} />
                  <span className="grow"><code>{r.action}</code> {r.dry_run && <span className="tag">dry run</span>} {r.message}</span>
                  <span className="ts">{clock(r.at)}</span>
                </div>
              </div>
            ))}
            {ex.rollback_results.map((r, i) => (
              <div key={`rb${i}`} className="item small">
                <div className="item-row"><span className="dot warn" /><span className="grow">rollback: {r.message}</span></div>
              </div>
            ))}
            {!ex.results.length && <div className="item muted small">Nothing executed. Every action goes through the Action API after approval.</div>}
          </div>
        </div>
        <div className="section">
          <h3>Verification</h3>
          <div className="card tight small stack-s">
            <p>{v.checked ? (v.recovered ? <span className="t-ok">Recovered</span> : <span className="t-crit">Not recovered</span>) : <span className="muted">Not checked</span>}{v.notes && <span className="muted"> · {v.notes}</span>}</p>
            {v.signals_checked.length > 0 && <p className="muted">Signals: {v.signals_checked.join("; ")}</p>}
          </div>
        </div>
      </section>
    </div>
  );
}

export function ReportTab() {
  const { inc } = useInc();
  const { data } = useLiveData<{ json: unknown; markdown: string | null }>(`/api/incidents/${inc.id}/report`);
  if (!data?.markdown) return <div className="card empty"><h2>No report yet</h2><p>The Reporter writes it when the incident closes.</p></div>;
  const blob = (s: string, type: string) => URL.createObjectURL(new Blob([s], { type }));
  return (
    <div className="stack">
      <div className="row">
        <a className="btn small" download={`${inc.id}.json`} href={blob(JSON.stringify(data.json, null, 2), "application/json")}>Download JSON</a>
        <a className="btn small quiet" download={`${inc.id}.md`} href={blob(data.markdown, "text/markdown")}>Download Markdown</a>
      </div>
      <article className="card"><Markdown text={data.markdown} /></article>
    </div>
  );
}
