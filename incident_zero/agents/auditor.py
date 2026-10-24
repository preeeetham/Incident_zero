"""Agent 4, the Evidence Auditor: plain code between the Diagnostician and everything that acts (REQUIREMENTS F3.3,
F3.4, R5). Cited ids must exist, quotes must appear verbatim, components must be real, every active alert must be
explained. A failed audit goes back to the Investigator with the list of problems."""
from __future__ import annotations

from ..evidence import audit
from ..models import AuditResult, Diagnosis, Evidence, Incident
from .context import Context


class Auditor:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, inc: Incident, d: Diagnosis, evidence: list[Evidence]) -> AuditResult:
        with self.ctx.status.working("auditor", inc.id, f"checking {sum(len(r.claims) for r in d.root_causes)} claims"):
            known = self.ctx.services() or None
            res = audit(d, evidence, inc.alerts, known)
        verdict = "passed" if res.passed else f"rejected: {'; '.join(res.problems)[:300]}"
        self.ctx.bus.publish("auditor", "step", f"audit of {res.checked_claims} claims {verdict}", incident_id=inc.id,
                             detail=res.model_dump())
        return res
