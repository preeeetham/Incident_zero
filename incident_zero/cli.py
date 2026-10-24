"""`incident_zero` command line.

  incident_zero demo up|down|ps          the OpenTelemetry Demo (compose.yaml + compose.observability.yaml)
  incident_zero action-api               the mock Action API (its own process; owns every write)
  incident_zero up [--eval]              Sentinel + agents + Control Room on http://127.0.0.1:8700
  incident_zero status                   mode, kill switch, open incidents
  incident_zero stop|resume --as NAME    the kill switch (works without the server)
  incident_zero decide INC approve|reject|send-back --as NAME [--note ...]
  incident_zero report INC [--json]
  incident_zero practice list|run ID|run blind|runs|stop RUN|campaign IDS|campaign blind
  incident_zero ui-build                 build the Control Room (npm)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from .config import PACKAGE_DIR, load_settings


def _api(s, method: str, path: str, body: dict | None = None) -> dict:
    url = f"http://{s.host}:{s.port}{path}"
    try:
        r = requests.request(method, url, json=body, timeout=30)
    except requests.RequestException:
        sys.exit(f"Incident Zero is not running at http://{s.host}:{s.port} (start it with `incident_zero up`)")
    data = r.json()
    if r.status_code >= 400:
        sys.exit(f"error: {data.get('error', data)}")
    return data


def cmd_up(s, a) -> int:
    from .app import build
    from .server.http import serve

    s.eval_mode = s.eval_mode or a.eval
    if a.port:
        s.port = a.port
    app = build(s)
    srv = serve(app, s.host, s.port)
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    app.orchestrator.start(sentinel=not a.no_sentinel)
    ctx = app.ctx
    ep = ctx.telemetry.endpoints
    print(f"Incident Zero Control Room  http://{s.host}:{s.port}")
    print(f"  mode          {'DRY RUN' if ctx.safety.dry_run else 'LIVE'}{'   (kill switch ENGAGED)' if ctx.safety.frozen else ''}")
    print(f"  eval mode     {'on: practice is disabled' if s.eval_mode else 'off'}")
    print(f"  model         {ctx.llm.label if ctx.llm.enabled else 'none: ' + ctx.llm.reason}")
    print(f"  prometheus    {ep.prometheus or '-'}\n  jaeger        {ep.jaeger or '-'}\n  opensearch    {ep.opensearch or '-'}")
    print(f"  action api    {s.action_api_url} ({'reachable' if ctx.actions.health() else 'NOT reachable'})")
    for n in ep.notes:
        print(f"  note: {n}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app.orchestrator.stop()
        srv.shutdown()
    return 0


def cmd_action_api(s, a) -> int:
    from .actions.mock_server import ActionAPI, DockerRuntime, serve

    rt = DockerRuntime(a.project or s.compose_project, a.network, a.demo_dir or s.demo_dir,
                       s.loadgen_url or s.frontend_url.rstrip("/") + "/loadgen")
    api = ActionAPI(rt, Path(s.data_dir) / "action_api" / "state.json")
    host, port = a.host, a.port
    srv = serve(api, host, port, s.action_api_token)
    print(f"mock Action API on http://{host}:{port} for project {rt.d.project} "
          f"({'kill switch ENGAGED' if api.killed else 'ready'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


def _safety(s):
    from .actions.client import ActionAPIClient
    from .events import EventBus
    from .safety import Safety
    from .store import IncidentStore

    store = IncidentStore(s.data_dir)
    return Safety(store, EventBus(store), ActionAPIClient(s.action_api_url, s.action_api_token), s), store


def cmd_status(s, a) -> int:
    safety, store = _safety(s)
    print(f"mode {'DRY RUN' if safety.dry_run else 'LIVE'}; kill switch {'ENGAGED' if safety.frozen else 'off'}")
    for inc in store.list()[:15]:
        rc = inc.diagnosis.root_causes[0].component if inc.diagnosis and inc.diagnosis.root_causes else "-"
        print(f"{inc.id}  {inc.phase:<18} {rc:<16} {inc.title}")
    return 0


def cmd_stop(s, a) -> int:
    safety, _ = _safety(s)
    rolled = safety.stop(a.actor, a.reason or "")
    print(f"kill switch ENGAGED; {len(rolled)} recent action(s) rolled back")
    for r in rolled:
        print(f"  {r.execution_id}: {r.message}")
    return 0


def cmd_resume(s, a) -> int:
    safety, _ = _safety(s)
    safety.resume(a.actor)
    print("kill switch released")
    return 0


def cmd_decide(s, a) -> int:
    inc = _api(s, "GET", f"/api/incidents/{a.incident}")
    decision = {"approve": "approved", "reject": "rejected", "send-back": "sent_back"}[a.decision]
    if inc["phase"] != "awaiting_approval":
        sys.exit(f"{a.incident} is not waiting for approval (phase {inc['phase']})")
    plan = inc["plan"]
    print(f"{a.incident}: {inc['diagnosis']['summary']}")
    for x in plan["actions"]:
        print(f"  {x['action']} {json.dumps(x['parameters'])}: {x['reason']}\n    preview: {x.get('preview')}")
    out = _api(s, "POST", f"/api/incidents/{a.incident}/decision",
               {"decision": decision, "approver": a.actor, "change_hash": plan["change_hash"], "note": a.note or ""})
    print(f"{a.incident}: {out['approval']['decision']} by {out['approval']['approver']}")
    return 0


def cmd_report(s, a) -> int:
    from .store import IncidentStore

    j, md = IncidentStore(s.data_dir).report(a.incident)
    if j is None:
        sys.exit(f"no report for {a.incident} yet")
    print(json.dumps(j, indent=2) if a.json else md)
    return 0


def cmd_practice(s, a) -> int:
    if a.what == "list":
        for x in _api(s, "GET", "/api/practice/scenarios"):
            print(f"{x['id']:<28} {x['split']:<8} {','.join(x['faults']):<16} {x['title']}")
    elif a.what == "run":
        r = _api(s, "POST", "/api/practice/run", {"scenario_id": a.arg, "duration_s": a.duration})
        print(f"{r['id']} started: {r['title']} (auto-revert after {a.duration:.0f}s)")
        if a.arg == "blind":
            print("which fault was injected is revealed when the run is scored (`practice runs`)")
    elif a.what == "campaign":
        from .practice.campaign import main_campaign

        if not a.actor:
            sys.exit("campaign needs --as <operator name>")
        scen = [x for x in (a.arg or "").split(",") if x] or None
        path = main_campaign(f"http://{s.host}:{s.port}", f"{a.actor} (test operator)", s.practice_dir, scen, a.live,
                             gap_s=a.gap)
        print(f"campaign results: {path}")
    elif a.what == "stop":
        r = _api(s, "POST", f"/api/practice/runs/{a.arg}/stop", {})
        print(json.dumps(r.get("score"), indent=2))
    else:
        for r in _api(s, "GET", "/api/practice/runs"):
            sc = r.get("score") or {}
            print(f"{r['id']} {r['scenario_id']:<28} {r['status']:<9} {sc.get('points', '-')}/10")
    return 0


def _compose(s, args: list[str]) -> int:
    demo = Path(s.demo_dir).expanduser()
    if not (demo / "compose.yaml").is_file():
        sys.exit("set demo_dir in incident_zero.yaml (or IZ_DEMO_DIR) to a checkout of open-telemetry/opentelemetry-demo")
    cmd = ["docker", "compose", "-p", s.compose_project, "--env-file", ".env"]
    if (demo / ".env.override").is_file():
        cmd += ["--env-file", ".env.override"]
    for f in s.compose_files:
        cmd += ["-f", f]
    print("$ " + " ".join(cmd + args))
    return subprocess.call(cmd + args, cwd=demo)


def cmd_demo(s, a) -> int:
    return _compose(s, {"up": ["up", "-d", "--remove-orphans"], "down": ["down"], "ps": ["ps"]}[a.action])


def cmd_ui_build(s, a) -> int:
    ui = PACKAGE_DIR / "ui"
    if not (ui / "node_modules").is_dir():
        if subprocess.call(["npm", "ci"], cwd=ui):
            return 1
    return subprocess.call(["npm", "run", "build"], cwd=ui)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="incident_zero", description="AI on-call engineer for the OpenTelemetry Demo")
    p.add_argument("--config", help="incident_zero.yaml (default: ./incident_zero.yaml, then the repo root)")
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="start the Sentinel, the agents and the Control Room")
    up.add_argument("--eval", action="store_true", help="evaluation mode: practice route, API and Simulator off")
    up.add_argument("--no-sentinel", action="store_true", help="do not watch for new alerts (resume open incidents only)")
    up.add_argument("--port", type=int)
    api = sub.add_parser("action-api", help="run the mock Action API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8701)
    api.add_argument("--project")
    api.add_argument("--network", default="opentelemetry-demo")
    api.add_argument("--demo-dir")
    sub.add_parser("status", help="mode, kill switch, recent incidents")
    st = sub.add_parser("stop", help="KILL SWITCH: freeze agents, block actions, roll back recent actions")
    st.add_argument("--as", dest="actor", required=True)
    st.add_argument("--reason")
    rs = sub.add_parser("resume", help="release the kill switch")
    rs.add_argument("--as", dest="actor", required=True)
    de = sub.add_parser("decide", help="approve, reject or send back a plan")
    de.add_argument("incident")
    de.add_argument("decision", choices=["approve", "reject", "send-back"])
    de.add_argument("--as", dest="actor", required=True)
    de.add_argument("--note")
    rp = sub.add_parser("report", help="print an incident report")
    rp.add_argument("incident")
    rp.add_argument("--json", action="store_true")
    pr = sub.add_parser("practice", help="practice incidents (not in eval mode)")
    pr.add_argument("what", choices=["list", "run", "runs", "stop", "campaign"])
    pr.add_argument("arg", nargs="?", help="scenario id or `blind` (run), run id (stop), comma-separated ids or `blind` (campaign)")
    pr.add_argument("--duration", type=float, default=600)
    pr.add_argument("--as", dest="actor", help="campaign: the test operator who approves plans")
    pr.add_argument("--live", action="store_true", help="campaign: execute approved actions for real")
    pr.add_argument("--gap", type=float, default=320, help="campaign: seconds between scenarios (>300 when consecutive "
                    "scenarios hit the same service)")
    dm = sub.add_parser("demo", help="the OpenTelemetry Demo stack")
    dm.add_argument("action", choices=["up", "down", "ps"])
    sub.add_parser("ui-build", help="build the Control Room front end")
    a = p.parse_args(argv)
    s = load_settings(a.config)
    handlers = {"up": cmd_up, "action-api": cmd_action_api, "status": cmd_status, "stop": cmd_stop,
                "resume": cmd_resume, "decide": cmd_decide, "report": cmd_report, "practice": cmd_practice,
                "demo": cmd_demo, "ui-build": cmd_ui_build}
    return handlers[a.cmd](s, a)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
