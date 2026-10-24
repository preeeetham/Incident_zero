from __future__ import annotations

import pytest

from incident_zero.evidence import EvidenceLedger, audit
from incident_zero.models import Alert, Claim, Diagnosis, Incident, RootCause
from incident_zero.redact import MASK, mentions_flags, redact, redact_text
from incident_zero.report_schema import ReportInvalid, validate


def test_store_round_trip_and_ids(store):
    a, b = store.next_incident_id(), store.next_incident_id()
    assert (a, b) == ("INC-0001", "INC-0002")
    inc = store.save(Incident(id=a, title="checkout errors"))
    assert store.load(a).title == "checkout errors"
    assert [i.id for i in store.open_incidents()] == [a]
    inc.phase = "resolved"
    store.save(inc)
    assert store.open_incidents() == []


def test_store_rejects_path_traversal(store):
    with pytest.raises(ValueError):
        store.incident_dir("../etc")


def test_events_are_persisted_and_streamed(store, bus):
    q = bus.subscribe()
    bus.publish("sentinel", "alert", "error rate on cart", incident_id="INC-0001")
    bus.publish("system", "system", "started")
    assert q.get_nowait().title == "error rate on cart"
    assert [e.seq for e in store.events()] == [1, 2]
    assert [e.title for e in store.events(incident_id="INC-0001")] == ["error rate on cart"]
    assert [e.seq for e in store.events(after=1)] == [2]


def _ledger(store, bus):
    led = EvidenceLedger(store, bus, "INC-0001")
    led.add("prometheus", "service_red", {"service": "payment"}, "payment error ratio 0.42",
            "service=payment rps=1.20 error_ratio=0.42 p95_ms=180")
    led.add("logs", "search_logs", {"service": "payment"}, "3 error lines",
            "2026-10-23T10:00:01Z payment ERROR Payment request failed. Invalid token. app.loyalty.level=gold")
    return led


def test_ledger_ids_and_reload(store, bus):
    led = _ledger(store, bus)
    assert [e.id for e in led.all()] == ["E1", "E2"]
    again = EvidenceLedger(store, bus, "INC-0001")
    assert again.get("E2").source == "logs"
    assert again.add("docker", "container_status", {}, "s", "c").id == "E3"
    # two live ledgers for one incident (Sentinel + Investigator) never reuse an id
    assert led.add("docker", "x", {}, "s", "c").id == "E4"
    assert [e.id for e in led.all()] == ["E1", "E2", "E3", "E4"]


def _diag(claims, explained=("A1",)):
    return Diagnosis(summary="payment fails", explained_alerts=list(explained),
                     root_causes=[RootCause(component="payment", category="application_error",
                                            cause="charge requests fail", confidence=0.8, claims=claims)])


ALERTS = [Alert(id="A1", rule="error_rate", service="payment", signal="error ratio")]


def test_audit_passes_verbatim_quotes(store, bus):
    led = _ledger(store, bus)
    d = _diag([Claim(text="errors", evidence_id="E1", quote="error_ratio=0.42"),
               Claim(text="log", evidence_id="E2", quote="payment  ERROR Payment request failed")])
    res = audit(d, led.all(), ALERTS, known_components={"payment", "checkout"})
    assert res.passed, res.problems
    assert res.checked_claims == 2


@pytest.mark.parametrize("claim, problem", [
    (Claim(text="x", evidence_id="E9", quote="error_ratio=0.42"), "does not exist"),
    (Claim(text="x", evidence_id="E1", quote="error_ratio=0.97"), "is not in E1"),
    (Claim(text="x", evidence_id="E1", quote="0."), "is not in E1"),  # too short to count as a quote
])
def test_audit_rejects_invented_evidence(store, bus, claim, problem):
    res = audit(_diag([claim]), _ledger(store, bus).all(), ALERTS)
    assert not res.passed and any(problem in p for p in res.problems)


def test_audit_requires_every_alert_explained(store, bus):
    alerts = ALERTS + [Alert(id="A2", rule="latency_p95", service="frontend", signal="p95")]
    res = audit(_diag([Claim(text="x", evidence_id="E1", quote="error_ratio=0.42")]), _ledger(store, bus).all(), alerts)
    assert res.unexplained_alerts == ["A2"]
    assert not res.passed


def test_false_alarm_needs_quoted_health_evidence(store, bus):
    led = _ledger(store, bus)
    ok = Diagnosis(summary="noise", root_causes=[], false_alarm=True,
                   claims=[Claim(text="healthy", evidence_id="E1", quote="rps=1.20")])
    assert audit(ok, led.all(), ALERTS).passed
    bare = Diagnosis(summary="noise", root_causes=[], false_alarm=True)
    assert not audit(bare, led.all(), ALERTS).passed
    fake = Diagnosis(summary="noise", false_alarm=True, claims=[Claim(text="x", evidence_id="E1", quote="all good")])
    assert not audit(fake, led.all(), ALERTS).passed


def test_audit_rejects_unknown_component_and_alert(store, bus):
    d = _diag([Claim(text="x", evidence_id="E1", quote="error_ratio=0.42")], explained=("A1", "A7"))
    d.root_causes[0].component = "billing"
    res = audit(d, _ledger(store, bus).all(), ALERTS, known_components={"payment"})
    assert any("not a known component" in p for p in res.problems)
    assert any("A7" in p for p in res.problems)


# ---------------------------------------------------------------- R2: feature-flag redaction
JAEGER_SPAN = {
    "operationName": "oteldemo.PaymentService/Charge",
    "tags": [{"key": "rpc.system", "value": "grpc"},
             {"key": "feature_flag.key", "value": "paymentFailure"},
             {"key": "feature_flag.variant", "value": "on"},
             {"key": "app.payment.amount", "value": 42}],
    "logs": [{"timestamp": 1, "fields": [{"key": "event", "value": "feature_flag.evaluation"},
                                         {"key": "feature_flag.provider_name", "value": "flagd"}]},
             {"timestamp": 2, "fields": [{"key": "event", "value": "exception"},
                                         {"key": "exception.message", "value": "Invalid token. paymentFailure"}]}],
}


def test_redact_removes_flag_tags_events_and_names():
    out = redact(JAEGER_SPAN)
    keys = [t["key"] for t in out["tags"]]
    assert keys == ["rpc.system", "app.payment.amount"]
    assert len(out["logs"]) == 1
    assert out["logs"][0]["fields"][1]["value"] == f"Invalid token. {MASK}"
    assert not mentions_flags(str(out).replace(MASK, ""))


def test_redact_keeps_flagd_as_a_service_name():
    out = redact({"serviceName": "flagd", "operationName": "flagd.evaluation.v1.Service/ResolveBoolean"})
    assert out["serviceName"] == "flagd"


def test_redact_text_keeps_flagd_service_lines():
    # found live: dropping every line that said "flagd" hid flagd's container row and checkout's whole callee list
    text = ("checkout calls: cart, flagd, kafka\nflagd: state=exited exit_code=137 oom_killed=false restarts=0\n"
            "flagd evaluated feature_flag.key=paymentFailure")
    assert redact_text(text) == ("checkout calls: cart, flagd, kafka\n"
                                 "flagd: state=exited exit_code=137 oom_killed=false restarts=0")


def test_redact_text_drops_flag_lines():
    text = "cart ok\nfeature_flag.key=cartFailure evaluated\ncart error: cartFailure path"
    assert redact_text(text) == f"cart ok\ncart error: {MASK} path"


def test_redact_attribute_dict_keys():
    assert redact({"feature_flag.key": "x", "http.status": 500, "nested": {"openfeature.provider": "flagd"}}) == {
        "http.status": 500, "nested": {}}


# ---------------------------------------------------------------- report schema
def _report(**over):
    r = {
        "incident_id": "INC-0001", "status": "resolved", "summary": "s",
        "timeline": [{"timestamp": "2026-10-23T10:00:00Z", "event": "e", "details": "sentinel"}],
        "diagnosis": {"affected_component": "payment", "root_cause": "charge path fails", "confidence": 0.8,
                      "alternatives_considered": []},
        "evidence": [{"source": "metric", "observation": "[E1] error_ratio=0.42", "timestamp": None,
                      "reference": "q", "supports": "payment"}],
        "proposed_action": {"action": "toggle_feature_flag", "parameters": {"flag": "x", "enabled": False},
                            "reason": "r", "dry_run": False},
        "approval": {"required": True, "decision": "approved", "approver": "preet", "timestamp": "x"},
        "execution": {"attempted": True, "action_api_only": True, "action_result": "ok",
                      "rollback_performed": False, "notes": ""},
        "verification": {"checked": True, "recovered": True, "signals_checked": ["x"], "notes": ""},
        "prevention": ["p"], "limitations": [],
    }
    r.update(over)
    return r


def test_report_schema_accepts_a_complete_report():
    validate(_report())


@pytest.mark.parametrize("over", [
    {"approval": {"required": False, "decision": "approved"}},
    {"execution": {"attempted": True, "action_api_only": False}},
    {"approval": {"required": True, "decision": "sent_back"}},
    {"extra_field": 1},
    {"proposed_action": {"action": "x", "reason": "r"}},
    {"status": "open"},
    {"evidence": [{"source": "prometheus", "observation": "o", "supports": "s"}]},
    {"diagnosis": {"affected_component": "payment", "root_cause": "x", "confidence": 1.5}},
])
def test_report_schema_is_strict(over):
    with pytest.raises(ReportInvalid):
        validate(_report(**over))
