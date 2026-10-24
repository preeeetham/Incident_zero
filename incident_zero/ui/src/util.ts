import type { Phase } from "./types";
import { TERMINAL } from "./types";

export const PHASE_LABEL: Record<Phase, string> = {
  detected: "Detected", investigating: "Investigating", diagnosing: "Diagnosing", auditing: "Auditing evidence",
  planning: "Planning", awaiting_approval: "Awaiting approval", executing: "Executing", verifying: "Verifying",
  reporting: "Reporting", resolved: "Resolved", dry_run_complete: "Dry run complete", escalated: "Escalated",
  rejected: "Rejected", failed: "Failed", false_alarm: "False alarm",
};

export function phaseTone(p: Phase): "ok" | "warn" | "crit" | "info" | "" {
  if (p === "resolved") return "ok";
  if (p === "awaiting_approval") return "warn";
  if (p === "escalated" || p === "failed") return "crit";
  if (p === "rejected" || p === "dry_run_complete") return "";
  return "info";
}

export const isOpen = (p: Phase) => !TERMINAL.includes(p);

// The nine agents in pipeline order, and which step each phase is on.
export const STEPS = [
  ["sentinel", "Sentinel"], ["investigator", "Investigator"], ["diagnostician", "Diagnostician"],
  ["auditor", "Auditor"], ["planner", "Planner"], ["approval", "Approval"], ["executor", "Executor"],
  ["verifier", "Verifier"], ["reporter", "Reporter"],
] as const;

export function stepIndex(p: Phase): number {
  const map: Partial<Record<Phase, number>> = {
    detected: 0, investigating: 1, diagnosing: 2, auditing: 3, planning: 4, awaiting_approval: 5, executing: 6,
    verifying: 7, reporting: 8,
  };
  return map[p] ?? 9;
}

export function ago(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "-";
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(t).toLocaleDateString();
}

export function until(iso: string | null | undefined): string {
  if (!iso) return "-";
  const s = Math.round((Date.parse(iso) - Date.now()) / 1000);
  if (s <= 0) return "now";
  if (s < 60) return `in ${s}s`;
  if (s < 3600) return `in ${Math.floor(s / 60)}m ${s % 60}s`;
  return `in ${Math.floor(s / 3600)}h`;
}

export function clock(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function duration(from: string, to?: string | null): string {
  const s = Math.max(0, Math.round(((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function pct(x: number | null | undefined, digits = 1): string {
  return x === null || x === undefined ? "-" : `${(x * 100).toFixed(digits)}%`;
}

export function num(x: number | null | undefined, digits = 0): string {
  return x === null || x === undefined ? "-" : x.toFixed(digits);
}

export const params = (p: Record<string, unknown>) =>
  Object.entries(p).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
