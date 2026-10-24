import { useState } from "react";
import { BrowserRouter, NavLink, Outlet, Route, Routes, useLocation } from "react-router";
import { post } from "./api";
import { Dialog, useNamed } from "./components";
import { LiveProvider, useLive } from "./live";
import Agents from "./pages/Agents";
import IncidentPage, { DiagnosisTab, EvidenceTab, PlanTab, ReportTab, TimelineTab } from "./pages/Incident";
import Incidents from "./pages/Incidents";
import Practice from "./pages/Practice";
import Services from "./pages/Services";
import WarRoom from "./pages/WarRoom";

export default function App() {
  return (
    <LiveProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<WarRoom />} />
            <Route path="incidents" element={<Incidents />} />
            <Route path="incidents/:id" element={<IncidentPage />}>
              <Route index element={<TimelineTab />} />
              <Route path="timeline" element={<TimelineTab />} />
              <Route path="evidence" element={<EvidenceTab />} />
              <Route path="diagnosis" element={<DiagnosisTab />} />
              <Route path="plan" element={<PlanTab />} />
              <Route path="report" element={<ReportTab />} />
            </Route>
            <Route path="agents" element={<Agents />} />
            <Route path="services" element={<Services />} />
            <Route path="practice" element={<Practice />} />
            <Route path="*" element={<div className="empty"><h2>Not found</h2></div>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LiveProvider>
  );
}

function Shell() {
  const { state, refreshState, connected } = useLive();
  const loc = useLocation();
  const [nameDialog, withName] = useNamed();
  const [confirm, setConfirm] = useState<"" | "live" | "stop">("");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const call = (path: string, body: Record<string, unknown>) =>
    withName(async (actor) => {
      setErr(null);
      try {
        await post(path, { ...body, actor });
        setConfirm("");
        setReason("");
        refreshState();
      } catch (e) {
        setErr((e as Error).message);
      }
    });

  const links: [string, string][] = [
    ["/", "War room"], ["/incidents", "Incidents"], ["/agents", "Agents"], ["/services", "Services"],
  ];
  if (state && !state.eval_mode) links.push(["/practice", "Practice"]);
  const dry = state?.dry_run ?? true;

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">Incident Zero</div>
          <nav className="nav">
            {links.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) =>
                isActive || (to === "/incidents" && loc.pathname.startsWith("/incidents/")) ? "active" : ""}>
                {label}
                {to === "/" && !!state?.awaiting_approval && (
                  <span className="badge" title="incidents waiting for your decision">{state.awaiting_approval}</span>
                )}
              </NavLink>
            ))}
          </nav>
          <div className="bar-right">
            {!connected && <span className="small t-warn" title="live updates reconnecting">offline</span>}
            <button className={`mode ${dry ? "" : "live"}`} title="Execution mode" onClick={() =>
              dry ? setConfirm("live") : call("/api/mode", { dry_run: true })}>
              <span className={`dot ${dry ? "" : "crit"}`} />
              {dry ? "Dry run" : "Live"}
            </button>
            {!state?.frozen && <button className="btn danger small" onClick={() => setConfirm("stop")}>Stop all</button>}
          </div>
        </div>
      </header>
      {state?.frozen && (
        <div className="banner">
          <div className="topbar-inner">
            <span>Kill switch engaged. Agents are frozen and the Action API refuses every action.</span>
            <button className="btn small" onClick={() => call("/api/resume", {})}>Resume</button>
          </div>
        </div>
      )}
      <main>
        <Outlet />
      </main>
      {nameDialog}
      {confirm === "live" && (
        <Dialog title="Switch to live mode?" onClose={() => setConfirm("")}>
          <p className="muted">Approved actions will change the running system through the Action API. Every action
            still needs your explicit approval first.</p>
          {err && <p className="error">{err}</p>}
          <div className="actions">
            <button className="btn quiet" onClick={() => setConfirm("")}>Stay in dry run</button>
            <button className="btn primary" onClick={() => call("/api/mode", { dry_run: false })}>Go live</button>
          </div>
        </Dialog>
      )}
      {confirm === "stop" && (
        <Dialog title="Stop all" onClose={() => setConfirm("")}>
          <p className="muted">Freezes every agent, blocks every Action API call, and rolls back actions executed in the
            last few minutes. It stays engaged until you resume.</p>
          <div style={{ marginTop: 16 }}>
            <label htmlFor="iz-reason">Reason</label>
            <input id="iz-reason" autoFocus value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          {err && <p className="error">{err}</p>}
          <div className="actions">
            <button className="btn quiet" onClick={() => setConfirm("")}>Cancel</button>
            <button className="btn danger" onClick={() => call("/api/stop", { reason })}>Stop all</button>
          </div>
        </Dialog>
      )}
    </>
  );
}
