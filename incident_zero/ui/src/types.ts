// Mirrors incident_zero/models.py and the server's JSON.

export type Phase =
  | "detected" | "investigating" | "diagnosing" | "auditing" | "planning" | "awaiting_approval"
  | "executing" | "verifying" | "reporting"
  | "resolved" | "dry_run_complete" | "escalated" | "rejected" | "failed" | "false_alarm";

export const TERMINAL: Phase[] = ["resolved", "dry_run_complete", "escalated", "rejected", "failed", "false_alarm"];

export interface Alert {
  id: string; rule: string; service: string; signal: string;
  value: number | null; threshold: number | null; baseline: number | null; unit: string;
  query: string; first_seen: string; last_seen: string; active: boolean;
}

export interface Claim { text: string; evidence_id: string; quote: string }
export interface RootCause { component: string; category: string; cause: string; confidence: number; claims: Claim[] }
export interface Diagnosis {
  summary: string; root_causes: RootCause[]; explained_alerts: string[]; produced_by: "llm" | "rules"; created_at: string;
  false_alarm: boolean; claims: Claim[];
}
export interface AuditResult {
  passed: boolean; problems: string[]; unexplained_alerts: string[]; checked_claims: number; created_at: string;
}
export interface Citation { knowledge_id: string; quote: string }
export interface KnowledgeItem {
  id: string; source: "code" | "runbook" | "memory"; ref: string; summary: string; content: string;
  data: Record<string, unknown>;
}
export interface ProposedAction {
  action: string; parameters: Record<string, unknown>; reason: string; dry_run: boolean;
  preview: string | null; reversible: boolean; citations: Citation[]; track_record: string;
}
export interface Plan {
  actions: ProposedAction[]; change_hash: string; produced_by: string; no_action_reason: string; created_at: string;
}
export interface Approval {
  required: boolean; decision: "pending" | "approved" | "rejected" | "sent_back" | "timed_out" | "not_requested";
  approver: string | null; timestamp: string | null; note: string; change_hash: string | null;
  requested_at: string | null; expires_at: string | null;
}
export interface ActionResult {
  action: string; parameters: Record<string, unknown>; execution_id: string | null; ok: boolean; dry_run: boolean;
  message: string; at: string;
}
export interface Incident {
  id: string; title: string; phase: Phase; created_at: string; updated_at: string; resolved_at: string | null;
  alerts: Alert[]; services: string[]; rounds: number; investigation_notes: string[]; human_notes: string[];
  diagnosis: Diagnosis | null; audits: AuditResult[]; knowledge: KnowledgeItem[]; plan: Plan | null;
  approval: Approval;
  execution: { attempted: boolean; results: ActionResult[]; rollback_performed: boolean;
               rollback_results: ActionResult[]; notes: string };
  verification: { checked: boolean; recovered: boolean | null; signals_checked: string[]; notes: string;
                  samples: { at: string; firing: string[] }[] };
  prevention: string[]; limitations: string[]; escalation_reason: string; report_summary: string; llm: string;
  tokens: TokenUsage; recall: Recall | null;
}
export interface TokenUsage {
  calls: number; prompt_tokens: number; completion_tokens: number; estimated: boolean;
  by_agent: Record<string, { calls: number; prompt_tokens: number; completion_tokens: number }>;
}
export interface Recall {
  status: "matched" | "confirmed" | "abandoned"; case: string; similarity: number; component: string; category: string;
  actions: { action: string; parameters: Record<string, unknown> }[]; track_record: string; reason: string;
  model_calls_skipped: number; tokens_saved: number; tokens_saved_estimated: boolean;
}
export interface Case {
  incident: string; at: string; outcome: string; alerts: string[]; components: string[]; categories: string[];
  mode: "reasoned" | "recalled"; recall_of: string | null; tokens: { calls: number; total: number; estimated?: boolean };
  tokens_saved: number;
}
export interface BrainStats {
  model: string; recall_enabled: boolean; incidents: number; by_outcome: Record<string, number>; known_fixes: number;
  recalled: number; model_calls: number; tokens_used: number; tokens_saved: number; cases: Case[];
}
export interface IncidentSummary {
  id: string; title: string; phase: Phase; created_at: string; updated_at: string; services: string[];
  alerts: number; active_alerts: number; root_cause: string | null; confidence: number | null; approval: string;
}
export interface Evidence {
  id: string; source: string; tool: string; args: Record<string, unknown>; summary: string; content: string;
  data: unknown; created_at: string;
}
export interface Event {
  seq: number; ts: string; incident_id: string | null; agent: string; kind: string; title: string;
  detail: Record<string, unknown> | null; evidence_id: string | null;
}
export interface State {
  dry_run: boolean; frozen: boolean; eval_mode: boolean; llm: string | null; llm_reason: string;
  endpoints: { prometheus: string; jaeger: string; opensearch: string; action_api: string; notes: string[] };
  open: number; awaiting_approval: number; total: number; last_event: number; project: string;
  approval_timeout_s: number;
}
export interface AgentRow {
  agent: string; layer: "eyes" | "hands"; state: "idle" | "working"; incident_id: string | null; doing: string; since: string; runs: number;
  recent: Event[];
}
export interface RED { rps: number; error_ratio: number; p95_ms?: number | null; p95?: number | null }
export interface ServiceRow {
  now: RED | null; baseline: RED | null; callees: string[];
  container: { state: string; running: boolean; restart_count: number; oom_killed: boolean; health: string | null;
               uptime_s: number | null } | null;
}
export interface Services { at: number; error: string | null; services: Record<string, ServiceRow> }
export interface Scenario { id: string; title: string; split: string; faults: string[]; injects?: string; effect?: string }
export interface PracticeRun {
  id: string; scenario_id: string; title: string; split: string; started_at: number; deadline: number;
  status: "active" | "reverted"; notes: string[]; ended_at?: number;
  truth?: { components: string[]; categories: string[]; actions: string[] };
  score: { points: number; max: number; incidents: string[]; detail: Record<string, unknown> } | null;
}
export type Point = [number, number];
export interface StabilitySample {
  t: number; score: number | null; healthy: number; total: number; up: number; containers: number;
  unhealthy: { name: string; why: string }[];
}
export interface ContainerTile {
  name: string; state: string; running: boolean; restarts: number; oom: boolean; uptime_s: number | null;
}
export interface OverviewIncident {
  id: string; title: string; phase: Phase; start: number; end: number | null; services: string[];
  fixes: { t: number; action: string; parameters: Record<string, unknown> }[];
}
export interface Overview {
  now: number; window_s: number; since: number; samples: StabilitySample[]; latest: StabilitySample | null;
  containers: ContainerTile[]; incidents: OverviewIncident[]; definition: string;
  traffic: {
    entry: string; rps_query: string; error_query: string; rps: Point[]; errors: Point[]; error: string | null;
    by_service: Record<string, { rps: Point[]; errors: Point[] }>;
  };
}
