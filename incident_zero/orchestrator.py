"""The incident lifecycle: one worker thread per incident, moving it phase by phase between the agents, saving
after every phase (REQUIREMENTS N2).

  detected -> investigating -> diagnosing -> auditing --fail (retries left)--> investigating
                                                 | pass
                                              planning --no fitting action--> escalated
                                                 |
                                          awaiting_approval --rejected--> rejected
                                                 |  --sent back--> investigating (with the human's note)
                                                 |  --timeout--> escalated
                                              executing -> verifying --recovered--> resolved
                                                                     --dry run----> dry_run_complete
                                                                     --not recovered (rolled back)--> escalated
  every terminal phase -> Reporter, incident memory and the casebook

The agents are grouped in three layers: the Eyes (eyes/: sentinel, investigator, diagnostician, auditor), the Hands
(hands/: planner, approval, executor, verifier, reporter) and the Brain (brain/: the model, knowledge, the casebook
and the token meter). On a known incident (brain.recall) the same phases run with no model calls: the playbook
investigates, the rules diagnose and must agree with the case, the audit still checks, the case's verified fix is
planned and a human still approves. Any disagreement abandons recall and the full pipeline takes over.

The kill switch pauses a worker where it stands (agents raise Frozen) until resumed. A restart resumes open
incidents; an incident whose actions were already attempted goes to verification, never to execution again.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .agents.context import Context
from .agents.diagnostician import drop_unsupported, rule_diagnosis
from .brain import Brain
from .brain.meter import bound
from .brain.recall import Recall
from .evidence import EvidenceLedger
from .eyes import Eyes
from .hands import Hands
from .models import ApprovalRecord, Incident, iso
from .safety import Frozen


class Orchestrator:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.eyes = Eyes(ctx, self.open)
        self.brain = Brain(ctx)
        self.hands = Hands(ctx, self.brain, self.eyes.sentinel.learned)
        # the agents by name, as the CLI, server and tests address them
        self.sentinel, self.investigator = self.eyes.sentinel, self.eyes.investigator
        self.diagnostician, self.auditor = self.eyes.diagnostician, self.eyes.auditor
        self.planner, self.approval, self.executor = self.hands.planner, self.hands.approval, self.hands.executor
        self.verifier, self.reporter = self.hands.verifier, self.hands.reporter
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._sentinel_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self, sentinel: bool = True) -> None:
        for inc in self.ctx.store.open_incidents():
            self.ctx.bus.publish("system", "system", f"resuming {inc.id} at {inc.phase}", incident_id=inc.id)
            self.open(inc.id)
        if sentinel:
            self._sentinel_thread = threading.Thread(target=self.sentinel.run, name="sentinel", daemon=True)
            self._sentinel_thread.start()

    def stop(self) -> None:
        self.sentinel.stop()

    def open(self, incident_id: str) -> None:
        with self._lock:
            t = self._workers.get(incident_id)
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self.run_incident, args=(incident_id,), name=incident_id, daemon=True)
            self._workers[incident_id] = t
            t.start()

    def join(self, incident_id: str, timeout: float = 30) -> None:
        t = self._workers.get(incident_id)
        if t is not None:
            t.join(timeout)

    # ------------------------------------------------------------ the worker
    def run_incident(self, incident_id: str) -> None:
        inc = self.ctx.store.load(incident_id)
        if inc is not None:
            self.brain.meter.seed(incident_id, inc.tokens)
        with bound(incident_id):  # every model call on this worker is counted against this incident
            self._run(incident_id)

    def _run(self, incident_id: str) -> None:
        frozen_said = False
        while True:
            inc = self.ctx.store.load(incident_id)
            if inc is None or inc.terminal:
                return
            if not inc.llm:
                inc.llm = self.ctx.llm.label if self.ctx.llm.enabled else f"none ({self.ctx.llm.reason})"
            try:
                self.step(inc)
                frozen_said = False
            except Frozen:
                if not frozen_said:
                    self.ctx.bus.publish("system", "system", f"{inc.id} frozen at {inc.phase} by the kill switch",
                                         incident_id=inc.id)
                    frozen_said = True
                while self.ctx.safety.frozen:
                    time.sleep(0.5)
            except Exception as exc:  # an agent bug must still end in a report
                inc = self.ctx.store.load(incident_id) or inc
                inc.limitations.append(f"internal error at {inc.phase}: {exc.__class__.__name__}: {exc}")
                self.ctx.bus.publish("system", "error", f"{inc.id} failed at {inc.phase}: {exc}", incident_id=inc.id)
                self.finish(inc, "failed")
                return

    def _goto(self, inc: Incident, phase: str) -> None:
        old = inc.phase
        inc.phase = phase  # type: ignore[assignment]
        inc.tokens = self.brain.meter.usage(inc.id)
        self.ctx.store.save(inc)
        self.ctx.bus.publish("system", "phase", f"{inc.id}: {old} -> {phase}", incident_id=inc.id,
                             detail={"from": old, "to": phase})

    def finish(self, inc: Incident, phase: str) -> None:
        """Terminal phase. The report is written before the phase is saved, so whoever sees a closed incident (the
        Control Room, the practice Judge) always finds its report."""
        if phase == "resolved":
            inc.resolved_at = iso()
        old, inc.phase = inc.phase, phase  # type: ignore[assignment]
        inc.tokens = self.brain.meter.usage(inc.id)
        self.ctx.bus.publish("system", "phase", f"{inc.id}: {old} -> {phase}", incident_id=inc.id,
                             detail={"from": old, "to": phase})
        try:
            self.reporter.run(inc)
        except Exception as exc:
            inc.limitations.append(f"report generation failed: {exc}")
            self.ctx.bus.publish("reporter", "error", f"report failed: {exc}", incident_id=inc.id)
        try:  # incident memory: what ran live, and whether the Verifier saw the system recover
            entry = self.planner.knowledge.memory.record(inc)
            if entry:
                self.ctx.bus.publish("planner", "step", f"remembered {inc.id}: "
                                     f"{'recovered' if entry['recovered'] else 'did not recover'} after "
                                     f"{', '.join(a['action'] for a in entry['actions'])}", incident_id=inc.id)
        except OSError as exc:
            self.ctx.bus.publish("planner", "error", f"incident memory not written: {exc}", incident_id=inc.id)
        inc.tokens = self.brain.meter.usage(inc.id)  # the Reporter's wording call is counted too
        try:  # the casebook: every closed incident, with what the model cost
            entry = self.brain.casebook.record(inc)
            if entry:
                cost = (f"{inc.tokens.calls} model calls, {inc.tokens.total} tokens"
                        + (" (estimated)" if inc.tokens.estimated else ""))
                if entry["mode"] == "recalled":
                    cost += f"; saved about {entry['tokens_saved']} tokens by recalling {entry['recall_of']}"
                self.ctx.bus.publish("brain", "step", f"casebook: {inc.id} stored as {phase} ({cost})",
                                     incident_id=inc.id, detail={"tokens": inc.tokens.model_dump(),
                                                                 "mode": entry["mode"]})
        except OSError as exc:
            self.ctx.bus.publish("brain", "error", f"casebook not written: {exc}", incident_id=inc.id)
        self.ctx.store.save(inc)

    # ------------------------------------------------------------ recall (brain.recall)
    @staticmethod
    def _recalling(inc: Incident) -> bool:
        return inc.recall is not None and inc.recall.status in ("matched", "confirmed")

    def _abandon_recall(self, inc: Incident, why: str) -> None:
        assert inc.recall is not None
        inc.recall.status, inc.recall.reason = "abandoned", why
        inc.recall.model_calls_skipped, inc.recall.tokens_saved = 0, 0
        inc.limitations.append(f"known-incident recall of {inc.recall.case} abandoned: {why}")
        self.ctx.bus.publish("brain", "step", f"recall abandoned ({why}); reasoning with the model",
                             incident_id=inc.id)

    def step(self, inc: Incident) -> None:
        ctx = self.ctx
        p = inc.phase
        if p == "detected":
            rec = self.brain.recall.lookup(inc)
            if rec is not None:
                inc.recall = rec
                ctx.bus.publish("brain", "step", f"known incident: alerts match {rec.case} ({rec.component}, "
                                f"{rec.category}; {rec.track_record}). Answering from the casebook with no model "
                                "calls; the evidence audit and human approval still apply", incident_id=inc.id,
                                detail=rec.model_dump())
            return self._goto(inc, "investigating")

        if p == "investigating":
            ctx.safety.check()
            ledger = EvidenceLedger(ctx.store, ctx.bus, inc.id)
            last = inc.audits[-1] if inc.audits and not inc.audits[-1].passed else None
            summary = self.investigator.run(inc, ledger, last.problems if last else None,
                                            use_model=not self._recalling(inc))
            inc = ctx.store.load(inc.id) or inc  # the Sentinel may have attached alerts meanwhile
            inc.investigation_notes.append(summary)
            inc.rounds += 1
            ctx.bus.publish("investigator", "step", f"round {inc.rounds} done: {summary[:300]}", incident_id=inc.id)
            return self._goto(inc, "diagnosing")

        if p == "diagnosing":
            ctx.safety.check()
            failed_rounds = sum(1 for a in inc.audits if not a.passed)
            recalling = self._recalling(inc)
            use_model = failed_rounds < ctx.settings.audit_retries and not recalling
            d = self.diagnostician.run(inc, ctx.store.evidence(inc.id),
                                       inc.investigation_notes[-1] if inc.investigation_notes else "", use_model)
            if recalling:
                assert inc.recall is not None
                why = Recall.confirms(inc.recall, d)
                if why:
                    self._abandon_recall(inc, why)
                    return self._goto(inc, "investigating")
            inc.diagnosis = d
            ctx.bus.publish("diagnostician", "step", f"({d.produced_by}) {d.summary} confidence {d.confidence:.2f}",
                            incident_id=inc.id, detail=d.model_dump())
            return self._goto(inc, "auditing")

        if p == "auditing":
            evidence = ctx.store.evidence(inc.id)
            assert inc.diagnosis is not None
            res = self.auditor.run(inc, inc.diagnosis, evidence)
            inc.audits.append(res)
            if res.passed and inc.diagnosis.false_alarm:
                inc.escalation_reason = ""
                return self.finish(inc, "false_alarm")  # nothing to fix: no plan, no approval, no action
            if res.passed:
                if self._recalling(inc):
                    assert inc.recall is not None
                    inc.recall.status = "confirmed"
                    ctx.bus.publish("brain", "step", f"known incident confirmed: the rules agree with {inc.recall.case} "
                                    "and the evidence audit passed", incident_id=inc.id)
                return self._goto(inc, "planning")
            if self._recalling(inc):
                self._abandon_recall(inc, "the evidence audit rejected the rule-based diagnosis: "
                                     + "; ".join(res.problems)[:200])
                return self._goto(inc, "investigating")
            failed_rounds = sum(1 for a in inc.audits if not a.passed)
            if failed_rounds <= ctx.settings.audit_retries:
                return self._goto(inc, "investigating")
            # out of retries: rules over the same evidence, then keep only what the evidence supports
            d = inc.diagnosis
            if d.produced_by == "llm":
                d = rule_diagnosis(inc, evidence, ctx.telemetry.topology, tuple(ctx.settings.ignore_services))
                res2 = self.auditor.run(inc, d, evidence)
                inc.audits.append(res2)
                inc.diagnosis = d
                if res2.passed:
                    inc.limitations.append("the model's diagnosis failed the evidence audit; the rule-based "
                                           "diagnosis was used")
                    return self._goto(inc, "planning")
                res = res2
            d = drop_unsupported(d, evidence)
            inc.diagnosis = d
            if not d.root_causes:
                inc.escalation_reason = "no root cause could be supported by the collected evidence"
                inc.limitations.append("audit problems: " + "; ".join(res.problems))
                return self.finish(inc, "escalated")
            if res.unexplained_alerts:
                inc.limitations.append("alerts not explained by the diagnosis: " + ", ".join(res.unexplained_alerts))
            inc.limitations.append("unsupported claims were removed by the evidence audit")
            return self._goto(inc, "planning")

        if p == "planning":
            assert inc.diagnosis is not None
            plan = self.planner.run(inc, inc.diagnosis, ctx.store.evidence(inc.id))
            if plan.produced_by == "memory" and not plan.actions:
                self._abandon_recall(inc, plan.no_action_reason)
                plan = self.planner.run(inc, inc.diagnosis, ctx.store.evidence(inc.id))
            inc.plan = plan
            if not plan.actions:
                inc.escalation_reason = plan.no_action_reason or "no action proposed"
                return self.finish(inc, "escalated")
            # mark the plan pending before the phase changes: anyone who sees "awaiting_approval" (the Control Room,
            # the CLI) can decide at once, without racing the request
            self.approval.request(inc)
            self._goto(inc, "awaiting_approval")
            return None

        if p == "awaiting_approval":
            if inc.approval.decision != "pending" or inc.approval.change_hash != (inc.plan.change_hash if inc.plan else None):
                self.approval.request(inc)  # e.g. resumed after a restart mid-request
            decision = self.approval.wait(inc)
            inc = ctx.store.load(inc.id) or inc
            if decision == "approved":
                return self._goto(inc, "executing")
            if decision == "rejected":
                inc.escalation_reason = f"plan rejected by {inc.approval.approver}" + (
                    f": {inc.approval.note}" if inc.approval.note else "")
                return self.finish(inc, "rejected")
            if decision == "sent_back":
                inc.human_notes.append(f"{inc.approval.approver}: {inc.approval.note}")
                inc.approval = ApprovalRecord()
                inc.plan = None
                return self._goto(inc, "investigating")
            inc.escalation_reason = "no approval decision before the timeout"
            inc.limitations.append("no human decision before the approval timeout; nothing was executed")
            return self.finish(inc, "escalated")

        if p == "executing":
            if not inc.execution.attempted:
                inc = self.executor.run(inc)
            return self._goto(inc, "verifying")

        if p == "verifying":
            inc = self.verifier.run(inc)
            if inc.verification.recovered is True:
                return self.finish(inc, "resolved")
            if inc.verification.recovered is None:
                if inc.execution.results and all(r.dry_run for r in inc.execution.results):
                    inc.limitations.append("executed in dry-run mode: the Action API previewed the plan and changed "
                                           "nothing, so recovery was not verified")
                    return self.finish(inc, "dry_run_complete")
                inc.escalation_reason = "the approved action did not execute successfully"
                return self.finish(inc, "escalated")
            return self.finish(inc, "escalated")

        raise RuntimeError(f"unknown phase {p}")
