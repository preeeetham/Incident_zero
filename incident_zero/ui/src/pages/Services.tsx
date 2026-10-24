import { useLive, useLiveData } from "../live";
import type { ServiceRow, Services as S } from "../types";
import { ago, num, pct } from "../util";
import { Spark, useOverview } from "../viz";

function tone(r: ServiceRow): "ok" | "warn" | "crit" | "" {
  const c = r.container;
  if (c && !c.running) return "crit";
  const n = r.now, b = r.baseline;
  if (n && n.error_ratio >= 0.05) return "crit";
  const p = n?.p95_ms ?? n?.p95, bp = b?.p95_ms ?? b?.p95;
  if (p && bp && p > Math.max(800, 3 * bp)) return "warn";
  if (c && (c.restart_count > 0 || c.oom_killed)) return "warn";
  return n || c ? "ok" : "";
}

export default function Services() {
  const { data, error } = useLiveData<S>("/api/services");
  const { state } = useLive();
  const ov = useOverview().data;
  const hist = ov?.traffic.by_service ?? {};
  const rows = Object.entries(data?.services ?? {});
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Services</h1>
          <p className="sub">Project {state?.project ?? "-"} · request metrics over the last 2 minutes vs. baseline, trends over 30 minutes · {data ? `updated ${ago(data.at)}` : "loading"}</p>
        </div>
      </div>
      {(error || data?.error) && <p className="error" style={{ marginBottom: 16 }}>{error ?? data?.error}</p>}
      <div className="card" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Service</th><th>Container</th><th className="num">Req/s</th><th className="num">Errors</th>
              <th className="num">p95 ms</th><th className="num hide-sm">Baseline p95</th>
              <th className="hide-sm">Req/s · 30 min</th><th className="hide-sm">Errors · 30 min</th><th className="hide-sm">Calls</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, r]) => (
              <tr key={name}>
                <td><span className="row" style={{ flexWrap: "nowrap" }}><span className={`dot ${tone(r)}`} />{name}</span></td>
                <td className="small muted">
                  {r.container ? <>{r.container.state}{r.container.restart_count ? ` · ${r.container.restart_count} restarts` : ""}{r.container.oom_killed ? " · OOM" : ""}</> : "-"}
                </td>
                <td className="num">{num(r.now?.rps, 2)}</td>
                <td className={`num ${(r.now?.error_ratio ?? 0) >= 0.05 ? "t-crit" : ""}`}>{r.now ? pct(r.now.error_ratio) : "-"}</td>
                <td className="num">{num(r.now?.p95_ms ?? r.now?.p95)}</td>
                <td className="num hide-sm faint">{num(r.baseline?.p95_ms ?? r.baseline?.p95)}</td>
                <td className="hide-sm"><Spark points={hist[name]?.rps ?? []} format={(v) => `${v.toFixed(2)} req/s`} title={`${name} requests`} /></td>
                <td className="hide-sm">{hist[name]?.rps?.length
                  ? <Spark points={hist[name]?.errors?.length ? hist[name].errors : hist[name].rps.map(([t]) => [t, 0] as [number, number])}
                      format={(v) => `${v.toFixed(1)}%`} title={`${name} errors (scale 0-5% or more)`} minTop={5} />
                  : <span className="faint small">-</span>}</td>
                <td className="hide-sm small faint">{r.callees.join(", ") || "-"}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={9} className="muted">{data ? "No services found. Is the demo running?" : "Loading…"}</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
