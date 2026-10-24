import { useState } from "react";
import { Link } from "react-router";
import { post } from "../api";
import { useLive, useLiveData } from "../live";
import type { PracticeRun, Scenario } from "../types";
import { ago } from "../util";

export default function Practice() {
  const { state } = useLive();
  const scen = useLiveData<Scenario[]>(state && !state.eval_mode ? "/api/practice/scenarios" : null);
  const runs = useLiveData<PracticeRun[]>(state && !state.eval_mode ? "/api/practice/runs" : null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (state?.eval_mode) return <div className="card empty"><h2>Not available</h2><p>Practice is switched off in evaluation mode.</p></div>;
  const active = (runs.data ?? []).find((r) => r.status === "active");
  const act = async (path: string, body: unknown) => {
    setBusy(true);
    setErr(null);
    try {
      await post(path, body);
      runs.reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const scored = (runs.data ?? []).filter((r) => r.score);
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Practice</h1>
          <p className="sub">Inject a known fault, let the agents work it, then score them against the hidden ground truth.
            Listed: the 10 faults verified live at 10/10. Open a scenario to see what it injects.</p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      {active && (
        <section className="card spread">
          <div>
            <div className="row"><span className="dot warn pulse" /><strong>{active.title}</strong></div>
            <p className="small muted" style={{ marginTop: 4 }}>started {ago(active.started_at)} · reverts automatically when its incidents close</p>
          </div>
          <button className="btn" disabled={busy} onClick={() => act(`/api/practice/runs/${active.id}/stop`, {})}>Revert and score</button>
        </section>
      )}
      <div className="grid-2">
        <section className="section">
          <h3>Scenarios</h3>
          <div className="list">
            {(scen.data ?? []).map((s) => (
              <details key={s.id} className="item">
                <summary>
                  <div className="item-row">
                    <span className="chev">›</span>
                    <span className="grow">{s.title}<span className="faint small"> · {s.faults.join(" + ")}</span></span>
                    {/* preventDefault: clicking Run must not open or close the description */}
                    <button className="btn small" disabled={busy || !!active}
                      onClick={(e) => { e.preventDefault(); act("/api/practice/run", { scenario_id: s.id }); }}>Run</button>
                  </div>
                </summary>
                <div className="expand small stack-s">
                  <p><strong>Injects</strong> · {s.injects || "-"}</p>
                  <p><strong>Effect</strong> · {s.effect || "-"}</p>
                  <p className="faint">The right fix stays hidden until the run is scored.</p>
                </div>
              </details>
            ))}
          </div>
        </section>
        <section className="section">
          <h3>Results</h3>
          <div className="list">
            {scored.map((r) => {
              const d = r.score!.detail as Record<string, unknown>;
              return (
                <details key={r.id} className="item">
                  <summary>
                    <div className="item-row">
                      <span className="grow">{r.title}</span>
                      <span className={`mono ${r.score!.points >= 8 ? "t-ok" : r.score!.points >= 5 ? "t-warn" : "t-crit"}`}>{r.score!.points}/10</span>
                      <span className="chev">›</span>
                    </div>
                  </summary>
                  <div className="expand small stack-s">
                    <p>Truth: {r.truth?.components.join(", ")} ({r.truth?.categories.join(" / ")})</p>
                    <p>Named: {(d.components_named as string[] | undefined)?.join(", ") || "-"} · actions {(d.actions as string[] | undefined)?.join(", ") || "-"}</p>
                    <p className="muted">detected {String(d.detected)} · audit {String(d.evidence_passed_audit ?? "-")} · recovered {String(d.recovered ?? "-")}
                      {d.time_to_diagnosis_s !== undefined && ` · diagnosed in ${d.time_to_diagnosis_s}s`}</p>
                    <p>{r.score!.incidents.map((id) => <Link key={id} className="t-info" to={`/incidents/${id}`} style={{ marginRight: 8 }}>{id}</Link>)}</p>
                  </div>
                </details>
              );
            })}
            {!scored.length && <div className="item muted small">No scored runs yet.</div>}
          </div>
        </section>
      </div>
    </div>
  );
}
