"""The Brain: the casebook stores every closed incident, the token meter counts what the model cost, and a known
incident is answered from the casebook with no model calls, still audited and still approved by a human."""
from __future__ import annotations

import json
import time

from incident_zero.brain.casebook import Casebook, alert_keys
from incident_zero.brain.meter import TokenMeter, bound, usage_from_response
from incident_zero.llm import LLM
from incident_zero.llm_client import LLMClient

from .test_pipeline import build, heal_all, payment_failure, tick_until_incident, wait_phase

FIX = ("toggle_feature_flag", {"flag": "paymentFailure", "enabled": False})


class CountingClient:
    """A model that always fails to answer (so every agent's rules decide) but reports what each call cost."""

    def __init__(self):
        self.calls = 0

    def chat_json(self, system, user, max_tokens=700):
        self.calls += 1
        return None

    def last_usage(self):
        return 1200, 300


def counting_llm():
    client = CountingClient()
    return LLM(client=client, label="counting"), client


def break_payment(svcs):
    fresh, _ = payment_failure()
    for name in ("payment", "checkout", "frontend"):
        svcs[name].now = dict(fresh[name].now)


def new_incident(orch, seen: set[str]) -> str:
    for _ in range(8):
        orch.sentinel.tick()
        ids = [i.id for i in orch.ctx.store.list() if i.id not in seen]
        if ids:
            return ids[0]
    raise AssertionError("no new incident opened")


def pending(store, inc_id, timeout=10.0):
    """Awaiting approval AND marked pending by the gate (it saves the phase a moment before the request)."""
    end = time.time() + timeout
    while time.time() < end:
        inc = wait_phase(store, inc_id, ("awaiting_approval",), timeout)
        if inc.approval.decision == "pending":
            return inc
        time.sleep(0.02)
    raise AssertionError(f"{inc_id} never became pending")


def resolve(orch, store, inc_id):
    orch.open(inc_id)
    inc = pending(store, inc_id)
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("resolved", "escalated"))
    assert inc.phase == "resolved", (inc.escalation_reason, inc.verification, inc.limitations)
    return inc


# ---------------------------------------------------------------- recall, end to end
def test_known_incident_is_answered_from_the_casebook_with_no_model_calls(settings, store, bus):
    settings.symptom_hold_s = 0
    svcs, traces = payment_failure()
    llm, client = counting_llm()
    orch, rt = build(settings, store, bus, svcs, traces, fixes={"toggle_feature_flag": heal_all}, llm=llm)
    orch.ctx.safety.set_dry_run(False, "preet")

    first = resolve(orch, store, tick_until_incident(orch))
    assert first.recall is None and first.plan.produced_by != "memory"
    assert first.tokens.calls == client.calls > 0 and first.tokens.total == client.calls * 1500
    assert first.tokens.by_agent["diagnostician"]["calls"] >= 1
    calls_after_first = client.calls

    for _ in range(2):
        orch.sentinel.tick()
    break_payment(svcs)
    second_id = new_incident(orch, {first.id})
    orch.open(second_id)
    inc = pending(store, second_id)
    assert client.calls == calls_after_first  # investigated, diagnosed and planned without the model
    assert inc.recall.status == "confirmed" and inc.recall.case == first.id
    assert inc.audits[-1].passed and inc.diagnosis.produced_by == "rules"
    assert inc.plan.produced_by == "memory"
    assert [(a.action, a.parameters) for a in inc.plan.actions] == [FIX]
    cited = {k.id: k for k in inc.knowledge}[inc.plan.actions[0].citations[0].knowledge_id]
    assert cited.source == "memory" and cited.ref == first.id
    assert inc.plan.actions[0].preview  # still previewed by the Action API as a dry run

    orch.approval.decide(second_id, "approved", "preet", inc.plan.change_hash)  # a human still approves
    inc = wait_phase(store, second_id, ("resolved",))
    assert client.calls == calls_after_first and inc.tokens.calls == 0
    assert inc.recall.tokens_saved == first.tokens.total and inc.recall.model_calls_skipped == first.tokens.calls
    assert "Known incident" in store.report(second_id)[0]["summary"]

    stats = orch.brain.stats()
    assert stats["incidents"] == 2 and stats["recalled"] == 1 and stats["tokens_saved"] == first.tokens.total
    assert stats["tokens_used"] == first.tokens.total


def test_recall_is_abandoned_when_the_evidence_disagrees(settings, store, bus):
    svcs, traces = payment_failure()
    llm, client = counting_llm()
    orch, _ = build(settings, store, bus, svcs, traces, llm=llm)
    orch.sentinel.on_new_incident = lambda _id: None  # open it ourselves, after the past case is written
    inc_id = tick_until_incident(orch)
    inc = store.load(inc_id)
    # a past case with the same alerts, but a different cause: the new evidence must not be forced to fit it
    casebook = Casebook(settings.data_dir)
    casebook.file.parent.mkdir(parents=True, exist_ok=True)
    casebook.file.write_text(json.dumps({
        "incident": "INC-0900", "at": "2026-10-01T00:00:00Z", "outcome": "resolved", "alerts": alert_keys(inc),
        "components": ["cart"], "categories": ["container_down"], "causes": ["cart stopped"],
        "actions": [{"action": "restart_service", "parameters": {"service": "cart"}}], "dry_run": False,
        "recovered": True, "mode": "reasoned", "tokens": {"calls": 4, "total": 6000}}) + "\n")
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert inc.recall.status == "abandoned" and "payment" in inc.recall.reason and inc.recall.tokens_saved == 0
    assert client.calls > 0                      # the full pipeline ran, model included
    assert inc.plan.produced_by != "memory"
    assert [(a.action, a.parameters) for a in inc.plan.actions] == [FIX]
    assert any("recall of INC-0900 abandoned" in x for x in inc.limitations)


def test_recall_can_be_switched_off(settings, store, bus):
    settings.recall_enabled = False
    settings.symptom_hold_s = 0
    svcs, traces = payment_failure()
    llm, client = counting_llm()
    orch, _ = build(settings, store, bus, svcs, traces, fixes={"toggle_feature_flag": heal_all}, llm=llm)
    orch.ctx.safety.set_dry_run(False, "preet")
    first = resolve(orch, store, tick_until_incident(orch))
    orch.sentinel.tick()
    break_payment(svcs)
    second_id = new_incident(orch, {first.id})
    orch.open(second_id)
    inc = wait_phase(store, second_id, ("awaiting_approval",))
    assert inc.recall is None and inc.tokens.calls > 0


# ---------------------------------------------------------------- the casebook
def _entry(inc, alerts, outcome="resolved", recovered=True, dry=False, actions=None, mode="reasoned", calls=4,
           total=6000, recall_of=None):
    return {"incident": inc, "at": "2026-10-01T00:00:00Z", "outcome": outcome, "alerts": alerts,
            "components": ["payment"], "categories": ["application_error"], "causes": ["x"],
            "actions": actions if actions is not None else [{"action": FIX[0], "parameters": FIX[1]}],
            "dry_run": dry, "recovered": recovered, "mode": mode, "recall_of": recall_of,
            "tokens": {"calls": calls, "total": total}, "tokens_saved": 0}


def _book(tmp_path, *entries) -> Casebook:
    cb = Casebook(tmp_path)
    cb.file.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return cb


ALERTS = ["error_rate:checkout", "error_rate:payment", "operation_errors:payment"]


def test_only_live_verified_fixes_make_an_incident_known(tmp_path):
    assert _book(tmp_path, _entry("INC-0001", ALERTS, dry=True, recovered=None,
                                  outcome="dry_run_complete")).known(ALERTS) is None
    assert _book(tmp_path, _entry("INC-0001", ALERTS, outcome="escalated", recovered=False)).known(ALERTS) is None
    assert _book(tmp_path, _entry("INC-0001", ALERTS, outcome="false_alarm", actions=[])).known(ALERTS) is None
    case = _book(tmp_path, _entry("INC-0001", ALERTS)).known(ALERTS)
    assert case.incident == "INC-0001" and case.similarity == 1.0 and case.track_record.startswith("recovered 1 of 1")


def test_a_fix_that_failed_last_time_is_not_reused(tmp_path):
    cb = _book(tmp_path, _entry("INC-0001", ALERTS), _entry("INC-0002", ALERTS, outcome="escalated", recovered=False))
    assert cb.known(ALERTS) is None
    cb = _book(tmp_path, _entry("INC-0001", ALERTS), _entry("INC-0002", ALERTS, outcome="escalated", recovered=False),
               _entry("INC-0003", ALERTS))
    case = cb.known(ALERTS)
    assert case.incident == "INC-0003" and case.successes == 2 and case.attempts == 3


def test_similar_alerts_match_and_unrelated_alerts_do_not(tmp_path):
    cb = _book(tmp_path, _entry("INC-0001", ALERTS))
    assert cb.known(ALERTS + ["error_rate:frontend"]).incident == "INC-0001"  # 3 of 4 alerts shared
    assert cb.known(["container_down:valkey-cart"]) is None
    assert cb.known(ALERTS, min_successes=2) is None


def test_tokens_saved_trace_back_to_the_reasoned_original(tmp_path):
    cb = _book(tmp_path, _entry("INC-0001", ALERTS, calls=5, total=9000),
               _entry("INC-0002", ALERTS, mode="recalled", calls=0, total=0, recall_of="INC-0001"))
    calls, tokens, estimated = cb.cost_of(cb.known(ALERTS))
    assert (calls, tokens, estimated) == (5, 9000, False)


def test_every_closed_incident_is_stored_once(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    settings.approval_timeout_s = 0.3
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    wait_phase(store, inc_id, ("escalated",))
    entries = orch.brain.casebook.entries()
    assert [e["incident"] for e in entries] == [inc_id] and entries[0]["outcome"] == "escalated"
    assert orch.brain.casebook.record(store.load(inc_id)) is None  # not stored twice


# ---------------------------------------------------------------- the token meter
def test_usage_is_read_from_each_provider():
    assert usage_from_response("openai", {"usage": {"prompt_tokens": 10, "completion_tokens": 4}}) == (10, 4)
    assert usage_from_response("anthropic", {"usage": {"input_tokens": 7, "output_tokens": 2}}) == (7, 2)
    assert usage_from_response("ollama", {"prompt_eval_count": 5, "eval_count": 3}) == (5, 3)
    assert usage_from_response("openai", {"choices": []}) is None


def test_meter_attributes_calls_to_the_bound_incident_and_agent():
    meter = TokenMeter()
    meter.record(100, 10)  # outside any incident: not attributed
    with bound("INC-0007"):
        meter.record(100, 10, agent="planner")
        meter.record(50, 5, agent="planner", estimated=True)
    u = meter.usage("INC-0007")
    assert (u.calls, u.total, u.estimated) == (2, 165, True)
    assert u.by_agent["planner"] == {"calls": 2, "prompt_tokens": 150, "completion_tokens": 15}


def test_llm_counts_real_usage_and_estimates_when_none_is_reported(monkeypatch):
    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    body = {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 321, "completion_tokens": 12}}
    monkeypatch.setattr("incident_zero.llm_client.requests.post", lambda *a, **k: Resp(body))
    client = LLMClient(provider="openai", api_key="k")
    llm = LLM(client=client, label="test")
    with bound("INC-0001"):
        assert llm.json("sys", "user") == {"ok": True}
        body.pop("usage")
        llm.json("system prompt", "x" * 400)
    u = llm.meter.usage("INC-0001")
    assert u.calls == 2 and u.prompt_tokens == 321 + len("system prompt" + "x" * 400) // 4 and u.estimated


# ---------------------------------------------------------------- the layers
def test_agents_are_grouped_into_eyes_and_hands(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    assert orch.eyes.sentinel is orch.sentinel and orch.hands.planner is orch.planner
    assert orch.hands.planner.knowledge is orch.brain.knowledge
    layers = {r["agent"]: r["layer"] for r in orch.ctx.status.rows()}
    assert {a for a, layer in layers.items() if layer == "eyes"} == {"sentinel", "investigator", "diagnostician",
                                                                     "auditor"}
    assert {a for a, layer in layers.items() if layer == "hands"} == {"planner", "approval", "executor", "verifier",
                                                                      "reporter"}
