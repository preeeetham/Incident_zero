"""The casebook: every incident the agents closed, whatever the outcome, in data_dir/casebook.jsonl.

Incident memory (knowledge/memory.py) keeps only fixes that ran live and were judged, because the Planner cites it.
The casebook keeps everything (resolved, escalated, rejected, false alarms, dry runs), with what the model cost, so
the Brain can tell which incidents it has already solved and what solving them took.

An incident is "known" when its alerts match a past incident's (Jaccard similarity of the (rule, service) pairs)
and that past incident was resolved live with recovery verified, and the latest live attempt on those alerts did
not fail. Nothing else counts: a dry run, a rollback or an escalation never teaches a fix.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..models import Incident, iso

_lock = threading.Lock()


def alert_keys(inc: Incident) -> list[str]:
    """The incident's fingerprint: its alert rules and the services they fired on."""
    return sorted({f"{a.rule}:{a.service}" for a in inc.alerts})


def similarity(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


@dataclass
class Case:
    """A past incident whose verified fix may be reused."""
    entry: dict[str, Any]
    similarity: float
    successes: int
    attempts: int

    @property
    def incident(self) -> str:
        return self.entry["incident"]

    @property
    def track_record(self) -> str:
        return f"recovered {self.successes} of {self.attempts} past incidents with these alerts"


class Casebook:
    def __init__(self, root: str | Path):
        self.file = Path(root) / "casebook.jsonl"

    def entries(self) -> list[dict[str, Any]]:
        if not self.file.is_file():
            return []
        out = []
        for line in self.file.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def record(self, inc: Incident) -> Optional[dict[str, Any]]:
        """Store a closed incident. Each incident is stored once (a report rewrite does not duplicate it)."""
        if not inc.terminal or any(e["incident"] == inc.id for e in self.entries()):
            return None
        d = inc.diagnosis
        rcs = d.root_causes if d else []
        live = [r for r in inc.execution.results if r.ok and not r.dry_run]
        recall = inc.recall
        entry = {
            "incident": inc.id, "at": iso(), "outcome": inc.phase, "alerts": alert_keys(inc),
            "components": [r.component for r in rcs], "categories": [r.category for r in rcs],
            "causes": [r.cause for r in rcs], "diagnosed_by": d.produced_by if d else None,
            "planned_by": inc.plan.produced_by if inc.plan else None,
            "actions": [{"action": r.action, "parameters": r.parameters} for r in live],
            "dry_run": bool(inc.execution.results) and all(r.dry_run for r in inc.execution.results),
            "recovered": inc.verification.recovered, "rolled_back": inc.execution.rollback_performed,
            "mode": "recalled" if recall and recall.status == "confirmed" else "reasoned",
            "recall_of": recall.case if recall and recall.status == "confirmed" else None,
            "tokens": {"calls": inc.tokens.calls, "total": inc.tokens.total, "estimated": inc.tokens.estimated},
            "tokens_saved": recall.tokens_saved if recall and recall.status == "confirmed" else 0,
        }
        with _lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    @staticmethod
    def solved(e: dict[str, Any]) -> bool:
        return e["outcome"] == "resolved" and e.get("recovered") is True and bool(e["actions"])

    def known(self, keys: list[str], min_similarity: float = 0.6, min_successes: int = 1) -> Optional[Case]:
        """The best past incident to reuse for alerts `keys`, or None."""
        if not keys:
            return None
        entries = self.entries()
        scored = [(similarity(keys, e["alerts"]), i, e) for i, e in enumerate(entries)]
        close = [(s, i, e) for s, i, e in scored if s >= min_similarity]
        tried = [x for x in close if x[2]["actions"] and not x[2].get("dry_run")]  # a fix ran live
        if not tried:
            return None
        latest = max(tried, key=lambda x: x[1])
        if not self.solved(latest[2]):
            return None  # the last time a fix ran on these alerts it did not work: think again
        successes = [x for x in tried if self.solved(x[2])]
        if len(successes) < min_successes:
            return None
        best = max(successes, key=lambda x: (x[0], x[1]))  # most similar, then most recent
        same_fix = [x for x in tried if x[2]["actions"] == best[2]["actions"]]
        return Case(entry=best[2], similarity=round(best[0], 2),
                    successes=sum(1 for x in same_fix if self.solved(x[2])), attempts=len(same_fix))

    def cost_of(self, case: Case) -> tuple[int, int, bool]:
        """(model calls, tokens, estimated) that reasoning out this case took: its own measurement when it was
        reasoned, otherwise the original incident it was recalled from, otherwise the average reasoned incident."""
        by_id = {e["incident"]: e for e in self.entries()}
        e: Optional[dict] = case.entry
        seen: set[str] = set()
        while e is not None and e.get("mode") == "recalled" and e["incident"] not in seen:
            seen.add(e["incident"])
            e = by_id.get(e.get("recall_of") or "")
        if e is not None and e.get("mode") != "recalled" and e["tokens"]["calls"]:
            return e["tokens"]["calls"], e["tokens"]["total"], bool(e["tokens"].get("estimated"))
        reasoned = [x["tokens"] for x in by_id.values() if x.get("mode") != "recalled" and x["tokens"]["calls"]]
        if not reasoned:
            return 0, 0, True
        return (round(sum(t["calls"] for t in reasoned) / len(reasoned)),
                round(sum(t["total"] for t in reasoned) / len(reasoned)), True)

    def stats(self) -> dict[str, Any]:
        es = self.entries()
        recalled = [e for e in es if e.get("mode") == "recalled"]
        return {
            "incidents": len(es),
            "by_outcome": {o: sum(1 for e in es if e["outcome"] == o) for o in sorted({e["outcome"] for e in es})},
            "known_fixes": len({json.dumps(e["alerts"]) for e in es if self.solved(e)}),
            "recalled": len(recalled),
            "model_calls": sum(e["tokens"]["calls"] for e in es),
            "tokens_used": sum(e["tokens"]["total"] for e in es),
            "tokens_saved": sum(e.get("tokens_saved", 0) for e in recalled),
        }
