from __future__ import annotations

from incident_zero.actions.catalog import MOCK_ACTIONS
from incident_zero.agents.planner import rule_actions
from incident_zero.knowledge import Knowledge
from incident_zero.models import Alert, Claim, Diagnosis, Evidence, Incident, RootCause

RB = {"starved_cpu_limit": 0.25, "starved_memory_limit_mb": 64}


def _ev(content: str, eid: str = "E1", source: str = "docker", tool: str = "container_status") -> Evidence:
    return Evidence(id=eid, source=source, tool=tool, summary="s", content=content)  # type: ignore[arg-type]


def _diag(component: str, category: str, quotes: tuple[str, ...] = ()) -> Diagnosis:
    claims = [Claim(text="t", evidence_id="E9", quote=q) for q in quotes]
    return Diagnosis(summary="s", root_causes=[RootCause(component=component, category=category,  # type: ignore[arg-type]
                                                         cause="c", confidence=0.8, claims=claims)])


def _container(svc, cpu="None", mem="None", networks="opentelemetry-demo", state="running", oom="false"):
    return (f"{svc}: state={state} exit_code=0 oom_killed={oom} restarts=0 uptime_s=10 health=None "
            f"networks={networks} cpu_limit={cpu} memory_limit_mb={mem}")


def _plan(demo_dir, tmp_path, d: Diagnosis, ev: list[Evidence], alerts=(), **kw):
    inc = Incident(id="INC-0001", title="t", alerts=list(alerts))
    items = Knowledge(str(demo_dir), tmp_path / "data").gather(inc, d, ev)
    acts, notes = rule_actions(d, ev, MOCK_ACTIONS, items, RB, **kw)
    return acts, notes, items


def test_cpu_starvation_by_limit_escalates(demo_dir, tmp_path):
    ev = [_ev(_container("checkout", cpu="0.02")),
          _ev("checkout: cpu_percent=2.0 throttled_periods_delta=40 memory_mb=50 memory_limit_mb=None "
              "memory_percent=None net_rx_errors=0 net_tx_dropped=0 pids=9", "E2", tool="container_resources")]
    acts, notes, _ = _plan(demo_dir, tmp_path, _diag("checkout", "resource_cpu"), ev)
    assert acts == [] and "no allowed action raises limits" in notes[0]


def test_detached_network_escalates_and_netem_restarts(demo_dir, tmp_path):
    acts, notes, _ = _plan(demo_dir, tmp_path, _diag("currency", "network"),
                           [_ev(_container("currency", networks="none"))])
    assert acts == [] and "detached" in notes[0]
    acts, _, items = _plan(demo_dir, tmp_path, _diag("quote", "network"), [_ev(_container("quote"))])
    assert [(a.action, a.parameters) for a in acts] == [("restart_service", {"service": "quote"})]
    assert items[int(acts[0].citations[0].knowledge_id[1:]) - 1].source == "runbook"


def test_app_fault_toggles_the_flag_guarding_the_failing_line(demo_dir, tmp_path):
    ev = [_ev("error origin (deepest failing span): payment charge: Payment request failed. Invalid token.",
              "E2", "jaeger", "trace_detail")]
    acts, _, items = _plan(demo_dir, tmp_path, _diag("payment", "application_error"), ev)
    assert [(a.action, a.parameters) for a in acts] == [("toggle_feature_flag", {"flag": "paymentFailure",
                                                                                 "enabled": False})]
    code = next(k for k in items if k.id == acts[0].citations[0].knowledge_id)
    assert code.ref == "src/payment/charge.js:9" and [g["flag"] for g in code.data["guards"]] == ["paymentFailure"]


def test_unreachable_dependency_follows_the_callers_code(demo_dir, tmp_path):
    """The error is raised in checkout; the flag that picks the bad address guards it there."""
    quote = "could not charge the card: rpc error: code = Unavailable desc = name resolver error"
    ev = [_ev(f"error origin (deepest failing span): checkout chargeCard: {quote}", "E2", "jaeger", "trace_detail")]
    acts, _, _ = _plan(demo_dir, tmp_path, _diag("payment", "dependency_failure", (quote,)), ev)
    assert [(a.action, a.parameters) for a in acts] == [("toggle_feature_flag", {"flag": "paymentUnreachable",
                                                                                 "enabled": False})]


def test_cpu_in_own_code_picks_the_flag_by_symptom(demo_dir, tmp_path):
    ev = [_ev(_container("ad")), _ev("ad: cpu_percent=97.0 throttled_periods_delta=0 memory_mb=300 "
                                     "memory_limit_mb=None memory_percent=None", "E2", tool="container_resources")]
    acts, _, _ = _plan(demo_dir, tmp_path, _diag("ad", "resource_cpu"), ev)
    assert [(a.action, a.parameters) for a in acts] == [("toggle_feature_flag", {"flag": "adHighCpu",
                                                                                 "enabled": False})]


def test_flood_throttles_and_unknown_app_fault_restarts(demo_dir, tmp_path):
    acts, _, _ = _plan(demo_dir, tmp_path, _diag("frontend-proxy", "traffic"), [],
                       [Alert(id="A1", rule="traffic_spike", service="frontend-proxy", signal="s")], normal_users=5)
    assert [(a.action, a.parameters) for a in acts] == [("throttle_load_generator", {"users": 5})]
    acts, _, _ = _plan(demo_dir, tmp_path, _diag("quote", "application_error"), [])
    assert [(a.action, a.parameters) for a in acts] == [("restart_service", {"service": "quote"})]
    assert all(a.citations for a in acts)


def test_failing_operation_leads_to_the_flag_in_its_handler(demo_dir, tmp_path):
    """Found live: cart's error is raised in the store (no flag there); the flag is read in the EmptyCart handler."""
    alert = Alert(id="A1", rule="operation_errors", service="cart",
                  signal="cart POST /oteldemo.CartService/EmptyCart failed 5 of 15 calls (error_ratio=0.311) over 5m")
    ev = [Evidence(id="E1", source="logs", tool="search_logs", summary="s", args={"service": "cart"},
                   content="2026-10-23T18:29:00Z cart ERROR Wasn't able to connect to redis")]
    acts, _, items = _plan(demo_dir, tmp_path, _diag("cart", "dependency_failure"), ev, [alert],
                           operations={"cart": "EmptyCart"})
    assert [(a.action, a.parameters) for a in acts] == [("toggle_feature_flag", {"flag": "cartFailure",
                                                                                 "enabled": False})]
    cited = next(k for k in items if k.id == acts[0].citations[0].knowledge_id)
    assert cited.data["kind"] == "op_guard" and "EmptyCart" in cited.content
