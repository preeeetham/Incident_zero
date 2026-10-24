from __future__ import annotations

from incident_zero.evidence import EvidenceLedger
from incident_zero.redact import mentions_flags
from incident_zero.telemetry import queries as Q
from incident_zero.telemetry.tools import Toolbox, summarize_trace

from . import fakes


def _box(settings, store, bus, svcs, traces=None, prom_down=False):
    led = EvidenceLedger(store, bus, "INC-0001")
    return Toolbox(fakes.telemetry(settings, svcs, traces, prom_down), led), led


def test_queries_select_inbound_spans_and_offsets():
    q = Q.error_rps("2m", "cart", offset_s=600)
    assert 'service_name="cart"' in q and "STATUS_CODE_ERROR" in q and "offset 600s" in q
    assert "SPAN_KIND_SERVER" in Q.rps("2m")
    assert Q.datastore_prefix("valkey-cart") == "redis" and Q.datastore_prefix("astronomy-db") == "postgresql"


def test_overview_flags_the_failing_service(settings, store, bus):
    svcs = fakes.healthy()
    svcs["payment"].now = {"rps": 1.0, "error_ratio": 0.42, "p95": 130}
    box, led = _box(settings, store, bus, svcs)
    out = box.overview()
    assert out.startswith("E1: ")
    pay = next(ln for ln in out.splitlines() if ln.startswith("payment:"))
    assert "error_ratio=0.420" in pay and "high error ratio" in pay
    assert led.get("E1").data["flagged"] == ["payment"]


def test_overview_flags_stopped_container_and_missing_traffic(settings, store, bus):
    svcs = fakes.healthy()
    svcs["cart"].state, svcs["cart"].exit_code = "exited", 137
    svcs["cart"].now = {"rps": 0.0, "error_ratio": 0.0, "p95": 0}
    box, _ = _box(settings, store, bus, svcs)
    cart = next(ln for ln in box.overview().splitlines() if ln.startswith("cart:"))
    assert "container exited" in cart and "no requests now" in cart


def test_service_metrics_shows_outbound_errors(settings, store, bus):
    svcs = fakes.healthy()
    svcs["checkout"].outbound_errors = {"oteldemo.PaymentService/Charge": 0.8}
    box, _ = _box(settings, store, bus, svcs)
    out = box.service_metrics("checkout")
    assert "outbound call errors: oteldemo.PaymentService/Charge error_rps=0.800" in out


def test_trace_summary_finds_the_origin():
    content, data = summarize_trace(fakes.payment_trace())
    assert "error origin (deepest failing span): payment oteldemo.PaymentService/Charge: Payment request failed" in content
    assert data["origins"] == ["payment"]


def test_trace_summary_shows_network_gap():
    tr = {"traceID": "t", "processes": {"a": {"serviceName": "cart"}, "b": {"serviceName": "valkey-cart"}},
          "spans": [fakes.span("s1", "a", "HGET", 900, kind="client"), fakes.span("s2", "b", "HGET", 2, "s1")]}
    content, _ = summarize_trace(tr)
    assert "client-server gap: cart->valkey-cart HGET client_ms=900.0 server_ms=2.0 gap_ms=898.0" in content


def test_otlp_conversion_from_jaeger_v3():
    from incident_zero.telemetry.backends import otlp_to_traces

    def attr(k, v):
        return {"key": k, "value": {"stringValue": v}}

    res = {"resourceSpans": [
        {"resource": {"attributes": [attr("service.name", "checkout")]}, "scopeSpans": [{"spans": [
            {"traceId": "t1", "spanId": "a1", "name": "oteldemo.PaymentService/Charge", "kind": 3,
             "startTimeUnixNano": "1000000000", "endTimeUnixNano": "1250000000", "status": {"code": 2}}]}]},
        {"resource": {"attributes": [attr("service.name", "payment")]}, "scopeSpans": [{"spans": [
            {"traceId": "t1", "spanId": "b1", "parentSpanId": "a1", "name": "oteldemo.PaymentService/Charge", "kind": 2,
             "startTimeUnixNano": "1010000000", "endTimeUnixNano": "1240000000",
             "status": {"code": 2, "message": "Payment request failed. Invalid token."},
             "events": [{"name": "exception", "attributes": [attr("exception.message", "Invalid token")]}]}]}]}]}
    [tr] = otlp_to_traces(res)
    content, data = summarize_trace(tr)
    assert "error origin (deepest failing span): payment oteldemo.PaymentService/Charge: Invalid token" in content
    assert "client-server gap: checkout->payment" in content and data["origins"] == ["payment"]


def test_trace_tools_record_redacted_evidence(settings, store, bus):
    tr = fakes.payment_trace()
    tr["spans"][3]["tags"].append({"key": "feature_flag.key", "value": "paymentFailure"})
    box, led = _box(settings, store, bus, fakes.healthy(), {"payment": [tr]})
    assert "origin=payment" in box.find_traces("payment")
    detail = box.trace_detail("abc123")
    assert "Payment request failed" in detail
    for ev in led.all():
        assert not mentions_flags(ev.content + str(ev.data))


def test_logs_and_flag_refusals(settings, store, bus):
    svcs = fakes.healthy()
    svcs["payment"].logs = ["Payment request failed. Invalid token. app.loyalty.level=gold"]
    box, led = _box(settings, store, bus, svcs)
    assert "Invalid token" in box.search_logs("payment")
    assert box.search_logs("flagd").startswith("refused")
    assert box.promql('sum(feature_flag_flagd_evaluation_total)').startswith("refused")
    assert box.search_logs("cart", contains="cartFailure").startswith("refused")
    assert len(led.all()) == 1  # refusals record nothing


def test_container_tools(settings, store, bus):
    svcs = fakes.healthy()
    svcs["valkey-cart"].state, svcs["valkey-cart"].oom, svcs["valkey-cart"].restarts = "exited", True, 2
    box, _ = _box(settings, store, bus, svcs)
    out = box.container_status("valkey-cart")
    assert "state=exited" in out and "oom_killed=true" in out and "restarts=2" in out
    assert "no container" in box.container_status("billing")
    assert "memory_percent=30.0" in box.container_resources("cart")


def test_dependencies_and_datastore(settings, store, bus):
    box, _ = _box(settings, store, bus, fakes.healthy())
    assert "cart calls: valkey-cart" in box.dependencies("cart")
    assert "called by: checkout, frontend" in box.dependencies("cart")
    assert "valkey-cart: newest redis_* sample 5s ago" in box.datastore_metrics("valkey-cart")


def test_backend_failure_is_evidence(settings, store, bus):
    box, led = _box(settings, store, bus, fakes.healthy(), prom_down=True)
    out = box.service_metrics("cart")
    assert "Prometheus unreachable" in out and led.get("E1") is not None


def test_dispatch_validates_arguments(settings, store, bus):
    box, _ = _box(settings, store, bus, fakes.healthy())
    assert box.call("nope").startswith("unknown tool")
    assert box.call("service_metrics", {}).startswith("bad arguments")
    assert box.call("dependencies", {"service": "cart"}).startswith("E1:")
