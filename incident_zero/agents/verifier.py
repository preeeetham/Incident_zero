"""Agent 8, the Verifier: confirms recovery on the signals that raised the incident (REQUIREMENTS F6.2, F6.3).

After a settle period it re-evaluates the Sentinel's rules with a short rate window, until none of the incident's
(rule, service) signals fire for `verify_consecutive` checks in a row, or the window ends. Not recovered: roll back
every reversible action through the Action API, then escalate. A dry run changes nothing, so it is not verified.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from .. import notify
from ..actions.client import ActionAPIError
from ..models import ActionResult, Incident, iso
from .context import Context
from ..telemetry import queries as Q
from ..telemetry.backends import BackendError
from .sentinel import RULE_LABEL, evaluate


class Verifier:
    def __init__(self, ctx: Context, learned: Optional[dict] = None):
        self.ctx = ctx
        self.learned = learned if learned is not None else {}

    def run(self, inc: Incident) -> Incident:
        v = inc.verification
        executed = [r for r in inc.execution.results if r.ok and not r.dry_run]
        if not executed:
            v.checked, v.recovered = False, None
            v.notes = ("dry run: the Action API previewed the plan, nothing changed, so there is no recovery to verify"
                       if inc.execution.results and all(r.dry_run for r in inc.execution.results)
                       else "no action succeeded; nothing to verify")
            return inc
        keys = {(a.rule, a.service) for a in inc.alerts}
        v.signals_checked = [f"{RULE_LABEL.get(r, r)} on {s}" for r, s in sorted(keys)]
        s = self.ctx.settings
        # A failing operation can be rare (cart's EmptyCart, ~2 calls/min): it is checked over the whole time since
        # the fix, and is clear only once it has served enough calls without failing (found live: a 1-minute window
        # saw no calls at all, so a restart that fixed nothing was recorded as "recovered").
        ops = [a for a in inc.alerts if a.rule == "operation_errors"]
        fixed_at = time.time()
        with self.ctx.status.working("verifier", inc.id, "checking recovery"):
            self._sleep(s.verify_settle_s)
            window_s = s.verify_window_s + (s.op_verify_extra_s if ops else 0)
            clear, deadline = 0, time.time() + window_s
            window = "1m"
            while True:
                self.ctx.safety.check()
                since = f"{max(120, int(time.time() - fixed_at))}s"
                signals, _ = evaluate(self.ctx, {k: x for k, x in self.learned.items()}, window=window, verify=True,
                                      op_window=since if ops else None)
                firing = sorted({f"{RULE_LABEL.get(sg.rule, sg.rule)} on {sg.service}: {sg.signal}"
                                 for sg in signals if sg.key in keys} | set(self._restarted_since(inc, fixed_at)))
                waiting = [] if firing else self._too_few_calls(ops, since)
                v.samples.append({"at": iso(), "firing": firing, "waiting": waiting})
                clear = 0 if firing else (clear if waiting else clear + 1)
                self.ctx.bus.publish("verifier", "verify", ("still firing: " + "; ".join(firing)) if firing
                                     else ("no failures yet, waiting for calls: " + "; ".join(waiting)) if waiting
                                     else f"signals clear ({clear}/{s.verify_consecutive})", incident_id=inc.id)
                if clear >= s.verify_consecutive:
                    v.checked, v.recovered = True, True
                    v.notes = f"all {len(keys)} triggering signals clear for {clear} consecutive checks"
                    return inc
                if time.time() >= deadline:
                    break
                self._sleep(min(s.sentinel_interval_s, max(deadline - time.time(), 0)))
        v.checked, v.recovered = True, False
        v.notes = "signals still firing at the end of the verification window: " + "; ".join(v.samples[-1]["firing"])
        self.rollback(inc)
        return inc

    def _restarted_since(self, inc: Incident, fixed_at: float) -> list[str]:
        """Containers of the incident's container alerts that are down, or that started again after the fix (found
        live: a container killed for 2 s every 22 s looked "running" at both 15 s checks, so a restart that fixed
        nothing was verified). The fix's own restart is allowed for `container_restart_grace_s`."""
        out = []
        for svc in sorted({a.service for a in inc.alerts if a.rule in ("container_down", "container_restarts")}):
            try:
                c = self.ctx.telemetry.docker.container(svc)
                st = self.ctx.telemetry.docker.status(c) if c is not None else None
            except Exception:
                continue
            if st is None:
                continue
            if not st["running"] or st["uptime_s"] is None:
                out.append(f"Container down on {svc}: state={st['state']}")
            elif time.time() - st["uptime_s"] > fixed_at + self.ctx.settings.container_restart_grace_s:
                out.append(f"Container restarting on {svc}: started again {st['uptime_s']}s ago, after the fix")
        return out

    def _too_few_calls(self, ops: list, window: str) -> list[str]:
        """The failing operations that have not yet served op_verify_min_calls calls since the fix."""
        if not ops:
            return []
        try:
            calls = self.ctx.telemetry.prom.vector_by(Q.op_calls(window), ("service_name", "span_name"))
        except BackendError:
            return [f"{a.service}: span metrics unavailable" for a in ops]
        out = []
        for a in ops:
            m = re.match(rf"^{re.escape(a.service)} (.+?) failed ", a.signal)
            n = sum(v for (svc, op), v in calls.items() if svc == a.service and (m is None or op == m.group(1)))
            if n < self.ctx.settings.op_verify_min_calls:
                out.append(f"{a.service} {m.group(1) if m else 'operation'} {n:.0f} calls over {window}")
        return out

    def rollback(self, inc: Incident) -> None:
        for r in [r for r in inc.execution.results if r.ok and not r.dry_run and r.execution_id]:
            try:
                rb = self.ctx.actions.rollback(r.execution_id)
            except ActionAPIError as exc:
                rb = ActionResult(action="rollback", execution_id=r.execution_id, ok=False, dry_run=False,
                                  message=str(exc))
            inc.execution.rollback_results.append(rb)
            if rb.ok and "not reversible" not in rb.message:
                inc.execution.rollback_performed = True
            self.ctx.bus.publish("verifier", "action", f"rollback of {r.action}: {rb.message}", incident_id=inc.id,
                                 detail=rb.model_dump())
        inc.escalation_reason = "recovery not confirmed after the approved action; rolled back where possible"
        self.ctx.bus.publish("verifier", "step", notify.send(self.ctx.settings, f"[Incident Zero] ESCALATION {inc.id}",
                                                             f"{inc.title}\n\n{inc.verification.notes}"),
                             incident_id=inc.id)

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.ctx.safety.check()
            time.sleep(min(1.0, max(end - time.time(), 0)))
