"""End to end through every agent, on fake telemetry and an in-process Action API."""
from __future__ import annotations

import time
from typing import Callable, Optional

import pytest

from incident_zero.actions.mock_server import ActionAPI
from incident_zero.agents.approval import ApprovalError
from incident_zero.agents.context import Context
from incident_zero.llm import LLM
from incident_zero.models import ActionResult, Incident
from incident_zero.orchestrator import Orchestrator
from incident_zero.redact import mentions_flags
from incident_zero.report_schema import validate
from incident_zero.safety import Safety

from . import fakes


class Runtime:
    """Fake Action API runtime: actions mutate the fake system through `fixes`."""

    def __init__(self, svcs, fixes: Optional[dict[str, Callable]] = None):
        self.svcs, self.fixes, self.calls, self.undone = svcs, fixes or {}, [], []

    def services(self):
        return set(self.svcs)

    def _act(self, name, target, dry, undo=None):
        self.calls.append((name, target, dry))
        if dry:
            return f"would {name} {target}", None
        fn = self.fixes.get(name)
        if fn:
            fn(self.svcs, target)
        return f"did {name} {target}", undo

    def restart_service(self, dry, service):
        return self._act("restart_service", service, dry)

    def toggle_feature_flag(self, dry, flag, enabled):
        return self._act("toggle_feature_flag", flag, dry, {"kind": "flags", "prev": {flag: "on"}})

    def throttle_load_generator(self, dry, users):
        return self._act("throttle_load_generator", users, dry, {"kind": "users", "prev": 50})

    def scale_service(self, dry, service, replicas):
        raise RuntimeError("Kubernetes only")

    def undo(self, undo):
        self.undone.append(undo)
        return "restored"


class InProcClient:
    def __init__(self, api: ActionAPI):
        self.api = api

    def actions(self):
        return self.api.catalog

    def execute(self, action, parameters, reason, dry_run, incident_id=""):
        code, d = self.api.execute({"action": action, "parameters": parameters, "reason": reason,
                                    "dry_run": dry_run, "incident_id": incident_id})
        return ActionResult(action=action, parameters=parameters, execution_id=d.get("execution_id"),
                            ok=code < 400 and d.get("ok", False), dry_run=dry_run, message=d.get("message", ""))

    def rollback(self, execution_id):
        code, d = self.api.rollback({"execution_id": execution_id})
        return ActionResult(action="rollback", execution_id=execution_id, ok=code < 400 and d.get("ok", False),
                            dry_run=False, message=d.get("message", ""))

    def kill(self):
        self.api.killed = True

    def resume(self):
        self.api.killed = False


def build(settings, store, bus, svcs, traces=None, fixes=None, llm: Optional[LLM] = None):
    rt = Runtime(svcs, fixes)
    client = InProcClient(ActionAPI(rt))
    ctx = Context(settings=settings, store=store, bus=bus, telemetry=fakes.telemetry(settings, svcs, traces),
                  actions=client, safety=Safety(store, bus, client, settings), llm=llm or LLM())  # type: ignore[arg-type]
    return Orchestrator(ctx), rt


def wait_phase(store, inc_id, phases, timeout=10.0) -> Incident:
    end = time.time() + timeout
    while time.time() < end:
        inc = store.load(inc_id)
        if inc and inc.phase in phases:
            return inc
        time.sleep(0.02)
    raise AssertionError(f"{inc_id} never reached {phases}; at {store.load(inc_id).phase}")


def tick_until_incident(orch, n=5) -> str:
    for _ in range(n):
        orch.sentinel.tick()
        incs = orch.ctx.store.list()
        if incs:
            return incs[-1].id
    raise AssertionError("no incident opened")


# ---------------------------------------------------------------- scenarios
def payment_failure():
    svcs = fakes.healthy()
    for s, r in (("payment", 0.6), ("checkout", 0.5), ("frontend", 0.3)):
        svcs[s].now = {"rps": 1.0, "error_ratio": r, "p95": 150}
    svcs["checkout"].outbound_errors = {"oteldemo.PaymentService/Charge": 0.6}
    svcs["payment"].logs = ["Payment request failed. Invalid token. app.loyalty.level=gold"]
    tr = fakes.payment_trace()
    return svcs, {"payment": [tr], "checkout": [tr], "frontend": [tr]}


def heal_all(svcs, service):
    for s in svcs.values():
        if s.now.get("rps", 0) > 0 or s.base.get("rps", 0) > 0:
            s.now = dict(s.base)
        s.state = "running"


def test_application_fault_dry_run_end_to_end(settings, store, bus):
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    inc = store.load(inc_id)
    assert {a.service for a in inc.alerts} >= {"payment", "checkout"}  # correlated into one incident
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    rc = inc.diagnosis.root_causes[0]
    assert (rc.component, rc.category) == ("payment", "application_error")
    assert inc.audits[-1].passed
    # the flag comes from the code that raises the error, not from a list: only paymentFailure guards that throw
    assert [(a.action, a.parameters) for a in inc.plan.actions] == [
        ("toggle_feature_flag", {"flag": "paymentFailure", "enabled": False})]
    k = {x.id: x for x in inc.knowledge}
    cited = [k[c.knowledge_id] for c in inc.plan.actions[0].citations]
    assert cited[0].source == "code" and cited[0].ref == "src/payment/charge.js:9"
    assert any(x.source == "runbook" for x in cited)
    assert inc.plan.actions[0].preview == "would toggle_feature_flag paymentFailure"
    assert all(dry for _, _, dry in rt.calls)  # nothing but previews before approval
    with pytest.raises(ApprovalError):
        orch.approval.decide(inc_id, "approved", "", inc.plan.change_hash)
    with pytest.raises(ApprovalError):
        orch.approval.decide(inc_id, "approved", "preet", "stale-hash")
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("dry_run_complete",))
    assert inc.execution.results[0].dry_run and inc.verification.recovered is None
    report, md = store.report(inc_id)
    validate(report)
    assert any(e["source"] == "other" and e["reference"] == "src/payment/charge.js:9" for e in report["evidence"])
    assert "based on: src/payment/charge.js:9" in report["proposed_action"]["reason"]
    assert report["approval"] == {"required": True, "decision": "approved", "approver": "preet",
                                  "timestamp": inc.approval.timestamp}
    assert report["execution"]["action_api_only"] is True and report["proposed_action"]["dry_run"] is True
    assert report["status"] == "unresolved"  # a dry run changes nothing
    assert report["diagnosis"]["affected_component"] == "payment"
    assert any("Payment request failed" in e["observation"] and e["source"] == "trace" for e in report["evidence"])
    assert {e["source"] for e in report["evidence"]} >= {"trace", "metric"}
    assert "# INC-0001" in md
    assert (store.root / "submission.json").is_file()


def test_stopped_cache_resolved_live(settings, store, bus):
    svcs = fakes.healthy()
    svcs["valkey-cart"].state, svcs["valkey-cart"].exit_code = "exited", 137
    svcs["cart"].now = {"rps": 1.0, "error_ratio": 0.9, "p95": 40}
    svcs["cart"].logs = ["Wasn't able to connect to redis valkey-cart:6379: connection refused"]

    orch, rt = build(settings, store, bus, svcs, fixes={"restart_service": heal_all})
    orch.ctx.safety.set_dry_run(False, "preet")
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert inc.diagnosis.root_causes[0].component == "valkey-cart"
    assert inc.diagnosis.root_causes[0].category == "container_down"
    assert inc.plan.actions[0].action == "restart_service"
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("resolved",))
    assert ("restart_service", "valkey-cart", False) in rt.calls
    assert inc.verification.recovered is True and inc.resolved_at
    assert store.report(inc_id)[0]["status"] == "resolved"


def test_failed_recovery_rolls_back_and_escalates(settings, store, bus):
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces)  # the action fixes nothing
    orch.ctx.safety.set_dry_run(False, "preet")
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("escalated",))
    assert inc.verification.recovered is False and inc.execution.rollback_performed
    assert rt.undone and "recovery not confirmed" in inc.escalation_reason
    rep = store.report(inc_id)[0]
    assert rep["execution"]["rollback_performed"] is True and rep["verification"]["recovered"] is False
    assert rep["status"] == "escalated"


def test_reject_and_send_back(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    with pytest.raises(ApprovalError):
        orch.approval.decide(inc_id, "sent_back", "preet", inc.plan.change_hash, note="")
    orch.approval.decide(inc_id, "sent_back", "preet", inc.plan.change_hash, note="check valkey too")
    time.sleep(0.3)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert inc.rounds == 2 and inc.human_notes == ["preet: check valkey too"]
    orch.approval.decide(inc_id, "rejected", "preet", inc.plan.change_hash, note="not now")
    inc = wait_phase(store, inc_id, ("rejected",))
    assert not inc.execution.attempted
    assert store.report(inc_id)[0]["approval"]["decision"] == "rejected"


def test_approval_timeout_escalates(settings, store, bus):
    settings.approval_timeout_s = 0.3
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("escalated",))
    assert not inc.execution.attempted and "timeout" in inc.escalation_reason
    assert store.report(inc_id)[0]["approval"]["decision"] == "pending"


def test_kill_switch_freezes_execution(settings, store, bus):
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces, fixes={"toggle_feature_flag": heal_all})
    orch.ctx.safety.set_dry_run(False, "preet")
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    orch.ctx.safety.stop("preet", "drill")
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    time.sleep(0.4)
    assert store.load(inc_id).phase == "executing" and not any(not dry for _, _, dry in rt.calls)
    orch.ctx.safety.resume("preet")
    inc = wait_phase(store, inc_id, ("resolved",))
    assert ("toggle_feature_flag", "paymentFailure", False) in rt.calls


class ScriptedLLM(LLM):
    """A model that invents evidence: its diagnosis cites a quote that is in no evidence."""

    def __init__(self):
        super().__init__(client=object(), label="scripted")
        self.calls = 0

    def json(self, system, user, max_tokens=1500):
        self.calls += 1
        if "diagnose" in system:
            return {"summary": "payment is broken", "explained_alerts": ["A1", "A2", "A3"],
                    "root_causes": [{"component": "payment", "category": "application_error", "cause": "bad deploy",
                                     "confidence": 0.99, "claims": [{"text": "x", "evidence_id": "E2",
                                                                     "quote": "error_ratio=0.999 everywhere"}]}]}
        return None


def test_invented_evidence_is_rejected_then_rules_take_over(settings, store, bus):
    svcs, traces = payment_failure()
    llm = ScriptedLLM()
    orch, _ = build(settings, store, bus, svcs, traces, llm=llm)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert [a.passed for a in inc.audits][:2] == [False, False] and inc.audits[-1].passed
    assert inc.diagnosis.produced_by == "rules" and inc.rounds == 3
    assert any("is not in E2" in p for p in inc.audits[0].problems)


def test_no_flag_data_anywhere(settings, store, bus):
    svcs, traces = payment_failure()
    traces["payment"][0]["spans"][3]["tags"].append({"key": "feature_flag.key", "value": "paymentFailure"})
    orch, _ = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    wait_phase(store, inc_id, ("awaiting_approval",))
    for ev in store.evidence(inc_id):
        assert not mentions_flags(ev.content + str(ev.data) + str(ev.args))


def test_sentinel_debounce_and_suppression(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    orch.sentinel.on_new_incident = lambda _id: None  # no worker: this test closes the incident by hand
    assert orch.sentinel.tick() == []  # first sighting is not yet an alert
    assert orch.sentinel.tick()        # confirmed on the second
    inc = store.list()[0]
    inc.phase = "rejected"
    store.save(inc)
    orch.sentinel.tick()
    assert len(store.list()) == 1      # same signals after an unresolved close: suppressed


def test_late_alerts_open_a_new_incident(settings, store, bus):
    """Found live: alerts that arrive after an incident is planned must not join it (they made a wrong fix look
    like a recovery)."""
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    orch.sentinel.on_new_incident = lambda _id: None
    first = tick_until_incident(orch)
    inc = store.load(first)
    inc.phase = "verifying"
    store.save(inc)
    svcs["cart"].now = {"rps": 1.0, "error_ratio": 0.7, "p95": 100}
    orch.sentinel.tick()
    orch.sentinel.tick()
    assert all(a.service != "cart" for a in store.load(first).alerts)
    assert any(a.service == "cart" for i in store.list() if i.id != first for a in i.alerts)


def test_one_failing_operation_is_an_alert(settings, store, bus):
    """Found live: cart's EmptyCart failed ~50% of its calls while cart overall stayed ~3%."""
    svcs = fakes.healthy()
    svcs["cart"].ops = {"POST /oteldemo.CartService/EmptyCart": (6.0, 12.0), "POST /oteldemo.CartService/GetCart": (0.0, 200.0)}
    orch, _ = build(settings, store, bus, svcs)
    orch.sentinel.on_new_incident = lambda _id: None
    orch.sentinel.tick()
    fired = orch.sentinel.tick()
    assert [(s.rule, s.service) for s in fired] == [("operation_errors", "cart")]
    assert "EmptyCart failed 6 of 12 calls (error_ratio=0.500)" in fired[0].signal
    assert "over 5m" in fired[0].signal  # rare operations are judged over the longer op_window
    svcs["cart"].ops = {"POST /oteldemo.CartService/EmptyCart": (1.0, 12.0)}  # one unlucky call is not an alert
    assert [s for s in orch.sentinel.tick() if s.rule == "operation_errors"] == []
    # ...but to the Verifier one failure still means "not recovered" (no false recovery on a short window)
    from incident_zero.agents.sentinel import evaluate

    svcs["cart"].ops = {"POST /oteldemo.CartService/EmptyCart": (1.0, 2.0)}  # a 1m view: ~2 calls, one failed
    sigs, _ = evaluate(orch.ctx, window="1m", verify=True)
    assert [(s.rule, s.service) for s in sigs] == [("operation_errors", "cart")]
    svcs["cart"].ops = {"POST /oteldemo.CartService/EmptyCart": (0.0, 12.0)}
    assert evaluate(orch.ctx, window="1m", verify=True)[0] == []


def test_symptoms_of_a_diagnosed_cause_do_not_open_new_incidents(settings, store, bus):
    """Found live: one catalog fault opened three incidents. A frontend symptom of a diagnosed product-catalog
    cause joins the open incident, and stays quiet just after it was resolved."""
    from incident_zero.models import Diagnosis, RootCause, iso

    svcs = fakes.healthy()
    orch, _ = build(settings, store, bus, svcs)
    orch.sentinel.on_new_incident = lambda _id: None
    inc = Incident(id=store.next_incident_id(), title="t", phase="awaiting_approval",
                   diagnosis=Diagnosis(summary="s", root_causes=[RootCause(component="product-catalog",
                                       category="application_error", cause="c", confidence=0.9)]))
    store.save(inc)
    svcs["frontend"].now = {"rps": 2.0, "error_ratio": 0.3, "p95": 120}
    orch.sentinel.tick()
    orch.sentinel.tick()
    assert [i.id for i in store.list()] == [inc.id]
    assert [a.service for a in store.load(inc.id).alerts] == ["frontend"]
    done = store.load(inc.id)
    done.phase, done.resolved_at = "resolved", iso()
    store.save(done)
    svcs["frontend-proxy"].now = {"rps": 2.0, "error_ratio": 0.5, "p95": 100}  # calls frontend -> product-catalog
    orch.sentinel.tick()
    orch.sentinel.tick()
    assert [i.id for i in store.list()] == [inc.id]  # held as a decaying symptom, no new incident
    svcs["cart"].now = {"rps": 1.0, "error_ratio": 0.6, "p95": 100}  # cart does not call product-catalog
    orch.sentinel.tick()
    orch.sentinel.tick()
    assert len(store.list()) == 2


def test_quiet_queue_consumer_is_not_an_alert(settings, store, bus):
    svcs = fakes.healthy()
    svcs["frontend"].base = {"rps": 0.09, "error_ratio": 0.0, "p95": 50}   # a sporadic consumer, now idle
    svcs["frontend"].now = {"rps": 0.0, "error_ratio": 0.0, "p95": 0}
    orch, _ = build(settings, store, bus, svcs)
    orch.sentinel.tick()
    assert orch.sentinel.tick() == []


class ScriptedToolModel:
    """Stands in for a LangChain chat model in the Investigator's LangGraph loop."""

    def __init__(self, script):
        self.script, self.seen = list(script), []

    def bind_tools(self, tools):
        self.tool_names = [t.name for t in tools]
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        self.seen.append(messages[-1])
        name, args = self.script.pop(0)
        return AIMessage(content=f"calling {name}", tool_calls=[{"name": name, "args": args, "id": f"c{len(self.seen)}"}])


def test_investigator_llm_loop_runs_tools_and_finishes(settings, store, bus):
    from incident_zero.agents.investigator import Investigator
    from incident_zero.evidence import EvidenceLedger

    svcs, traces = payment_failure()
    model = ScriptedToolModel([("overview", {}), ("service_metrics", {"service": "payment"}),
                               ("search_logs", {"service": "flagd"}),
                               ("finish_investigation", {"summary": "payment fails charges, see E1 E2"})])
    orch, _ = build(settings, store, bus, svcs, traces, llm=LLM(tool_model=model, label="scripted"))
    orch.sentinel.on_new_incident = lambda _id: None  # drive the Investigator directly, no orchestrator worker
    inc_id = tick_until_incident(orch)
    inc = store.load(inc_id)
    ledger = EvidenceLedger(store, bus, inc_id)
    summary = Investigator(orch.ctx).run(inc, ledger)
    assert summary == "payment fails charges, see E1 E2"
    assert "finish_investigation" in model.tool_names and "promql" in model.tool_names
    tools = [e.tool for e in ledger.all()]
    assert tools.count("overview") == 1 and "service_metrics" in tools
    assert "refused" in model.seen[3].content  # the flagd log request was refused, not recorded


def test_cpu_saturation_is_detected_and_planned_from_the_code(settings, store, bus):
    """Found live: adHighCpu took ad to 400 % CPU while its latency, traffic and errors stayed flat, so nothing fired."""
    svcs = fakes.healthy()
    svcs["ad"] = fakes.Svc(cpu=400.0, cpu_base=1.0)
    orch, rt = build(settings, store, bus, svcs)
    inc_id = tick_until_incident(orch)
    inc = store.load(inc_id)
    assert [(a.rule, a.service) for a in inc.alerts] == [("resource_saturation", "ad")]
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert (inc.diagnosis.root_causes[0].component, inc.diagnosis.root_causes[0].category) == ("ad", "resource_cpu")
    assert [(a.action, a.parameters) for a in inc.plan.actions] == [
        ("toggle_feature_flag", {"flag": "adHighCpu", "enabled": False})]


def test_steady_high_cpu_is_not_an_alert(settings, store, bus):
    svcs = fakes.healthy()
    svcs["product-catalog"].cpu = 90.0                    # busy, but busy like this at baseline too
    orch, _ = build(settings, store, bus, svcs)
    for _ in range(4):
        orch.sentinel.tick()
    assert store.list() == []


def test_memory_near_limit_counts_only_above_own_baseline():
    from incident_zero.agents.diagnostician import _memory_high

    assert not _memory_high(87.4, 85.0)   # checkout's normal share (INC-0011 was misdiagnosed on this)
    assert _memory_high(96.0, 70.0)       # well above its own normal
    assert not _memory_high(90.0, None)   # no baseline: only a container at its limit counts
    assert _memory_high(98.0, None)
