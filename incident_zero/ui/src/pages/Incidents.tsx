import { useState } from "react";
import { useNavigate } from "react-router";
import { PhaseLabel } from "../components";
import { useLiveData } from "../live";
import type { IncidentSummary } from "../types";
import { ago, isOpen } from "../util";

export default function Incidents() {
  const { data, error } = useLiveData<IncidentSummary[]>("/api/incidents");
  const [filter, setFilter] = useState<"all" | "open">("all");
  const nav = useNavigate();
  const rows = (data ?? []).filter((i) => filter === "all" || isOpen(i.phase));
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Incidents</h1>
          <p className="sub">{data ? `${data.length} total, ${data.filter((i) => isOpen(i.phase)).length} open` : "Loading…"}</p>
        </div>
        <div className="row">
          <button className={`btn small ${filter === "all" ? "" : "quiet"}`} onClick={() => setFilter("all")}>All</button>
          <button className={`btn small ${filter === "open" ? "" : "quiet"}`} onClick={() => setFilter("open")}>Open</button>
          <a className="btn small quiet" href="/api/submission" download="incident_reports.json">Download reports</a>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {rows.length === 0 ? (
        <div className="card empty"><h2>No incidents</h2><p>When the Sentinel raises one it appears here.</p></div>
      ) : (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr><th>Incident</th><th>State</th><th className="hide-sm">Root cause</th><th className="hide-sm">Services</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.id} className="clickable" onClick={() => nav(`/incidents/${i.id}`)}>
                  <td>
                    <div className="mono faint small">{i.id}</div>
                    <div>{i.title}</div>
                  </td>
                  <td><PhaseLabel phase={i.phase} /></td>
                  <td className="hide-sm">
                    {i.root_cause ? <>{i.root_cause} <span className="faint small">{Math.round((i.confidence ?? 0) * 100)}%</span></> : <span className="faint">-</span>}
                  </td>
                  <td className="hide-sm muted small">{i.services.join(", ")}</td>
                  <td className="small muted" style={{ whiteSpace: "nowrap" }}>{ago(i.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
