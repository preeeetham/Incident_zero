"""A stand-in Action API, run as its own process: `incident_zero action-api`.

It owns every write to the system, so "Action API only" is structural: the agent process holds an HTTP client, not
a Docker client it could write with. Each execution is recorded with what it changed, so reversible actions can be
rolled back. Its own kill switch refuses every execution until resumed. One action at a time per service.

The runtime is pluggable: DockerRuntime acts on the Compose project, tests use a fake.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .catalog import MOCK_ACTIONS, ActionDef, validate


class RuntimeErrorForAction(RuntimeError):
    pass


class DockerRuntime:
    """Acts on the Compose project: Docker through docker_control.DockerController, flags through the demo's flagd
    file, the load generator through Locust's web API (behind the demo's Envoy at /loadgen)."""

    def __init__(self, project: str, network: str, demo_dir: str = "", loadgen_url: str = ""):
        from .docker_control import DockerController

        self.d = DockerController(project, network)
        self.demo_dir = demo_dir
        self.loadgen_url = loadgen_url.rstrip("/")

    def services(self) -> set[str]:
        return set(self.d.containers())

    def restart_service(self, dry: bool, service: str) -> tuple[str, Optional[dict]]:
        c = self.d.container(service)
        if dry:
            return f"would restart {service} (now {c.status}, {c.attrs.get('RestartCount', 0)} restarts)", None
        if c.status == "paused":
            self.d.unpause(service)
        self.d.restart(service)  # also starts an exited container; a fresh start resets its network interface
        ok = self.d.wait_running(service, 90)
        return f"restarted {service}; running={ok}", None

    def toggle_feature_flag(self, dry: bool, flag: str, enabled: bool) -> tuple[str, Optional[dict]]:
        from .flags import FlagFile

        if not self.demo_dir:
            raise RuntimeErrorForAction("toggle_feature_flag needs demo_dir (the demo checkout) in the mock's settings")
        ff = FlagFile(self.demo_dir)
        variants = ff.variants()
        if flag not in variants:
            raise RuntimeErrorForAction(f"unknown flag {flag!r}")
        target = ff.off_variant(flag) if not enabled else ff.on_variant(flag)
        if dry:
            return f"would turn {flag} {'on' if enabled else 'off'}", None
        prev = ff.set({flag: target})
        return f"turned {flag} {'on' if enabled else 'off'}", {"kind": "flags", "prev": prev}

    def _users(self) -> Optional[int]:
        import requests

        r = requests.get(f"{self.loadgen_url}/stats/requests", timeout=10)
        return int(r.json().get("user_count", 0)) if r.ok else None

    def throttle_load_generator(self, dry: bool, users: int) -> tuple[str, Optional[dict]]:
        import requests

        if not self.loadgen_url:
            raise RuntimeErrorForAction("throttle_load_generator needs loadgen_url")
        prev = self._users()
        if dry:
            return f"would set the load generator to {users} users (now {prev})", None
        r = requests.post(f"{self.loadgen_url}/swarm", data={"user_count": users, "spawn_rate": max(1, users)},
                          timeout=15)
        if not r.ok:
            raise RuntimeErrorForAction(f"load generator answered {r.status_code}")
        return f"load generator set to {users} users (was {prev})", {"kind": "users", "prev": prev}

    def scale_service(self, dry: bool, service: str, replicas: int) -> tuple[str, Optional[dict]]:
        raise RuntimeErrorForAction("scale_service is available on Kubernetes only; this stack runs on Docker Compose")

    def undo(self, undo: dict) -> str:
        if undo["kind"] == "flags":
            from .flags import FlagFile

            FlagFile(self.demo_dir).restore(undo["prev"])
            return f"restored flags {', '.join(undo['prev'])}"
        if undo["kind"] == "users" and undo["prev"] is not None:
            self.throttle_load_generator(False, int(undo["prev"]))
            return f"load generator back to {undo['prev']} users"
        raise RuntimeErrorForAction(f"cannot undo {undo['kind']}")


class ActionAPI:
    def __init__(self, runtime: Any, state_file: Optional[Path] = None, catalog: list[ActionDef] = MOCK_ACTIONS):
        self.runtime, self.catalog = runtime, catalog
        self.state_file = state_file
        self.killed = False
        self.executions: dict[str, dict] = {}
        self._svc_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        if state_file and state_file.is_file():
            data = json.loads(state_file.read_text())
            self.killed = bool(data.get("killed"))
            self.executions = data.get("executions", {})

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"killed": self.killed, "executions": self.executions}, indent=2))

    def execute(self, body: dict) -> tuple[int, dict]:
        action, params = body.get("action", ""), body.get("parameters") or {}
        dry = bool(body.get("dry_run", True))
        if self.killed and not dry:
            return 423, {"ok": False, "message": "kill switch engaged: no actions until /resume"}
        try:
            services = self.runtime.services()
        except Exception as exc:
            return 503, {"ok": False, "message": f"runtime unavailable: {exc}"}
        errs = validate(action, params, self.catalog, services)
        if errs:
            return 400, {"ok": False, "message": "; ".join(errs)}
        if not body.get("reason"):
            return 400, {"ok": False, "message": "a reason is required"}
        target = str(params.get("service") or params.get("flag") or action)
        lock = self._svc_locks[target]
        if not lock.acquire(timeout=0.1):
            return 409, {"ok": False, "message": f"another action is running on {target}"}
        try:
            fn = getattr(self.runtime, action)
            message, undo = fn(dry, **params)
        except Exception as exc:
            return 500, {"ok": False, "message": f"{action} failed: {exc}"}
        finally:
            lock.release()
        ex_id = f"X-{uuid.uuid4().hex[:10]}"
        self.executions[ex_id] = {"id": ex_id, "action": action, "parameters": params, "dry_run": dry,
                                  "reason": body.get("reason"), "incident_id": body.get("incident_id", ""),
                                  "at": time.time(), "message": message, "undo": undo, "rolled_back": False}
        self._save()
        return 200, {"ok": True, "execution_id": ex_id, "message": message, "dry_run": dry,
                     "reversible": undo is not None}

    def rollback(self, body: dict) -> tuple[int, dict]:
        ex = self.executions.get(body.get("execution_id", ""))
        if ex is None:
            return 404, {"ok": False, "message": "unknown execution"}
        if ex["dry_run"]:
            return 200, {"ok": True, "message": "dry run: nothing to roll back"}
        if ex["rolled_back"]:
            return 200, {"ok": True, "message": "already rolled back"}
        if not ex.get("undo"):
            return 409, {"ok": False, "message": f"{ex['action']} is not reversible"}
        try:
            msg = self.runtime.undo(ex["undo"])
        except Exception as exc:
            return 500, {"ok": False, "message": f"rollback failed: {exc}"}
        ex["rolled_back"] = True
        self._save()
        return 200, {"ok": True, "message": msg}


def make_handler(api: ActionAPI, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self) -> bool:
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"ok": False, "message": "unauthorised"})
                return False
            return True

        def do_GET(self):
            if not self._authorised():
                return
            if self.path == "/health":
                return self._send(200, {"ok": True, "killed": api.killed})
            if self.path == "/actions":
                return self._send(200, {"actions": [a.model_dump() for a in api.catalog]})
            if self.path == "/executions":
                return self._send(200, {"executions": list(api.executions.values())})
            self._send(404, {"ok": False, "message": "not found"})

        def do_POST(self):
            if not self._authorised():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "message": "bad json"})
            if self.path == "/actions/execute":
                return self._send(*api.execute(body))
            if self.path == "/actions/rollback":
                return self._send(*api.rollback(body))
            if self.path == "/kill":
                api.killed = True
                api._save()
                return self._send(200, {"ok": True, "killed": True})
            if self.path == "/resume":
                api.killed = False
                api._save()
                return self._send(200, {"ok": True, "killed": False})
            self._send(404, {"ok": False, "message": "not found"})

    return Handler


def serve(api: ActionAPI, host: str, port: int, token: str = "") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(api, token))
    srv.daemon_threads = True
    return srv
