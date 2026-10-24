"""Agent 5, the Remediation Planner: proposes actions from the Action API's allowed list, with a reason
(REQUIREMENTS F4.1, F4.2, S5).

The model (or the rules, by diagnosis category) only chooses. Code validates every action and parameter against
the list the API returned, the API previews each one as a dry run, and the plan is hashed exactly as the human
will see it. If nothing in the runbook fits, the plan says so and the incident escalates.

Before choosing, it retrieves knowledge (incident_zero.knowledge): the code that raises the diagnosed error and the
flags that guard it, runbooks for the symptom, and past incidents' verified outcomes. Every proposed action must cite
that knowledge verbatim; code checks the citations and drops an action whose citations do not hold.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

import yaml

from ..actions.catalog import ActionDef, validate
from ..actions.client import ActionAPIError
from ..knowledge import Knowledge, check_citations, failing_operations, quote_in, render
from ..knowledge.memory import Memory, action_key
from ..models import Citation, Diagnosis, Evidence, Incident, KnowledgeItem, Plan, ProposedAction
from .context import Context
from .facts import extract, latest

SYSTEM = """You choose remediation actions for a diagnosed incident. Choose ONLY from the allowed actions given,
with parameters that match their definitions. Prefer the least disruptive action that removes the root cause, not
only its symptom. Base every action on the retrieved knowledge (K1, K2, ...): the code that raises the error and the
feature flags that decide whether it runs, runbooks for this kind of failure, and past incidents with their verified
outcomes. A restart does not fix a defect in code. When an alert names a failing operation, the code of its
handler shows which flag decides the branch it takes, even if the error itself is raised deeper (e.g. a client
that cannot connect because the branch picked a bad store). A flag read in the failing code outweighs a past
incident that recorded a restart as the fix: past outcomes can be wrong, the code is not. Only switch off a flag that the retrieved code shows deciding
the failing code path or causing the diagnosed symptom; never guess a flag name.
Return one JSON object:
{"actions": [{"action": "<name>", "parameters": {...}, "reason": "<one sentence>",
              "citations": [{"knowledge_id": "K<n>", "quote": "<an EXACT substring of that knowledge item>"}]}],
 "no_action_reason": "<set only when no allowed action fits>"}
At most 3 actions, in execution order. Every action needs at least one citation; quotes are checked by code and an
action whose citations do not check out is dropped."""


def plan_hash(incident_id: str, d: Diagnosis, actions: list[ProposedAction]) -> str:
    shown = {"incident": incident_id, "diagnosis": d.summary,
             "actions": [{"action": a.action, "parameters": a.parameters, "reason": a.reason,
                          "citations": [c.model_dump() for c in a.citations]} for a in actions]}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()


RUNBOOK_FILE = Path(__file__).resolve().parents[1] / "runbook.yaml"

# words that tie a flag-guarded code path to a kind of failure, with weights (ranks the flags a service's code
# evaluates by the code right around each one)
SYMPTOM_WORDS: dict[str, dict[str, int]] = {
    "application_error": {"fail": 3, "error": 2, "throw": 2, "exception": 2, "raise": 2, "unavailable": 2,
                          "bad": 1, "invalid": 1},
    "dependency_failure": {"unreachable": 3, "address": 2, "connect": 2, "bad": 1, "client": 1},
    "resource_cpu": {"cpu": 3, "load": 2, "busy": 2, "spin": 2, "loop": 1, "gc": 1, "garbage": 1},
    "resource_memory": {"leak": 3, "cache": 3, "memory": 3, "heap": 2, "alloc": 2, "gc": 1, "garbage": 1},
    "network": {"slow": 3, "sleep": 3, "delay": 3, "latency": 2, "lock": 2, "contention": 2, "timeout": 1},
    "traffic": {"flood": 3, "users": 2, "spawn": 2, "homepage": 1, "count": 1},
}


def load_runbook(path: Path = RUNBOOK_FILE) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())


def rule_actions(d: Diagnosis, evidence: list[Evidence], catalog: list[ActionDef],
                 knowledge: Optional[list[KnowledgeItem]] = None, runbook: Optional[dict] = None,
                 normal_users: int = 5, operations: Optional[dict[str, str]] = None
                 ) -> tuple[list[ProposedAction], list[str]]:
    """(actions, notes). Every action cites the retrieved knowledge it rests on. Notes explain root causes no
    allowed action can fix (they become the escalation reason). `operations`: service -> failing operation name."""
    names = {a.name for a in catalog}
    rb = runbook if runbook is not None else load_runbook()
    kn = knowledge or []
    facts = extract(evidence)
    out: list[ProposedAction] = []
    notes: list[str] = []

    def runbook_cite(word: str, category: str) -> list[Citation]:
        for k in kn:
            if k.source == "runbook" and k.data.get("category") == category:
                for sentence in re.split(r"(?<=[.:])\s+", k.content):
                    if word in sentence and len(sentence) >= 12:
                        return [Citation(knowledge_id=k.id, quote=sentence.strip())]
        return []

    def add(action: str, reason: str, citations: list[Citation], **params) -> bool:
        if action in names and not any(p.action == action and p.parameters == params for p in out):
            out.append(ProposedAction(action=action, parameters=params, reason=reason, citations=citations))
            return True
        return False

    def guarding_flags(rc) -> list[tuple[str, Citation, str]]:
        """Flags that decide whether the code raising this root cause's error runs: (flag, citation, where)."""
        quotes = " ".join(c.quote for c in rc.claims).lower()
        found = []
        for k in kn:
            dt = k.data
            if k.source != "code" or dt.get("kind") not in ("error_match", "op_guard") or not dt.get("guards"):
                continue
            if dt["kind"] == "op_guard" and dt.get("service") != rc.component:
                continue  # a flag in the handler of this component's failing operation
            if dt["kind"] == "error_match" and dt.get("service") != rc.component \
                    and dt.get("needle", "").lower() not in quotes:
                continue
            for g in dt["guards"]:
                found.append((g["flag"], Citation(knowledge_id=k.id, quote=g["text"]), f"{dt['path']}:{dt['line']}"))
        return found

    def related_flags(svc: str, category: str) -> list[tuple[str, Citation, str]]:
        """Flags the service's code evaluates whose own name and nearby code match the diagnosed symptom best."""
        words = SYMPTOM_WORDS.get(category, {})
        op = (operations or {}).get(svc, "")
        scored = []
        for k in kn:
            if k.source != "code" or k.data.get("kind") != "flag_sites" or k.data.get("service") != svc:
                continue
            for s in k.data["sites"]:
                near, name = s["near"].lower(), " ".join(_words(s["flag"]))
                score = sum(w * (near.count(t) + 2 * name.count(t)) for t, w in words.items())
                if op and op.lower() in s["context"].lower():
                    score += 6  # the flag is read in the handler of the operation that fails
                scored.append((score, s["flag"], Citation(knowledge_id=k.id, quote=s["text"]),
                               f"{s['path']}:{s['line']}"))
        best = max((x[0] for x in scored), default=0)
        return list({f: (f, c, w) for sc, f, c, w in scored if best > 0 and sc == best}.values())

    def flags_off(chosen: list[tuple[str, Citation, str]], why: str, category: str) -> bool:
        added = False
        for flag, cite, where in chosen:
            rb_cites = runbook_cite("toggle_feature_flag", category) or runbook_cite("flag", category)
            added = add("toggle_feature_flag", f"{why}: the code at {where} runs only when {flag} is on; switch it off",
                        [cite] + rb_cites, flag=flag, enabled=False) or added
        return added

    def on_the_wire(svc: str) -> bool:
        return any(f.kind == "gap" and f.service == svc and f.values["gap_ms"] >= 200 for f in facts)

    for rc in d.root_causes:
        svc, cat = rc.component, rc.category
        c = latest(facts, "container", svc)
        r = latest(facts, "resources", svc)
        cpu_limit = c.values.get("cpu_limit") if c else None
        mem_limit = c.values.get("memory_limit_mb") if c else None
        starved_mem = bool(c and c.values["oom"] and mem_limit and mem_limit <= rb.get("starved_memory_limit_mb", 64))
        restart_cite = runbook_cite("restart_service", cat)
        if cat == "network" and c and c.values["networks"] == "none":
            notes.append(f"{svc} is detached from the network; no allowed action reattaches it")
        elif cat in ("dependency_failure", "network", "container_down") and not starved_mem and \
                any(k.data.get("kind") == "op_guard" and k.data.get("service") == svc for k in kn) and guarding_flags(rc):
            flags_off(guarding_flags(rc), f"{svc}'s failing operation takes a flag-guarded branch", cat)
        elif cat in ("dependency_failure", "network") and guarding_flags(rc):
            flags_off(guarding_flags(rc), f"{svc}'s failing calls come from a flag-guarded code path", cat)
        elif cat == "network" and not on_the_wire(svc) and related_flags(svc, cat):
            flags_off(related_flags(svc, cat), f"{svc}'s own spans are slow (no network gap): a slow code path", cat)
        elif cat in ("container_down", "dependency_failure", "network") and not starved_mem:
            what = {"network": "its network interface carries the latency/loss; a restart recreates it",
                    "container_down": "it is not running", "dependency_failure": "its dependents cannot reach it"}[cat]
            add("restart_service", f"{svc}: {what}", restart_cite, service=svc)
        elif cat in ("resource_memory", "container_down") and starved_mem:
            notes.append(f"{svc} is OOM-killed under a {mem_limit:.0f} MB limit; no allowed action raises limits")
        elif cat == "resource_memory":
            flags_off(guarding_flags(rc) or related_flags(svc, cat), f"{svc}'s memory grows without bound", cat)
            add("restart_service", f"{svc} has leaked memory; restart it to reclaim it", restart_cite, service=svc)
        elif cat == "resource_cpu":
            if r and r.values["throttled"] > 0 and cpu_limit and cpu_limit <= rb.get("starved_cpu_limit", 0.25):
                notes.append(f"{svc} is throttled under a {cpu_limit:g}-CPU limit; no allowed action raises limits"
                             + ("; scale_service needs Kubernetes" if "scale_service" in names else ""))
            elif not flags_off(guarding_flags(rc) or related_flags(svc, cat), f"{svc} burns CPU in its own code", cat):
                add("restart_service", f"{svc} is CPU-bound; restart it (a mitigation: the cause is in its code)",
                    restart_cite, service=svc)
        elif cat == "application_error":
            if not flags_off(guarding_flags(rc) or related_flags(svc, cat), f"{svc}'s own code fails requests", cat):
                add("restart_service", f"{svc} fails requests and no flag guards the failing code; restart it as a "
                    "mitigation", restart_cite or runbook_cite("restart", cat), service=svc)
        elif cat == "traffic":
            add("throttle_load_generator", f"a request flood enters through {svc}; bring the load generator back to "
                f"its normal {normal_users} users", runbook_cite("throttle_load_generator", cat), users=normal_users)
            flags_off(related_flags("load-generator", cat), "the load generator floods the entry point", cat)
        else:
            add("restart_service", f"{svc} is failing for an unclear reason; restart it", restart_cite, service=svc)
    # a fix that worked for the same component and kind of failure before, and is not proposed yet
    for k in kn:
        e = k.data
        if k.source != "memory" or not e.get("recovered"):
            continue
        for rc in d.root_causes:
            if rc.component in e["components"] and rc.category in e["categories"]:
                for a in e["actions"]:
                    if not any(p.action == a["action"] for p in out):
                        add(a["action"], f"the same failure of {rc.component} recovered after this in {e['incident']}",
                            [Citation(knowledge_id=k.id, quote=action_key(a["action"], a["parameters"]))],
                            **a["parameters"])
    return out, notes


class Planner:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.knowledge = Knowledge(ctx.settings.demo_dir, ctx.settings.data_dir)

    def run(self, inc: Incident, d: Diagnosis, evidence: list[Evidence]) -> Plan:
        with self.ctx.status.working("planner", inc.id, "retrieving knowledge and choosing a runbook action"):
            try:
                catalog = self.ctx.actions.actions()
            except ActionAPIError as exc:
                return Plan(actions=[], change_hash="", no_action_reason=f"Action API unavailable: {exc}")
            items = self.knowledge.gather(inc, d, evidence)
            inc.knowledge = items
            for k in items:
                self.ctx.bus.publish("planner", "tool", f"{k.id} [{k.source}] {k.ref}: {k.summary}", incident_id=inc.id,
                                     detail={"knowledge_id": k.id, "source": k.source, "ref": k.ref})
            produced_by = "rules"
            actions: list[ProposedAction] = []
            notes: list[str] = []
            if self.ctx.llm.enabled:
                actions = self._model(inc, d, evidence, catalog, items) or []
                produced_by = "llm" if actions else "rules"
            if not actions:
                ops = failing_operations(inc.alerts)
                actions, notes = rule_actions(d, evidence, catalog, items, normal_users=self.ctx.settings.normal_load_users,
                                              operations=ops)
            services = self.ctx.services() or None
            history = [k.data for k in items if k.source == "memory"]
            valid: list[ProposedAction] = []
            for a in actions[: self.ctx.settings.max_plan_actions]:
                errs = validate(a.action, a.parameters, catalog, services) + check_citations(a, items)
                if not a.citations:
                    errs.append("cites no retrieved knowledge")
                if errs:
                    self.ctx.bus.publish("planner", "step", f"dropped {a.action}: {'; '.join(errs)}", incident_id=inc.id)
                    continue
                a.reversible = next((c.reversible for c in catalog if c.name == a.action), False)
                a.track_record = Memory.track_record(a.action, a.parameters, history)
                try:
                    prev = self.ctx.actions.execute(a.action, a.parameters, a.reason, dry_run=True, incident_id=inc.id)
                except ActionAPIError as exc:
                    prev = None
                    self.ctx.bus.publish("planner", "error", f"preview of {a.action} failed: {exc}", incident_id=inc.id)
                if prev is not None and not prev.ok:
                    self.ctx.bus.publish("planner", "step", f"Action API refused {a.action}: {prev.message}",
                                         incident_id=inc.id)
                    continue
                a.preview = prev.message if prev else None
                a.dry_run = self.ctx.safety.dry_run
                valid.append(a)
            reason = "" if valid else ("; ".join(notes) or "no allowed action fits this diagnosis")
            if valid and notes:
                inc.limitations.extend(notes)
            plan = Plan(actions=valid, change_hash=plan_hash(inc.id, d, valid), produced_by=produced_by,  # type: ignore[arg-type]
                        no_action_reason=reason)
        for a in valid:
            self.ctx.bus.publish("planner", "step", f"proposes {a.action} {json.dumps(a.parameters)}: {a.reason}"
                                 + (f" [cites {', '.join(c.knowledge_id for c in a.citations)}]" if a.citations else ""),
                                 incident_id=inc.id, detail={"preview": a.preview, "track_record": a.track_record})
        return plan

    def _model(self, inc: Incident, d: Diagnosis, evidence: list[Evidence], catalog: list[ActionDef],
               items: list[KnowledgeItem]) -> Optional[list[ProposedAction]]:
        facts = [f.line for f in extract(evidence) if f.kind in ("container", "resources")
                 and f.service in {r.component for r in d.root_causes}][-6:]
        user = (f"Diagnosis: {d.model_dump_json(include={'summary', 'root_causes'})}\n\nContainer facts:\n"
                + "\n".join(facts)
                + f"\nThe load generator's normal user count is {self.ctx.settings.normal_load_users}."
                + "\nIf a root cause is a resource limit or a detached network, no allowed action fixes it: say so in "
                  "no_action_reason.\n\nAllowed actions:\n" + json.dumps([a.model_dump() for a in catalog], indent=1)
                + "\n\nRetrieved knowledge (feature-flag values are unknown and must not be assumed):\n\n"
                + (render(items) or "(nothing retrieved)"))
        data = self.ctx.llm.json(SYSTEM, user, max_tokens=1200)
        if not data or not isinstance(data.get("actions"), list):
            return None
        by_id = {k.id: k for k in items}
        out = []
        for a in data["actions"]:
            if isinstance(a, dict) and a.get("action") and a.get("reason"):
                cites = [Citation(knowledge_id=str(c.get("knowledge_id", "")), quote=str(c.get("quote", "")))
                         for c in a.get("citations") or [] if isinstance(c, dict)]
                pa = ProposedAction(action=str(a["action"]), parameters=dict(a.get("parameters") or {}),
                                    reason=str(a["reason"])[:300], citations=cites)
                bad = check_citations(pa, items)
                pa.citations = [c for c in cites if c.knowledge_id in by_id and quote_in(by_id[c.knowledge_id], c.quote)]
                if bad:
                    self.ctx.bus.publish("planner", "step", "model citations rejected: " + "; ".join(bad)[:300],
                                         incident_id=inc.id)
                if pa.citations:
                    out.append(pa)
        return out or None
