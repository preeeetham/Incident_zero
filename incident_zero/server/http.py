"""The Control Room's server: a JSON API, a server-sent-events stream, and the built React app.

  GET  /api/state                         mode, kill switch, eval mode, model, endpoints, counts
  GET  /api/incidents                     list (newest first)
  GET  /api/incidents/<id>                the incident
  GET  /api/incidents/<id>/evidence       the evidence ledger
  GET  /api/incidents/<id>/events         its timeline
  GET  /api/incidents/<id>/report         {json, markdown}
  POST /api/incidents/<id>/decision       {decision: approved|rejected|sent_back, approver, change_hash, note}
  GET  /api/approvals                     incidents waiting for a decision
  GET  /api/agents                        live status of the nine agents
  GET  /api/overview                      stability samples, containers, traffic/errors, incident markers
  GET  /api/services                      per-service health snapshot (cached a few seconds)
  GET  /api/events?after=<seq>            events after a sequence number (polling fallback)
  GET  /api/stream                        server-sent events, one per agent step
  GET  /api/submission                    every report, wrapped as {"incident_reports": [...]}
  POST /api/mode                          {dry_run, actor}
  POST /api/stop | /api/resume            {actor, reason}
  GET  /api/practice/...  POST ...        only when not in eval mode (REQUIREMENTS C10)

Standard library only: no web framework dependency.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..agents.approval import ApprovalError
from ..app import App
from ..telemetry.tools import Toolbox
from .overview import Overview

UI_DIST = Path(__file__).resolve().parents[1] / "ui" / "dist"
INC_RE = re.compile(r"^/api/incidents/(INC-\d{4,})(/[a-z]+)?$")


class _ServicesCache:
    def __init__(self, app: App, ttl: float = 5.0):
        self.app, self.ttl, self._at, self._val = app, ttl, 0.0, None
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if self._val is None or time.time() - self._at > self.ttl:
                box = Toolbox(self.app.ctx.telemetry, None)  # type: ignore[arg-type]  # snapshot records nothing
                self._val, self._at = box.snapshot(), time.time()
            return self._val


def _summary(inc) -> dict:
    return {"id": inc.id, "title": inc.title, "phase": inc.phase, "created_at": inc.created_at,
            "updated_at": inc.updated_at, "services": inc.services,
            "alerts": len(inc.alerts), "active_alerts": sum(1 for a in inc.alerts if a.active),
            "root_cause": (inc.diagnosis.root_causes[0].component if inc.diagnosis and inc.diagnosis.root_causes
                           else None),
            "confidence": inc.diagnosis.confidence if inc.diagnosis else None,
            "approval": inc.approval.decision}


def make_handler(app: App, ui_dir: Path = UI_DIST):
    ctx = app.ctx
    services = _ServicesCache(app)
    overview = Overview(app, services.get)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ------------------------------------------------------------ helpers
        def _json(self, code: int, body: Any) -> None:
            raw = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                raise ValueError("body too large")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _state(self) -> dict:
            incs = ctx.store.list()
            ep = ctx.telemetry.endpoints
            return {
                "dry_run": ctx.safety.dry_run, "frozen": ctx.safety.frozen, "eval_mode": app.settings.eval_mode,
                "llm": ctx.llm.label if ctx.llm.enabled else None, "llm_reason": ctx.llm.reason,
                "endpoints": {"prometheus": ep.prometheus, "jaeger": ep.jaeger, "opensearch": ep.opensearch,
                              "action_api": app.settings.action_api_url, "notes": ep.notes},
                "open": sum(1 for i in incs if not i.terminal),
                "awaiting_approval": sum(1 for i in incs if i.phase == "awaiting_approval"),
                "total": len(incs), "last_event": (ctx.store.state().get("event_seq") or 0),
                "project": app.settings.compose_project, "approval_timeout_s": app.settings.approval_timeout_s,
            }

        # ------------------------------------------------------------ GET
        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            try:
                if path == "/api/state":
                    return self._json(200, self._state())
                if path == "/api/incidents":
                    return self._json(200, [_summary(i) for i in ctx.store.list()])
                if path == "/api/approvals":
                    return self._json(200, [i.model_dump() for i in ctx.store.list() if i.phase == "awaiting_approval"])
                if path == "/api/agents":
                    recent = ctx.store.events(limit=400)
                    rows = ctx.status.rows()
                    for r in rows:
                        r["recent"] = [e.model_dump() for e in recent if e.agent == r["agent"]][-5:]
                    return self._json(200, rows)
                if path == "/api/services":
                    return self._json(200, services.get())
                if path == "/api/overview":
                    return self._json(200, overview.get())
                if path == "/api/events":
                    after = int((qs.get("after") or ["0"])[0])
                    return self._json(200, [e.model_dump() for e in ctx.store.events(after=after, limit=500)])
                if path == "/api/submission":
                    p = ctx.store.root / "submission.json"
                    return self._json(200, json.loads(p.read_text()) if p.is_file() else {"incident_reports": []})
                if path == "/api/stream":
                    return self._stream()
                if path.startswith("/api/practice"):
                    return self._practice("GET", path, None)
                m = INC_RE.match(path)
                if m:
                    return self._incident(m.group(1), m.group(2) or "")
                if path.startswith("/api/"):
                    return self._json(404, {"error": "not found"})
                return self._static(path)
            except BrokenPipeError:
                return
            except Exception as exc:
                return self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

        def _incident(self, inc_id: str, sub: str):
            inc = ctx.store.load(inc_id)
            if inc is None:
                return self._json(404, {"error": f"no incident {inc_id}"})
            if sub == "":
                return self._json(200, inc.model_dump())
            if sub == "/evidence":
                return self._json(200, [e.model_dump() for e in ctx.store.evidence(inc_id)])
            if sub == "/events":
                return self._json(200, [e.model_dump() for e in ctx.store.events(incident_id=inc_id)])
            if sub == "/report":
                j, md = ctx.store.report(inc_id)
                return self._json(200 if j else 404, {"json": j, "markdown": md})
            return self._json(404, {"error": "not found"})

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = ctx.bus.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"id: {ev.seq}\nevent: event\ndata: {ev.model_dump_json()}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                ctx.bus.unsubscribe(q)
                self.close_connection = True

        def _static(self, path: str):
            if not ui_dir.is_dir():
                raw = (b"<!doctype html><meta charset=utf-8><title>Incident Zero</title><body style='font:14px "
                       b"system-ui;background:#0e1013;color:#c9ccd1;padding:40px'>The Control Room is not built yet. "
                       b"Run <code>incident_zero ui-build</code>. The API is live at <code>/api/state</code>.")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            target = (ui_dir / path.lstrip("/")).resolve()
            if not str(target).startswith(str(ui_dir.resolve())) or not target.is_file():
                target = ui_dir / "index.html"  # client-side routes
            raw = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(raw)))
            if "/assets/" in str(target):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(raw)

        # ------------------------------------------------------------ POST
        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc)})
            try:
                m = INC_RE.match(path)
                if m and m.group(2) == "/decision":
                    try:
                        inc = app.orchestrator.approval.decide(
                            m.group(1), str(body.get("decision", "")), str(body.get("approver", "")),
                            str(body.get("change_hash", "")), str(body.get("note", "")))
                    except ApprovalError as exc:
                        return self._json(409, {"error": str(exc)})
                    return self._json(200, inc.model_dump())
                actor = str(body.get("actor") or "").strip()
                if path in ("/api/mode", "/api/stop", "/api/resume") and not actor:
                    return self._json(400, {"error": "actor (your name) is required"})
                if path == "/api/mode":
                    ctx.safety.set_dry_run(bool(body.get("dry_run", True)), actor)
                    return self._json(200, self._state())
                if path == "/api/stop":
                    rolled = ctx.safety.stop(actor, str(body.get("reason", "")))
                    return self._json(200, {**self._state(), "rolled_back": [r.model_dump() for r in rolled]})
                if path == "/api/resume":
                    ctx.safety.resume(actor)
                    return self._json(200, self._state())
                if path.startswith("/api/practice"):
                    return self._practice("POST", path, body)
                return self._json(404, {"error": "not found"})
            except Exception as exc:
                return self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

        # ------------------------------------------------------------ practice (never in eval mode)
        def _practice(self, method: str, path: str, body: Optional[dict]):
            if app.settings.eval_mode or app.practice is None:
                return self._json(404, {"error": "not found"})
            return self._json(*app.practice.handle(method, path, body))

    return Handler


def serve(app: App, host: str, port: int, ui_dir: Path = UI_DIST) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(app, ui_dir))
    srv.daemon_threads = True
    return srv
