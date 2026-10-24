"""Knowledge source 3: incident memory. After an action has run live and the Verifier has judged it, the outcome is
appended to data_dir/memory.jsonl: what was wrong, what was done, and whether the system recovered. Only the
agents' own verified outcomes are stored, never practice ground truth. The Planner retrieves similar past incidents
to propose a fix that worked before, and to say how often it did."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from ..models import Incident, iso
from .search import BM25

_lock = threading.Lock()


def signature(components: list[str], categories: list[str], text: str) -> str:
    return f"{' '.join(components)} {' '.join(categories)} {text}"


def action_key(action: str, parameters: dict) -> str:
    return f"{action} {json.dumps(parameters, sort_keys=True)}"


class Memory:
    def __init__(self, root: str | Path):
        self.file = Path(root) / "memory.jsonl"

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

    def record(self, inc: Incident) -> Optional[dict]:
        """Store the outcome of a live, verified fix. Dry runs and incidents nothing was executed for teach nothing."""
        live = [r for r in inc.execution.results if r.ok and not r.dry_run]
        d = inc.diagnosis
        if not live or not d or not d.root_causes or not inc.verification.checked or inc.verification.recovered is None:
            return None
        if any(e["incident"] == inc.id for e in self.entries()):
            return None
        rcs = d.root_causes
        error_text = " ".join(c.quote for rc in rcs for c in rc.claims)[:600]
        entry = {"incident": inc.id, "at": iso(), "components": [r.component for r in rcs],
                 "categories": [r.category for r in rcs], "causes": [r.cause for r in rcs],
                 "alerts": sorted({f"{a.rule} {a.service}" for a in inc.alerts}), "evidence_text": error_text,
                 "actions": [{"action": r.action, "parameters": r.parameters} for r in live],
                 "recovered": bool(inc.verification.recovered), "rolled_back": inc.execution.rollback_performed,
                 "verification": inc.verification.notes[:300]}
        with _lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def similar(self, components: list[str], categories: list[str], text: str, k: int = 3) -> list[dict]:
        """Past incidents on the same component or with the same kind of cause, most similar first."""
        entries = [e for e in self.entries()
                   if set(e["components"]) & set(components) or set(e["categories"]) & set(categories)]
        if not entries:
            return []
        bm = BM25([signature(e["components"], e["categories"], " ".join(e["causes"]) + " " + " ".join(e["alerts"])
                             + " " + e.get("evidence_text", "")) for e in entries])
        ranked = bm.top(signature(components, categories, text), k, min_score=0.0)
        return [entries[i] for i, _ in ranked]

    @staticmethod
    def render(e: dict) -> str:
        acts = "; ".join(action_key(a["action"], a["parameters"]) for a in e["actions"])
        outcome = "recovered" if e["recovered"] else "did NOT recover" + (" (rolled back)" if e["rolled_back"] else "")
        return (f"{e['incident']}: {', '.join(f'{c} ({k})' for c, k in zip(e['components'], e['categories']))}: "
                f"{' / '.join(e['causes'])}\nactions: {acts}\noutcome: {outcome}\nverification: {e['verification']}")

    @staticmethod
    def track_record(action: str, parameters: dict, entries: list[dict]) -> str:
        key = action_key(action, parameters)
        used = [e for e in entries if any(action_key(a["action"], a["parameters"]) == key for a in e["actions"])]
        if not used:
            return ""
        ok = [e["incident"] for e in used if e["recovered"]]
        bad = [e["incident"] for e in used if not e["recovered"]]
        return (f"recovered {len(ok)} of {len(used)} similar past incidents"
                + (f" ({', '.join(ok)})" if ok else "") + (f"; did not recover in {', '.join(bad)}" if bad else ""))
