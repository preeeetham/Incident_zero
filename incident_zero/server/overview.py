"""The War Room's live picture: system stability, container states, shop traffic and errors, and incident markers.

Everything comes from the running system, nothing is generated:
  * stability and containers: sampled every `interval_s` from the same per-service snapshot the Services page shows
    (Prometheus span metrics + Docker state), judged by the Sentinel's own thresholds, kept for `keep_s`
  * traffic and errors: Prometheus range queries over the span metrics, the last `window_s`
  * markers: the incident store (alert opened, fix executed live, resolved)
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Optional

from ..telemetry import queries as Q
from ..telemetry.backends import BackendError


def _ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def containers_of(snapshot: dict) -> list[dict]:
    out = []
    for name, r in sorted((snapshot.get("services") or {}).items()):
        c = r.get("container")
        if c:
            out.append({"name": name, "state": c.get("state"), "running": bool(c.get("running")),
                        "restarts": c.get("restart_count") or 0, "oom": bool(c.get("oom_killed")),
                        "uptime_s": c.get("uptime_s")})
    return out


def judge(snapshot: dict, settings) -> dict:
    """One stability sample: which services are healthy by the Sentinel's alert rules, and the container states."""
    th, ignore = settings.thresholds, set(settings.ignore_services)
    healthy, unhealthy, containers = [], [], containers_of(snapshot)
    for name, r in sorted((snapshot.get("services") or {}).items()):
        c, n, b = r.get("container"), r.get("now"), r.get("baseline") or {}
        if name in ignore:
            continue
        why = ""
        if c and not c.get("running"):
            why = f"container {c.get('state')}"
        elif n and n.get("rps", 0) >= th.min_rps and n.get("error_ratio", 0) >= th.error_ratio:
            why = f"errors {n['error_ratio'] * 100:.0f}%"
        else:
            p, bp = (n or {}).get("p95_ms"), b.get("p95_ms")
            if p and bp and p > max(th.latency_p95_floor_ms, th.latency_factor * bp):
                why = f"p95 {p:.0f} ms"
        (unhealthy if why else healthy).append({"name": name, "why": why})
    total = len(healthy) + len(unhealthy)
    return {"t": snapshot.get("at") or time.time(), "score": (len(healthy) / total) if total else None,
            "healthy": len(healthy), "total": total, "unhealthy": unhealthy,
            "up": sum(1 for c in containers if c["running"]), "containers": len(containers)}


class Overview:
    def __init__(self, app, snapshot: Callable[[], dict], interval_s: float = 15.0, keep_s: float = 3600.0,
                 window_s: float = 1800.0, start: bool = True):
        self.app, self.snapshot = app, snapshot
        self.interval_s, self.window_s = interval_s, window_s
        self.samples: deque = deque(maxlen=int(keep_s / interval_s) + 1)
        self.containers: list[dict] = []
        self._traffic: tuple[float, dict] = (0.0, {})
        self._lock = threading.Lock()
        self.since = time.time()
        if start:
            threading.Thread(target=self._loop, daemon=True, name="overview").start()

    def sample(self) -> dict:
        snap = self.snapshot()
        s = judge(snap, self.app.settings)
        with self._lock:
            self.containers = containers_of(snap)
            if s["total"]:
                self.samples.append(s)
        return s

    def _loop(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(self.interval_s)

    def traffic(self) -> dict:
        """Shop traffic (requests/s into the entry service) and the error share of all server calls, from
        Prometheus over the last window_s. Cached for interval_s."""
        at, cached = self._traffic
        if time.time() - at < self.interval_s and cached:
            return cached
        prom, s = self.app.ctx.telemetry.prom, self.app.settings
        end = time.time()
        start, step = end - self.window_s, max(15.0, self.window_s / 120)
        entry = (s.entry_services or ["frontend-proxy"])[0]
        sel_in = Q.INBOUND
        w = s.rate_window  # the Sentinel's window: span metrics arrive about once a minute, so 1m rates are empty
        q_rps = f'sum(rate({Q.CALLS}{{{sel_in},service_name="{entry}"}}[{w}]))'
        # `or vector(0)`: no failing calls at all is 0 %, not a gap in the line
        q_err = (f"100 * (sum(rate({Q.CALLS}{{{sel_in},{Q.ERROR}}}[{w}])) or vector(0)) / "
                 f"sum(rate({Q.CALLS}{{{sel_in}}}[{w}]))")
        out: dict[str, Any] = {"entry": entry, "rps_query": q_rps, "error_query": q_err, "rps": [], "errors": [],
                               "by_service": {}, "error": None}

        def series(res: list[dict]) -> list[list[float]]:
            return [[float(t), float(v)] for t, v in res if v not in ("NaN", "+Inf", "-Inf")]

        try:
            for key, q in (("rps", q_rps), ("errors", q_err)):
                res = prom.query_range(q, start, end, step)
                out[key] = series(res[0]["values"]) if res else []
            # per service, for the Services page's trend lines: the Sentinel's rps and error-ratio queries over time
            for key, q in (("rps", Q.rps(w)), ("errors", f"100 * ({Q.error_rps(w)}) / ({Q.rps(w)})")):
                for item in prom.query_range(q, start, end, step):
                    svc = item.get("metric", {}).get("service_name")
                    if svc:
                        out["by_service"].setdefault(svc, {"rps": [], "errors": []})[key] = series(item["values"])
        except (BackendError, KeyError, ValueError, IndexError) as exc:
            out["error"] = str(exc)[:200]
        self._traffic = (time.time(), out)
        return out

    def incidents(self, since: float) -> list[dict]:
        out = []
        for inc in self.app.ctx.store.list():
            start = _ts(inc.created_at)
            if start is None:
                continue
            end = _ts(inc.resolved_at) or (_ts(inc.updated_at) if inc.terminal else None)
            if end is not None and end < since:
                continue
            fixes = [{"t": _ts(r.at), "action": r.action, "parameters": r.parameters}
                     for r in inc.execution.results if r.ok and not r.dry_run and _ts(r.at)]
            out.append({"id": inc.id, "title": inc.title, "phase": inc.phase, "start": start, "end": end,
                        "fixes": fixes, "services": inc.services})
        return out

    def get(self) -> dict:
        now = time.time()
        with self._lock:
            samples = [x for x in self.samples if x["t"] >= now - self.window_s]
            containers = list(self.containers)
        if not containers:
            containers = containers_of(self.snapshot())
        return {"now": now, "window_s": self.window_s, "since": self.since, "samples": samples,
                "latest": samples[-1] if samples else None, "containers": containers,
                "traffic": self.traffic(), "incidents": self.incidents(now - self.window_s),
                "definition": "A service is healthy when its container runs, its server error rate is under "
                              f"{self.app.settings.thresholds.error_ratio * 100:.0f}% and its p95 latency is within "
                              f"{self.app.settings.thresholds.latency_factor:g}x its baseline: the Sentinel's own "
                              "alert rules."}
