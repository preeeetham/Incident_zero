// The War Room's live picture, drawn as plain SVG from /api/overview (Prometheus span metrics, Docker state and the
// incident store; nothing generated). Status colours never carry meaning alone: every one has a glyph and a word.
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router";
import { useLiveData } from "./live";
import type { ContainerTile, Overview, OverviewIncident, Point } from "./types";

// Polls on its own as well: in a quiet system no event arrives to trigger a refetch.
export function useOverview() {
  const res = useLiveData<Overview>("/api/overview");
  const { reload } = res;
  useEffect(() => {
    const t = window.setInterval(reload, 15000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return res;
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)));
    ro.observe(el);
    setW(Math.floor(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const hhmm = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const hhmmss = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// A smooth curve through the points that never overshoots them (monotone cubic, Fritsch-Carlson), so a smooth line
// still shows exactly the measured values at the sample times.
export function monotonePath(p: [number, number][]): string {
  const n = p.length;
  if (!n) return "";
  if (n < 3) return p.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const dx: number[] = [], m: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(p[i + 1][0] - p[i][0]);
    m.push((p[i + 1][1] - p[i][1]) / (dx[i] || 1));
  }
  const t: number[] = [m[0]];
  for (let i = 1; i < n - 1; i++) t.push(m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2);
  t.push(m[n - 2]);
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i], b = t[i + 1] / m[i], h = a * a + b * b;
    if (h > 9) { const k = 3 / Math.sqrt(h); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  let d = `M${p[0][0].toFixed(1)},${p[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += `C${(p[i][0] + h).toFixed(1)},${(p[i][1] + t[i] * h).toFixed(1)} ${(p[i + 1][0] - h).toFixed(1)},`
      + `${(p[i + 1][1] - t[i + 1] * h).toFixed(1)} ${p[i + 1][0].toFixed(1)},${p[i + 1][1].toFixed(1)}`;
  }
  return d;
}

// Span metrics reach Prometheus about once a minute: keep one sample per `every` seconds (the same as querying the
// range with that step), so the line shows real changes instead of 15-second plateaus.
export function resample(points: Point[], every: number): Point[] {
  const out: Point[] = [];
  for (const p of points) if (!out.length || p[0] - out[out.length - 1][0] >= every - 1) out.push(p);
  const last = points[points.length - 1];
  if (last && out[out.length - 1] !== last) out.push(last);
  return out;
}

const dur = (s: number) => (s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`);

function bandText(i: OverviewIncident): string {
  const f = i.fixes.length ? Math.min(...i.fixes.map((x) => x.t)) : null;
  const fix = f !== null ? ` (fix after ${dur(f - i.start)})` : "";
  if (i.phase === "resolved" && i.end !== null) return `${i.id} resolved in ${dur(i.end - i.start)}${fix}`;
  if (i.end !== null) return `${i.id} ${i.phase.replace("_", " ")} after ${dur(i.end - i.start)}${fix}`;
  return `${i.id} open for ${dur(Date.now() / 1000 - i.start)}${fix}`;
}

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

// ------------------------------------------------------------------ line chart (one series, one axis)
interface LineProps {
  points: Point[];
  t0: number;
  t1: number;
  minTop: number;        // the y axis reaches at least this (so a flat 0.1 % does not look like a spike)
  fixedTop?: number;     // or exactly this (percentages of a whole)
  format: (v: number) => string;
  label: string;
  incidents: OverviewIncident[];
  height?: number;
}

export function LineChart({ points, t0, t1, minTop, fixedTop, format, label, incidents, height = 132 }: LineProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const pad = { l: 40, r: 48, t: 16, b: 36 };  // bottom: the fix-marker lane, then the time axis
  const W = Math.max(width, 200), H = height;
  const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
  const top = fixedTop ?? niceMax(Math.max(minTop, ...points.map((p) => p[1])));
  const x = (t: number) => pad.l + ((t - t0) / Math.max(1, t1 - t0)) * pw;
  const y = (v: number) => pad.t + ph - (Math.min(v, top) / top) * ph;
  const pts = points.filter((p) => p[0] >= t0 && p[0] <= t1);
  // smooth segments; break the line where samples are missing (more than three steps apart)
  const step = pts.length > 1 ? (pts[pts.length - 1][0] - pts[0][0]) / (pts.length - 1) : 15;
  const segs: [number, number][][] = [];
  pts.forEach((p, i) => {
    if (i === 0 || p[0] - pts[i - 1][0] > step * 3) segs.push([]);
    segs[segs.length - 1].push([x(p[0]), y(p[1])]);
  });
  const line = segs.map(monotonePath).join("");
  const area = segs.map((sg) => `${monotonePath(sg)}L${sg[sg.length - 1][0].toFixed(1)},${y(0)}`
    + `L${sg[0][0].toFixed(1)},${y(0)}Z`).join("");
  const ticks = [0, top / 2, top];
  const xticks: number[] = [];
  const every = t1 - t0 > 1200 ? 600 : t1 - t0 > 600 ? 300 : 120;
  for (let t = Math.ceil(t0 / every) * every; t <= t1; t += every) xticks.push(t);
  const last = pts[pts.length - 1];
  const hp = hover !== null ? pts[hover] : null;
  // the band is the whole time to resolve: from the alert to verified recovery (or until the incident closed / now).
  // Darker until the first live fix (broken), lighter after it (the Verifier confirming recovery).
  const fixAt = (i: OverviewIncident) => (i.fixes.length ? Math.min(...i.fixes.map((f) => f.t)) : null);
  const bandEnd = (i: OverviewIncident) => i.end ?? t1;
  const bandAt = (t: number) => incidents.find((i) => t >= i.start && t <= bandEnd(i));

  const pick = (clientX: number) => {
    const el = ref.current;
    if (!el || !pts.length) return;
    const px = clientX - el.getBoundingClientRect().left;
    const t = t0 + ((px - pad.l) / pw) * (t1 - t0);
    let best = 0;
    pts.forEach((p, i) => { if (Math.abs(p[0] - t) < Math.abs(pts[best][0] - t)) best = i; });
    setHover(best);
  };
  const onKey = (e: KeyboardEvent) => {
    if (!pts.length) return;
    if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? pts.length - 1) - 1));
    else if (e.key === "ArrowRight") setHover((h) => Math.min(pts.length - 1, (h ?? pts.length - 1) + 1));
    else return;
    e.preventDefault();
  };

  return (
    <div className="viz" ref={ref}>
      <svg width={W} height={H} role="img" aria-label={label} tabIndex={0}
        onPointerMove={(e) => pick(e.clientX)} onPointerLeave={() => setHover(null)} onKeyDown={onKey}
        onFocus={() => setHover(pts.length ? pts.length - 1 : null)} onBlur={() => setHover(null)}>
        {incidents.map((i) => {
          const a = Math.max(i.start, t0), b = Math.min(bandEnd(i), t1);
          if (b <= t0 || a >= t1) return null;
          const f = fixAt(i), mid = f !== null ? Math.min(Math.max(f, a), b) : b;
          return (
            <g key={i.id}>
              <rect className="band" x={x(a)} y={pad.t} width={Math.max(2, x(mid) - x(a))} height={ph} />
              {mid < b && <rect className="band-verify" x={x(mid)} y={pad.t} width={x(b) - x(mid)} height={ph} />}
              {i.phase === "resolved" && i.end !== null && i.end <= t1 && (
                <circle className="mk-resolved" cx={x(i.end)} cy={pad.t - 7} r={4}>
                  <title>{`${i.id}: resolved (recovery verified) ${hhmmss(i.end)}, ${dur(i.end - i.start)} after the alert`}</title>
                </circle>
              )}
              {i.start >= t0 && (
                <path className="mk-alert" d={`M${x(i.start) - 5},${pad.t - 11}h10l-5,8z`}>
                  <title>{`${i.id}: alert opened ${hhmmss(i.start)}`}</title>
                </path>
              )}
              {i.fixes.filter((f) => f.t >= t0 && f.t <= t1).map((f, k) => (
                <path key={k} className="mk-fix" d={`M${x(f.t)},${pad.t + ph + 3}l5,5l-5,5l-5,-5z`}>
                  <title>{`${i.id}: ${f.action} applied ${hhmmss(f.t)}`}</title>
                </path>
              ))}
            </g>
          );
        })}
        {ticks.map((v) => (
          <g key={v}>
            <line className="grid" x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} />
            <text className="tick" x={pad.l - 6} y={y(v) + 4} textAnchor="end">{format(v)}</text>
          </g>
        ))}
        {xticks.map((t) => (
          <text key={t} className="tick" x={x(t)} y={H - 4} textAnchor="middle">{hhmm(t)}</text>
        ))}
        <path className="area" d={area} />
        <path className="line" d={line} />
        {last && !hp && (
          <>
            <circle className="end" cx={x(last[0])} cy={y(last[1])} r={4} />
            <text className="endlabel" x={x(last[0]) + 8} y={y(last[1]) + 4}>{format(last[1])}</text>
          </>
        )}
        {hp && (
          <>
            <line className="cross" x1={x(hp[0])} x2={x(hp[0])} y1={pad.t} y2={pad.t + ph} />
            <circle className="end" cx={x(hp[0])} cy={y(hp[1])} r={4} />
          </>
        )}
      </svg>
      {!pts.length && <p className="viz-empty small faint">No samples yet</p>}
      {hp && (
        <div className="tip" style={{ left: Math.min(Math.max(x(hp[0]) - 70, 0), W - 150), top: 0 }}>
          <strong>{format(hp[1])}</strong>
          <span>{hhmmss(hp[0])}</span>
          {bandAt(hp[0]) && <span>{bandText(bandAt(hp[0])!)}</span>}
        </div>
      )}
    </div>
  );
}

export function MarkerLegend() {
  return (
    <div className="legend small muted" aria-label="Chart markers">
      <span><svg width="12" height="10" aria-hidden="true"><path className="mk-alert" d="M1,1h10l-5,8z" /></svg> alert opened (above the chart)</span>
      <span><svg width="12" height="12" aria-hidden="true"><path className="mk-fix" d="M6,1l5,5l-5,5l-5,-5z" /></svg> fix applied, live (below the chart)</span>
      <span><svg width="10" height="10" aria-hidden="true"><circle className="mk-resolved" cx="5" cy="5" r="4" /></svg> resolved (recovery verified)</span>
      <span><svg width="14" height="10" aria-hidden="true"><rect className="band" width="14" height="10" /></svg> broken: alert to fix</span>
      <span><svg width="14" height="10" aria-hidden="true"><rect className="band-verify" width="14" height="10" /></svg> verifying: fix to resolved</span>
    </div>
  );
}

// A table twin for every chart: the same numbers without hovering.
export function TableView({ points, format, title }: { points: Point[]; format: (v: number) => string; title: string }) {
  const every = Math.max(1, Math.floor(points.length / 12));
  const rows = points.filter((_, i) => i % every === 0 || i === points.length - 1).reverse();
  return (
    <details className="tableview">
      <summary className="small faint">Table <span className="chev">›</span></summary>
      <table>
        <thead><tr><th>Time</th><th className="num">{title}</th></tr></thead>
        <tbody>{rows.map(([t, v]) => <tr key={t}><td className="small muted">{hhmmss(t)}</td><td className="num">{format(v)}</td></tr>)}</tbody>
      </table>
    </details>
  );
}

// ------------------------------------------------------------------ containers
type Status = { tone: "good" | "warning" | "critical"; glyph: string; word: string };
export function containerStatus(c: ContainerTile): Status {
  if (c.state === "paused") return { tone: "critical", glyph: "❚❚", word: "paused" };
  if (!c.running) return { tone: "critical", glyph: "✕", word: c.state || "stopped" };
  if (c.state === "restarting") return { tone: "warning", glyph: "↻", word: "restarting" };
  if (c.restarts > 0 || c.oom) return { tone: "warning", glyph: "↻", word: c.oom ? "OOM-killed" : "restarted" };
  return { tone: "good", glyph: "✓", word: "running" };
}

function uptime(s: number | null): string {
  if (s === null || s === undefined) return "-";
  if (s < 120) return `${Math.round(s)} s`;
  if (s < 7200) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}

export function ContainerGrid({ containers }: { containers: ContainerTile[] }) {
  const [hover, setHover] = useState<string | null>(null);
  const st = containers.map((c) => [c, containerStatus(c)] as const);
  const counts = new Map<string, [Status, number]>();
  st.forEach(([, s]) => counts.set(s.word, [s, (counts.get(s.word)?.[1] ?? 0) + 1]));
  const h = st.find(([c]) => c.name === hover);
  return (
    <div className="stack-s">
      <div className="tiles" onPointerLeave={() => setHover(null)}>
        {st.map(([c, s]) => (
          <button key={c.name} type="button" className={`tile ${s.tone} ${hover === c.name ? "on" : ""}`}
            aria-label={`${c.name}: ${s.word}`} onPointerEnter={() => setHover(c.name)}
            onFocus={() => setHover(c.name)} onBlur={() => setHover(null)}>
            <span aria-hidden="true">{s.glyph}</span>
          </button>
        ))}
      </div>
      <p className="small tile-readout" aria-live="polite">
        {h ? <><strong>{h[0].name}</strong> <span className="muted">· {h[1].word} · {h[0].state} · up {uptime(h[0].uptime_s)}
          {h[0].restarts ? ` · ${h[0].restarts} restarts` : ""}</span></>
          : <span className="faint">Hover a tile for its container</span>}
      </p>
      <div className="legend small muted">
        {[...counts.values()].map(([s, n]) => (
          <span key={s.word}><i className={`sw ${s.tone}`} aria-hidden="true">{s.glyph}</i> {s.word} {n}</span>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ sparkline (Services table)
export function Spark({ points: raw, format, title, minTop = 0 }:
  { points: Point[]; format: (v: number) => string; title: string; minTop?: number }) {
  const W = 120, H = 30, pad = 5;
  const points = resample(raw, 60);
  if (!points.length) return <span className="faint small">-</span>;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  // minTop keeps a small blip small: error lines are scaled to at least the alert threshold
  const top = niceMax(Math.max(minTop, ...points.map((p) => p[1]), 1e-9));
  const x = (t: number) => pad + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * pad);
  const y = (v: number) => H - pad - (v / top) * (H - 2 * pad);
  const d = monotonePath(points.map((p) => [x(p[0]), y(p[1])]));
  const last = points[points.length - 1];
  const peak = Math.max(...points.map((p) => p[1]));
  return (
    <svg width={W} height={H} className="spark" role="img" aria-label={`${title}: now ${format(last[1])}, peak ${format(peak)}`}>
      <title>{`${title}: now ${format(last[1])}, 30-min peak ${format(peak)}`}</title>
      <path className="line" d={d} />
      <circle className="end" cx={x(last[0])} cy={y(last[1])} r={3} />
    </svg>
  );
}

// ------------------------------------------------------------------ the panel
const RANGES = [[600, "10 min"], [900, "15 min"], [1800, "30 min"]] as const;
const RANGE_KEY = "iz.range";

function readRange(): number {
  try {
    const v = Number(localStorage.getItem(RANGE_KEY));
    return RANGES.some(([s]) => s === v) ? v : 900;
  } catch {
    return 900;
  }
}

export function OverviewPanel() {
  const { data } = useOverview();
  const [range, setRangeState] = useState(readRange);
  const setRange = (v: number) => {
    setRangeState(v);
    try { localStorage.setItem(RANGE_KEY, String(v)); } catch { /* private mode: this page only */ }
  };
  if (!data) return null;
  const t1 = data.now, t0 = t1 - Math.min(range, data.window_s);
  const inView = data.incidents.filter((i) => (i.end ?? t1) >= t0);
  const latest = data.latest;
  const score = latest?.score;
  const tone = score === null || score === undefined ? "" : score >= 0.999 ? "good" : score >= 0.9 ? "warning" : "critical";
  const stab: Point[] = data.samples.filter((s) => s.score !== null).map((s) => [s.t, (s.score ?? 0) * 100]);
  const tr = data.traffic;
  const pctf = (v: number) => `${v < 10 && v % 1 ? v.toFixed(1) : Math.round(v)}%`;
  const rpsf = (v: number) => (v >= 10 || v === 0 ? v.toFixed(0) : v.toFixed(1));
  const up = data.containers.filter((c) => c.running).length;
  const rps = resample(tr.rps.filter((p) => p[0] >= t0), 60);
  const errs = resample(tr.errors.filter((p) => p[0] >= t0), 60);
  const rangeLabel = RANGES.find(([sec]) => sec === range)?.[1] ?? "";
  return (
    <div className="stack">
      <div className="spread">
        <div className="seg" role="group" aria-label="Time range for every chart">
          {RANGES.map(([sec, label]) => (
            <button key={sec} type="button" className={sec === range ? "on" : ""} aria-pressed={sec === range}
              onClick={() => setRange(sec)}>{label}</button>
          ))}
        </div>
        <span className="small faint">live · refreshes every 15 s · updated {hhmmss(data.now)}</span>
      </div>
      <div className="grid-2 even">
        <section className="card stack-s">
          <div className="card-head tight-head">
            <h3>System stability</h3>
            <span className="small faint">last {rangeLabel}{data.since > t0 ? ` · sampled since ${hhmm(data.since)}` : ""}</span>
          </div>
          <div className="hero-row">
            <span className="hero">{score === null || score === undefined ? "–" : `${Math.round(score * 100)}%`}</span>
            <span className="small muted">
              {latest ? `${latest.healthy} of ${latest.total} services healthy` : "waiting for the first sample"}
              <br />
              {latest && (latest.unhealthy.length
                ? <span className={`status-word ${tone}`}>
                    <i className={`sw ${tone}`} aria-hidden="true">!</i>{" "}
                    {latest.unhealthy.map((u) => `${u.name} (${u.why})`).join(", ")}
                  </span>
                : <span className="status-word good"><i className="sw good" aria-hidden="true">✓</i> all within alert rules</span>)}
            </span>
          </div>
          <LineChart points={stab} t0={t0} t1={t1} minTop={100} fixedTop={100} format={(v) => `${Math.round(v)}%`}
            label={`Share of services healthy, last ${rangeLabel}`} incidents={inView} height={130} />
          <p className="small faint">{data.definition}</p>
        </section>
        <section className="card stack-s">
          <div className="card-head tight-head">
            <h3>Containers</h3>
            <span className="small faint">Docker, live</span>
          </div>
          <div className="hero-row">
            <span className="hero">{up}<span className="hero-of">/{data.containers.length}</span></span>
            <span className="small muted">containers running</span>
          </div>
          <ContainerGrid containers={data.containers} />
        </section>
      </div>
      <section className="card stack-s">
        <div className="card-head tight-head">
          <h3>Traffic and errors</h3>
          <span className="small faint">Prometheus span metrics, one point per minute, last {rangeLabel}{tr.error ? ` · ${tr.error}` : ""}</span>
        </div>
        <div className="grid-2 even">
          <div className="stack-s">
            <p className="small"><strong>Shop traffic</strong> <span className="muted">· requests/s into {tr.entry}</span></p>
            <LineChart points={rps} t0={t0} t1={t1} minTop={1} format={rpsf} label={`Requests per second into ${tr.entry}`} incidents={inView} />
            <TableView points={rps} format={rpsf} title="req/s" />
          </div>
          <div className="stack-s">
            <p className="small"><strong>Errors</strong> <span className="muted">· % of all server calls</span></p>
            <LineChart points={errs} t0={t0} t1={t1} minTop={2} format={pctf} label="Error share of all server calls" incidents={inView} />
            <TableView points={errs} format={pctf} title="errors" />
          </div>
        </div>
        <MarkerLegend />
        {inView.length > 0 && (
          <p className="small faint">
            In view: {inView.map((i, k) => <span key={i.id}>{k ? ", " : ""}<Link className="t-info" to={`/incidents/${i.id}`}>{i.id}</Link> {i.services.join("/")}</span>)}
          </p>
        )}
      </section>
    </div>
  );
}

export function StatusMark({ tone, word }: { tone: "good" | "warning" | "critical"; word: string }): ReactNode {
  return <span className={`evmark ${tone}`}><i aria-hidden="true" />{word}</span>;
}
