from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest
import requests

from incident_zero.actions.mock_server import ActionAPI
from incident_zero.app import build
from incident_zero.llm import LLM
from incident_zero.models import Claim, Diagnosis, Incident, Plan, ProposedAction, RootCause
from incident_zero.practice.judge import score
from incident_zero.practice.runner import Practice
from incident_zero.server.http import serve

from . import fakes
from .test_pipeline import InProcClient, Runtime, payment_failure, tick_until_incident, wait_phase


class FakeSim:
    def __init__(self):
        self.injected, self.reverted = [], []

    def inject(self, run_id, scenario, duration_s):
        self.injected.append(scenario["id"])
        return [{"kind": "flag", "prev": {}}]

    def revert(self, undo):
        self.reverted.append(undo)
        return ["reverted"]


def _start(settings, eval_mode=False, tmp_path=None):
    settings.eval_mode = eval_mode
    svcs, traces = payment_failure()
    client = InProcClient(ActionAPI(Runtime(svcs)))
    app = build(settings, telemetry=fakes.telemetry(settings, svcs, traces), actions=client, llm=LLM())
    if app.practice is not None:
        app.practice = Practice(settings, simulator=FakeSim(), store=app.ctx.store, watch=False)  # type: ignore[arg-type]
    srv = serve(app, "127.0.0.1", 0, ui_dir=(tmp_path / "no-ui") if tmp_path else None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return app, srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def running(settings, tmp_path):
    app, srv, base = _start(settings, tmp_path=tmp_path)
    yield app, base
    srv.shutdown()


def test_state_and_static_fallback(running):
    app, base = running
    st = requests.get(base + "/api/state").json()
    assert st["dry_run"] is True and st["frozen"] is False and st["eval_mode"] is False
    page = requests.get(base + "/incidents/INC-0001/plan")
    assert page.status_code == 200 and "not built yet" in page.text
    assert requests.get(base + "/api/nope").status_code == 404


def test_decision_over_http(running):
    app, base = running
    app.orchestrator.start(sentinel=False)
    inc_id = tick_until_incident(app.orchestrator)
    wait_phase(app.ctx.store, inc_id, ("awaiting_approval",))
    inc = requests.get(f"{base}/api/incidents/{inc_id}").json()
    assert [a["id"] for a in requests.get(base + "/api/approvals").json()] == [inc_id]
    bad = requests.post(f"{base}/api/incidents/{inc_id}/decision",
                        json={"decision": "approved", "approver": "preet", "change_hash": "x"})
    assert bad.status_code == 409 and "plan changed" in bad.json()["error"]
    noname = requests.post(f"{base}/api/incidents/{inc_id}/decision",
                           json={"decision": "approved", "approver": " ", "change_hash": inc["plan"]["change_hash"]})
    assert noname.status_code == 409
    ok = requests.post(f"{base}/api/incidents/{inc_id}/decision",
                       json={"decision": "approved", "approver": "preet", "change_hash": inc["plan"]["change_hash"]})
    assert ok.status_code == 200 and ok.json()["approval"]["approver"] == "preet"
    wait_phase(app.ctx.store, inc_id, ("dry_run_complete",))
    rep = requests.get(f"{base}/api/incidents/{inc_id}/report").json()
    assert rep["json"]["approval"]["decision"] == "approved" and rep["markdown"].startswith("# ")
    assert requests.get(base + "/api/submission").json()["incident_reports"][0]["incident_id"] == inc_id
    assert len(requests.get(f"{base}/api/incidents/{inc_id}/evidence").json()) >= 5
    assert any(r["agent"] == "investigator" and r["runs"] >= 1 for r in requests.get(base + "/api/agents").json())


def test_mode_and_kill_switch_need_a_name(running):
    app, base = running
    assert requests.post(base + "/api/mode", json={"dry_run": False}).status_code == 400
    assert requests.post(base + "/api/mode", json={"dry_run": False, "actor": "preet"}).json()["dry_run"] is False
    assert requests.post(base + "/api/stop", json={"actor": "preet", "reason": "drill"}).json()["frozen"] is True
    assert requests.post(base + "/api/resume", json={"actor": "preet"}).json()["frozen"] is False


def test_event_stream_delivers_events(running):
    app, base = running
    got = []

    def read():
        with urllib.request.urlopen(base + "/api/stream", timeout=5) as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    got.append(json.loads(line[6:]))
                    return

    t = threading.Thread(target=read, daemon=True)
    t.start()
    time.sleep(0.3)
    app.ctx.bus.publish("system", "system", "hello control room")
    t.join(5)
    assert got and got[0]["title"] == "hello control room"


def test_services_snapshot(running):
    app, base = running
    svc = requests.get(base + "/api/services").json()["services"]
    assert svc["payment"]["now"]["error_ratio"] == pytest.approx(0.6)
    assert svc["cart"]["callees"] == ["valkey-cart"]


def test_practice_is_absent_in_eval_mode(settings, tmp_path):
    app, srv, base = _start(settings, eval_mode=True, tmp_path=tmp_path)
    try:
        assert app.practice is None
        assert requests.get(base + "/api/practice/scenarios").status_code == 404
        assert requests.post(base + "/api/practice/run", json={"scenario_id": "cart-stopped"}).status_code == 404
    finally:
        srv.shutdown()


def test_practice_run_hides_truth_until_scored(running):
    app, base = running
    scen = requests.get(base + "/api/practice/scenarios").json()
    assert len(scen) == 10 and "truth" not in scen[0]  # only the scenarios verified live are listed
    assert requests.post(base + "/api/practice/run", json={"scenario_id": "flag-x"}).status_code == 400
    r = requests.post(base + "/api/practice/run", json={"scenario_id": "cart-stopped"}).json()
    assert r["status"] == "active" and "truth" not in r
    assert requests.post(base + "/api/practice/run", json={"scenario_id": "valkey-down"}).status_code == 400
    done = requests.post(f"{base}/api/practice/runs/{r['id']}/stop", json={}).json()
    assert done["status"] == "reverted" and done["truth"]["components"] == ["cart"]
    assert done["score"]["detail"]["detected"] is False


def test_judge_scores_a_good_run():
    rc = RootCause(component="valkey-cart", category="container_down", cause="stopped", confidence=0.9,
                   claims=[Claim(text="x", evidence_id="E1", quote="state=exited")])
    inc = Incident(id="INC-0001", title="t", phase="resolved",
                   diagnosis=Diagnosis(summary="s", root_causes=[rc]),
                   plan=Plan(actions=[ProposedAction(action="restart_service", parameters={"service": "valkey-cart"},
                                                     reason="r")], change_hash="h"))
    from incident_zero.models import AuditResult

    inc.audits.append(AuditResult(passed=True))
    inc.verification.recovered = True
    truth = {"components": ["valkey-cart"], "categories": ["container_down"], "actions": ["restart_service"]}
    s = score(truth, 0, [inc])
    assert s["points"] == 10
    combo = score({**truth, "components": ["valkey-cart", "payment"], "all_components": True}, 0, [inc])
    assert combo["points"] == 8.5
    # a fault no allowed action fixes: proposing nothing and escalating earns the action and recovery points
    starved = Incident(id="INC-0002", title="t", phase="escalated",
                       diagnosis=Diagnosis(summary="s", root_causes=[rc.model_copy(update={"category": "resource_cpu"})]),
                       plan=Plan(actions=[], change_hash="", no_action_reason="limit"))
    starved.audits.append(AuditResult(passed=True))
    esc = score({"components": ["valkey-cart"], "categories": ["resource_cpu"], "actions": [], "escalate": True},
                0, [starved])
    assert esc["points"] == 10 and esc["detail"]["action_ok"]


def test_blind_run_hides_which_fault_until_scored(running):
    app, base = running
    scen = requests.get(base + "/api/practice/scenarios").json()
    assert not any(s["split"] == "blind" and s["id"] != "blind" for s in scen)  # blind ids and titles not listed
    r = requests.post(base + "/api/practice/run", json={"scenario_id": "blind"}).json()
    assert r["scenario_id"] == "blind" and "truth" not in r
    listed = requests.get(base + "/api/practice/runs").json()[0]
    assert listed["scenario_id"] == "blind" and "hidden" in listed["title"]
    done = requests.post(f"{base}/api/practice/runs/{r['id']}/stop", json={}).json()
    assert done["scenario_id"] != "blind" and done["truth"]


def test_overview_is_built_from_the_live_snapshot(running):
    app, base = running
    ov = requests.get(base + "/api/overview").json()
    assert {"samples", "containers", "traffic", "incidents", "definition"} <= set(ov)
    names = {c["name"] for c in ov["containers"]}
    assert "payment" in names and all("running" in c for c in ov["containers"])
    assert ov["traffic"]["error"] is None and ov["traffic"]["rps"]


def test_stability_uses_the_sentinels_rules(settings):
    from incident_zero.server.overview import judge

    snap = {"at": 1.0, "services": {
        "cart": {"now": {"rps": 1.0, "error_ratio": 0.3, "p95_ms": 50}, "baseline": {"p95_ms": 40},
                 "container": {"state": "running", "running": True, "restart_count": 0}},
        "valkey-cart": {"now": None, "baseline": None, "container": {"state": "exited", "running": False}},
        "quote": {"now": {"rps": 1.0, "error_ratio": 0.0, "p95_ms": 2000}, "baseline": {"p95_ms": 100},
                  "container": {"state": "running", "running": True}},
        "ad": {"now": {"rps": 1.0, "error_ratio": 0.01, "p95_ms": 90}, "baseline": {"p95_ms": 80},
               "container": {"state": "running", "running": True}},
        "load-generator": {"now": {"rps": 0, "error_ratio": 1.0}, "container": {"state": "running", "running": True}},
    }}
    s = judge(snap, settings)
    assert (s["healthy"], s["total"]) == (1, 4)                      # load-generator is ignored, as by the Sentinel
    assert {u["name"]: u["why"] for u in s["unhealthy"]} == {
        "cart": "errors 30%", "valkey-cart": "container exited", "quote": "p95 2000 ms"}
    assert (s["up"], s["containers"]) == (4, 5)


def test_brain_endpoint_and_agent_layers(running):
    app, base = running
    settings = app.settings
    settings.approval_timeout_s = 0.3
    app.orchestrator.start(sentinel=False)
    inc_id = tick_until_incident(app.orchestrator)
    wait_phase(app.ctx.store, inc_id, ("escalated",))
    brain = requests.get(base + "/api/brain").json()
    assert brain["incidents"] == 1 and brain["by_outcome"] == {"escalated": 1} and brain["recall_enabled"] is True
    assert brain["cases"][0]["incident"] == inc_id and brain["cases"][0]["mode"] == "reasoned"
    layers = {r["agent"]: r["layer"] for r in requests.get(base + "/api/agents").json()}
    assert layers["investigator"] == "eyes" and layers["executor"] == "hands"
    inc = requests.get(f"{base}/api/incidents/{inc_id}").json()
    assert inc["tokens"]["calls"] == 0 and inc["recall"] is None  # no model configured in this fixture
