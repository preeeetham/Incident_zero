import { Fragment, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { post } from "./api";
import { useLive } from "./live";
import type { Event, Incident, Phase } from "./types";
import { PHASE_LABEL, STEPS, clock, params, phaseTone, stepIndex } from "./util";

export function PhaseLabel({ phase }: { phase: Phase }) {
  const tone = phaseTone(phase);
  const moving = !["resolved", "dry_run_complete", "escalated", "rejected", "failed", "false_alarm",
    "awaiting_approval"].includes(phase);
  return (
    <span className="phase">
      <span className={`dot ${tone} ${moving ? "pulse" : ""}`} />
      {PHASE_LABEL[phase]}
    </span>
  );
}

export function Pipeline({ phase }: { phase: Phase }) {
  const at = stepIndex(phase);
  const failed = phase === "failed" || phase === "escalated";
  return (
    <div className="pipeline" aria-label="Agent pipeline">
      {STEPS.map(([key, label], i) => {
        const cls = i < at ? "done" : i === at ? "now" : at === 9 ? (failed && i === 8 ? "fail" : "done") : "";
        return (
          <div key={key} className={`step ${cls}`} title={label}>
            <div className="name">{label}</div>
          </div>
        );
      })}
    </div>
  );
}

export function Dialog({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

// Asks for the approver's name once per session, then runs the action with it.
export function useNamed(): [ReactNode, (run: (name: string) => void) => void] {
  const { approver, setApprover } = useLive();
  const [pending, setPending] = useState<((name: string) => void) | null>(null);
  const [draft, setDraft] = useState("");
  const ask = (run: (name: string) => void) => {
    if (approver) run(approver);
    else setPending(() => run);
  };
  const el = pending ? (
    <Dialog title="Who is on call?" onClose={() => setPending(null)}>
      <p className="muted small">Your name is stamped on every decision you make in this session.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const n = draft.trim();
          if (!n) return;
          setApprover(n);
          const run = pending;
          setPending(null);
          run(n);
        }}
      >
        <div style={{ marginTop: 16 }}>
          <label htmlFor="iz-name">Name</label>
          <input id="iz-name" autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} />
        </div>
        <div className="actions">
          <button type="button" className="btn quiet" onClick={() => setPending(null)}>Cancel</button>
          <button type="submit" className="btn primary" disabled={!draft.trim()}>Continue</button>
        </div>
      </form>
    </Dialog>
  ) : null;
  return [el, ask];
}

// The marker on the right of a log line: what this event means for the incident, as a coloured dot and a word.
export function eventStatus(ev: Event): { tone: "good" | "warning" | "critical"; word: string } | null {
  const to = (ev.detail as { to?: string } | null)?.to;
  // a held symptom is a leftover of an incident already resolved: the Sentinel suppressed it, so it is not a new alert
  if (ev.kind === "alert" && !ev.title.startsWith("held")) return { tone: "critical", word: "alert" };
  if (ev.kind === "error") return { tone: "critical", word: "error" };
  if (ev.kind === "phase" && (to === "escalated" || to === "failed")) return { tone: "critical", word: to };
  if (ev.kind === "approval" && ev.title.startsWith("approval needed")) return { tone: "warning", word: "needs approval" };
  if (ev.kind === "phase" && to === "resolved") return { tone: "good", word: "resolved" };
  if (ev.kind === "verify" && /clear \((\d+)\/\1\)/.test(ev.title)) return { tone: "good", word: "recovered" };
  return null;
}

export function EventRow({ ev, showIncident = false, fresh = false }: { ev: Event; showIncident?: boolean; fresh?: boolean }) {
  const hasDetail = !!(ev.detail && Object.keys(ev.detail).length) || !!ev.evidence_id;
  const status = eventStatus(ev);
  const head = (
    <div className="item-row">
      <span className="ts">{clock(ev.ts)}</span>
      <span className="who">{ev.agent}</span>
      <span className={`grow ${ev.kind === "error" ? "t-crit" : ""}`}>
        {showIncident && ev.incident_id && <span className="faint mono">{ev.incident_id} </span>}
        {ev.title}
      </span>
      {ev.evidence_id && <span className="eid">{ev.evidence_id}</span>}
      {status && <span className={`evmark ${status.tone}`}><i aria-hidden="true" />{status.word}</span>}
      {hasDetail && <span className="chev">›</span>}
    </div>
  );
  const cls = `item${fresh ? " fresh" : ""}${status?.tone === "critical" ? " flagged" : ""}`;
  if (!hasDetail) return <div className={cls}>{head}</div>;
  return (
    <details className={cls}>
      <summary>{head}</summary>
      <div className="expand stack-s">
        {ev.evidence_id && ev.incident_id && (
          <Link className="small t-info" to={`/incidents/${ev.incident_id}/evidence#${ev.evidence_id}`}>
            Open evidence {ev.evidence_id}
          </Link>
        )}
        {ev.detail && <pre>{JSON.stringify(ev.detail, null, 2)}</pre>}
      </div>
    </details>
  );
}

// The decision controls: approve (primary), send back (needs a note), reject.
export function Decision({ inc, onDone }: { inc: Incident; onDone?: () => void }) {
  const [nameDialog, withName] = useNamed();
  const [mode, setMode] = useState<"" | "sent_back" | "rejected">("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const plan = inc.plan;
  if (!plan || inc.phase !== "awaiting_approval" || inc.approval.decision !== "pending") return null;
  const send = (decision: string, n: string) =>
    withName(async (approver) => {
      setBusy(true);
      setErr(null);
      try {
        await post(`/api/incidents/${inc.id}/decision`, { decision, approver, change_hash: plan.change_hash, note: n });
        setMode("");
        onDone?.();
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setBusy(false);
      }
    });
  return (
    <div className="stack-s">
      {nameDialog}
      {mode === "" ? (
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={() => send("approved", "")}>
            Approve {plan.actions.length > 1 ? `${plan.actions.length} actions` : plan.actions[0]?.action}
          </button>
          <button className="btn" disabled={busy} onClick={() => setMode("sent_back")}>Send back</button>
          <button className="btn quiet" disabled={busy} onClick={() => setMode("rejected")}>Reject</button>
        </div>
      ) : (
        <form
          className="stack-s"
          onSubmit={(e) => {
            e.preventDefault();
            send(mode, note);
          }}
        >
          <div>
            <label htmlFor="iz-note">
              {mode === "sent_back" ? "What should the investigator look at?" : "Why reject? (optional)"}
            </label>
            <textarea id="iz-note" autoFocus value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
          <div className="row">
            <button type="submit" className="btn primary" disabled={busy || (mode === "sent_back" && !note.trim())}>
              {mode === "sent_back" ? "Send back to investigation" : "Reject plan"}
            </button>
            <button type="button" className="btn quiet" onClick={() => setMode("")}>Cancel</button>
          </div>
        </form>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}

export function PlanActions({ inc }: { inc: Incident }) {
  if (!inc.plan) return <p className="muted">No plan yet.</p>;
  if (!inc.plan.actions.length) return <p className="muted">No allowed action fits: {inc.plan.no_action_reason}</p>;
  return (
    <div className="stack-s">
      {inc.plan.actions.map((a, i) => (
        <div key={i} className="stack-s">
          <div className="row">
            <span className="faint mono">{i + 1}.</span>
            <code>{a.action}</code>
            <span className="mono muted">{params(a.parameters)}</span>
            {a.reversible && <span className="tag">reversible</span>}
          </div>
          <p>{a.reason}</p>
          {a.citations.map((c, j) => {
            const k = (inc.knowledge ?? []).find((x) => x.id === c.knowledge_id);
            return (
              <div key={j} className="small">
                <span className="eid">{c.knowledge_id}</span> <span className="tag">{k?.source ?? "?"}</span>{" "}
                <span className="mono muted">{k?.ref}</span>
                <div className="quote mono">{c.quote}</div>
              </div>
            );
          })}
          {a.track_record && <p className="small t-info">Incident memory: {a.track_record}</p>}
          {a.preview && <div className="quote">dry run: {a.preview}</div>}
        </div>
      ))}
    </div>
  );
}

// A small Markdown renderer for the report (headings, lists, bold, code, quotes). No HTML passthrough.
export function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const out: ReactNode[] = [];
  let list: ReactNode[][] = []; // each item: its text, then any quote lines under it
  const flush = () => {
    if (list.length) out.push(<ul key={`ul${out.length}`}>{list.map((parts, j) => <li key={j}>{parts}</li>)}</ul>);
    list = [];
  };
  lines.forEach((ln, i) => {
    if (ln.startsWith("# ")) {
      flush();
      out.push(<h1 key={i}>{inline(ln.slice(2))}</h1>);
    } else if (ln.startsWith("## ")) {
      flush();
      out.push(<h2 key={i}>{inline(ln.slice(3))}</h2>);
    } else if (ln.startsWith("- ")) {
      list.push([<Fragment key={i}>{inline(ln.slice(2))}</Fragment>]);
    } else if (ln.trim().startsWith("> ")) {
      const q = <blockquote key={i} className="quote">{ln.trim().slice(2)}</blockquote>;
      if (list.length) list[list.length - 1].push(q);
      else out.push(q);
    } else if (ln.trim()) {
      flush();
      out.push(<p key={i}>{inline(ln.replace(/ {2}$/, ""))}</p>);
    }
  });
  flush();
  return <div className="md">{out}</div>;
}

function inline(s: string): ReactNode {
  const parts = s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.map((p, i) =>
    p.startsWith("**") && p.endsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong>
    : p.startsWith("`") && p.endsWith("`") ? <code key={i}>{p.slice(1, -1)}</code>
    : <Fragment key={i}>{p}</Fragment>,
  );
}
