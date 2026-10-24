"""Agent 7, the Executor: runs the approved plan through the Action API and nothing else (REQUIREMENTS F6.1, S3).

The kill switch is checked before every call. The mode (dry run or live) is read at execution time, so switching
to dry run after approval still takes effect. A failed action stops the rest of the plan.
"""
from __future__ import annotations

from ..actions.client import ActionAPIError
from ..models import ActionResult, Incident
from .context import Context


class Executor:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, inc: Incident) -> Incident:
        assert inc.plan is not None and inc.approval.decision == "approved", "executor needs an approved plan"
        dry = self.ctx.safety.dry_run
        inc.execution.attempted = True
        with self.ctx.status.working("executor", inc.id, f"{'dry run of' if dry else 'executing'} "
                                     f"{len(inc.plan.actions)} action(s)"):
            for a in inc.plan.actions:
                self.ctx.safety.check()
                try:
                    r = self.ctx.actions.execute(a.action, a.parameters, a.reason, dry_run=dry, incident_id=inc.id)
                except ActionAPIError as exc:
                    r = ActionResult(action=a.action, parameters=a.parameters, ok=False, dry_run=dry, message=str(exc))
                inc.execution.results.append(r)
                self.ctx.store.save(inc)
                self.ctx.bus.publish("executor", "action", f"{'DRY RUN ' if dry else ''}{a.action} "
                                     f"{a.parameters}: {'ok' if r.ok else 'FAILED'} - {r.message}", incident_id=inc.id,
                                     detail=r.model_dump())
                if not r.ok:
                    inc.execution.notes = f"{a.action} failed: {r.message}; remaining actions skipped"
                    break
        return inc
