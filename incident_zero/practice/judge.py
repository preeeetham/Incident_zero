"""The Judge: scores what the agents did against a scenario's hidden ground truth (REQUIREMENTS P3).

Out of 10: detected (1), right component (3), right category (1), evidence passed the audit (2), an appropriate
allowed action (2), recovery verified (1). For combination scenarios (`all_components`), the component points are
split across the components that had to be named. For faults no allowed action can fix (`escalate: true`), the
action points go to proposing nothing and escalating, and the recovery point to escalating at all.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..models import Incident


def _ts(s: Optional[str]) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def score(truth: dict, started_at: float, incidents: list[Incident]) -> dict:
    mine = [i for i in incidents if _ts(i.created_at) >= started_at - 5]
    out: dict = {"incidents": [i.id for i in mine], "points": 0.0, "max": 10, "detail": {}}
    d = out["detail"]
    if not mine:
        d["detected"] = False
        return out
    d["detected"] = True
    out["points"] += 1
    d["time_to_detect_s"] = round(min(_ts(i.created_at) for i in mine) - started_at, 1)
    rcs = [rc for i in mine if i.diagnosis for rc in i.diagnosis.root_causes]
    named = {rc.component for rc in rcs}
    want = set(truth.get("components", []))
    if truth.get("all_components"):
        hit = len(named & want) / max(len(want), 1)
    else:
        hit = 1.0 if named & want else 0.0
    out["points"] += 3 * hit
    d["components_named"], d["component_score"] = sorted(named), round(hit, 2)
    cat = any(rc.category in truth.get("categories", []) for rc in rcs if rc.component in want)
    out["points"] += 1 if cat else 0
    d["category_ok"] = cat
    audited = any(i.audits and i.audits[-1].passed for i in mine)
    out["points"] += 2 if audited else 0
    d["evidence_passed_audit"] = audited
    actions = {a.action for i in mine if i.plan for a in i.plan.actions}
    executed = [r for i in mine for r in i.execution.results if r.ok and not r.dry_run]
    if truth.get("escalate"):
        # no allowed action fixes this fault: the right move is to propose nothing and escalate
        act_ok = not actions and any(i.phase == "escalated" for i in mine)
    elif truth.get("fixes"):
        # strict: the exact fix (action + parameters) must have been executed, not just a same-named action
        # (found live: toggling the wrong flag scored full marks)
        act_ok = any(r.action == f["action"] and all(r.parameters.get(k) == v for k, v in f.get("parameters", {}).items())
                     for r in executed for f in truth["fixes"])
        d["fix_executed"] = act_ok
    else:
        act_ok = bool(actions & set(truth.get("actions", [])))
    out["points"] += 2 if act_ok else 0
    d["actions"], d["action_ok"] = sorted(actions), act_ok
    # recovery counts only when the right fix was what ran (a recovery after the wrong action is a coincidence)
    rec = (any(i.verification.recovered for i in mine) and (act_ok or not truth.get("fixes"))) or \
        (bool(truth.get("escalate")) and act_ok)
    out["points"] += 1 if rec else 0
    d["recovered"] = rec
    diag_times = [_ts(i.diagnosis.created_at) for i in mine if i.diagnosis]
    if diag_times:
        d["time_to_diagnosis_s"] = round(min(diag_times) - started_at, 1)
    d["phases"] = [i.phase for i in mine]
    out["points"] = round(out["points"], 2)
    return out
