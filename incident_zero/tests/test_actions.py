from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

from incident_zero.actions.catalog import MOCK_ACTIONS, validate
from incident_zero.actions.client import ActionAPIClient
from incident_zero.actions.flags import FlagFile
from incident_zero.actions.mock_server import ActionAPI, serve
from incident_zero.models import ActionResult, Incident
from incident_zero.safety import Frozen, Safety


class FakeRuntime:
    def __init__(self):
        self.calls, self.undone = [], []

    def services(self):
        return {"cart", "payment", "valkey-cart"}

    def restart_service(self, dry, service):
        self.calls.append(("restart", service, dry))
        return (f"would restart {service}" if dry else f"restarted {service}"), None

    def toggle_feature_flag(self, dry, flag, enabled):
        self.calls.append(("toggle", flag, dry))
        return "toggled", (None if dry else {"kind": "flags", "prev": {flag: "on"}})

    def undo(self, undo):
        self.undone.append(undo)
        return "restored"


@pytest.fixture
def api_server(tmp_path):
    rt = FakeRuntime()
    api = ActionAPI(rt, tmp_path / "x.json")
    srv = serve(api, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield ActionAPIClient(f"http://127.0.0.1:{srv.server_address[1]}"), rt, api
    srv.shutdown()


def test_validate_refuses_unknown_actions_and_bad_params():
    services = {"cart"}
    assert validate("restart_service", {"service": "cart"}, MOCK_ACTIONS, services) == []
    assert "not an allowed action" in validate("rm_rf", {}, MOCK_ACTIONS)[0]
    assert validate("restart_service", {}, MOCK_ACTIONS) == ["missing required parameter 'service'"]
    assert "unknown service 'billing'" in validate("restart_service", {"service": "billing"}, MOCK_ACTIONS, services)
    assert "unexpected parameter 'force'" in validate("restart_service", {"service": "cart", "force": 1}, MOCK_ACTIONS)
    assert "users above 500" in validate("throttle_load_generator", {"users": 9000}, MOCK_ACTIONS)
    assert "enabled must be true or false" in validate("toggle_feature_flag", {"flag": "x", "enabled": "no"},
                                                      MOCK_ACTIONS)
    assert validate("toggle_feature_flag", {"flag": "paymentFailure", "enabled": False}, MOCK_ACTIONS, services) == []
    assert {a.name for a in MOCK_ACTIONS} == {"restart_service", "toggle_feature_flag", "throttle_load_generator",
                                              "scale_service"}


def test_dry_run_changes_nothing(api_server):
    client, rt, _ = api_server
    r = client.execute("restart_service", {"service": "cart"}, "test", dry_run=True)
    assert r.ok and r.message == "would restart cart"
    assert rt.calls == [("restart", "cart", True)]


def test_execute_requires_reason_and_known_service(api_server):
    client, _, _ = api_server
    assert not client.execute("restart_service", {"service": "cart"}, "", dry_run=False).ok
    r = client.execute("restart_service", {"service": "billing"}, "x", dry_run=False)
    assert not r.ok and "unknown service" in r.message


def test_kill_blocks_and_rollback_undoes(api_server):
    client, rt, _ = api_server
    r = client.execute("toggle_feature_flag", {"flag": "cartFailure", "enabled": False}, "faulty path", dry_run=False)
    assert r.ok and r.execution_id
    assert client.rollback(r.execution_id).message == "restored"
    assert client.rollback(r.execution_id).message == "already rolled back"
    nr = client.execute("restart_service", {"service": "cart"}, "x", dry_run=False)
    assert "not reversible" in client.rollback(nr.execution_id).message
    client.kill()
    blocked = client.execute("restart_service", {"service": "cart"}, "x", dry_run=False)
    assert not blocked.ok and "kill switch" in blocked.message
    assert client.execute("restart_service", {"service": "cart"}, "x", dry_run=True).ok  # previews still work
    client.resume()
    assert client.execute("restart_service", {"service": "cart"}, "x", dry_run=False).ok


def test_actions_list(api_server):
    client, _, _ = api_server
    assert {a.name for a in client.actions()} >= {"restart_service", "toggle_feature_flag", "throttle_load_generator"}


def test_safety_stop_freezes_and_rolls_back_recent(api_server, store, bus, settings):
    client, rt, _ = api_server
    safety = Safety(store, bus, client, settings)
    assert safety.dry_run is True  # S1: dry run at start
    r = client.execute("toggle_feature_flag", {"flag": "cartFailure", "enabled": False}, "x", dry_run=False)
    inc = Incident(id="INC-0001", title="t")
    inc.execution.results.append(r)
    inc.execution.results.append(ActionResult(action="restart_service", ok=True, dry_run=True, execution_id="X-dry"))
    store.save(inc)
    rolled = safety.stop("preet", "drill")
    assert [x.ok for x in rolled] == [True] and len(rt.undone) == 1
    assert store.load("INC-0001").execution.rollback_performed
    with pytest.raises(Frozen):
        safety.check()
    assert not client.execute("restart_service", {"service": "cart"}, "x", dry_run=False).ok
    safety.resume("preet")
    safety.check()
    assert client.execute("restart_service", {"service": "cart"}, "x", dry_run=False).ok


def test_flag_file_set_and_undo(tmp_path):
    f = tmp_path / "src" / "flagd"
    f.mkdir(parents=True)
    (f / "demo.flagd.json").write_text('{"flags": {"paymentFailure": {"variants": {"on": 1, "off": 0}, '
                                       '"defaultVariant": "on"}, "cartFailure": {"variants": {"on": true, '
                                       '"off": false}, "defaultVariant": "off"}}}')
    ff = FlagFile(tmp_path)
    assert ff.variants() == {"paymentFailure": "on", "cartFailure": "off"}
    assert ff.on_variant("cartFailure") == "on" and ff.off_variant("cartFailure") == "off"
    prev = ff.set({"paymentFailure": "off"})
    assert prev["paymentFailure"]["defaultVariant"] == "on" and ff.variants()["paymentFailure"] == "off"
    ff.restore(prev)
    assert ff.variants()["paymentFailure"] == "on"


def test_targeted_flag_switches_through_its_rule(tmp_path):
    """Found live: productCatalogFailure is decided by its targeting rule; setting defaultVariant did nothing."""
    f = tmp_path / "src" / "flagd"
    f.mkdir(parents=True)
    rule = {"if": [{"==": [{"var": "product_id"}, "OLJCESPC7Z"]}, "off", "off"]}
    (f / "demo.flagd.json").write_text(json.dumps({"flags": {"productCatalogFailure": {
        "defaultVariant": "off", "targeting": rule, "variants": {"off": False, "on": True}}}}))
    ff = FlagFile(tmp_path)
    assert ff.variants()["productCatalogFailure"] == "off"
    prev = ff.set({"productCatalogFailure": "on"})
    flag = json.loads((f / "demo.flagd.json").read_text())["flags"]["productCatalogFailure"]
    assert flag["targeting"]["if"][1:] == ["on", "off"] and flag["defaultVariant"] == "off"
    assert ff.variants()["productCatalogFailure"] == "on"
    ff.set({"productCatalogFailure": "off"})  # the Action API's fix really turns it off
    assert ff.variants()["productCatalogFailure"] == "off"
    ff.set({"productCatalogFailure": "on"})
    ff.restore(prev)
    assert json.loads((f / "demo.flagd.json").read_text())["flags"]["productCatalogFailure"]["targeting"] == rule


def test_judge_requires_the_exact_fix():
    from incident_zero.models import ActionResult, AuditResult, Diagnosis, Incident, RootCause
    from incident_zero.practice.judge import score

    rc = RootCause(component="product-catalog", category="application_error", cause="c", confidence=0.9)
    inc = Incident(id="INC-0001", title="t", phase="resolved", diagnosis=Diagnosis(summary="s", root_causes=[rc]))
    inc.audits.append(AuditResult(passed=True))
    inc.verification.recovered = True
    inc.execution.results.append(ActionResult(action="toggle_feature_flag", ok=True, dry_run=False,
                                              parameters={"flag": "productCatalogLockContention", "enabled": False}))
    truth = {"components": ["product-catalog"], "categories": ["application_error"], "actions": ["toggle_feature_flag"],
             "fixes": [{"action": "toggle_feature_flag", "parameters": {"flag": "productCatalogFailure", "enabled": False}}]}
    s = score(truth, 0, [inc])
    assert s["detail"]["fix_executed"] is False and s["points"] == 7  # wrong flag: no action, no recovery points


# ---------------------------------------------------------------- R1 / R3 enforced structurally
PKG = Path(__file__).resolve().parents[1]
# flags and practice hold the answers (R1, R3); docker_control and mock_server are the write path (F6.1)
FORBIDDEN_FOR_AGENTS = {"incident_zero.actions.flags", "incident_zero.practice", "incident_zero.actions.docker_control",
                        "incident_zero.actions.mock_server"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # relative: resolve against the file's package
                parts = path.relative_to(PKG.parent).with_suffix("").parts[:-node.level]
                mod = ".".join(parts + ((mod,) if mod else ()))
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


@pytest.mark.parametrize("sub", ["agents", "telemetry", "knowledge"])
def test_agents_and_tools_cannot_reach_flags_or_ground_truth(sub):
    d = PKG / sub
    if not d.is_dir():
        pytest.skip(f"{sub} not built yet")
    for py in d.rglob("*.py"):
        bad = {m for m in _imports(py) if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_FOR_AGENTS)}
        assert not bad, f"{py.relative_to(PKG)} imports {bad}"
